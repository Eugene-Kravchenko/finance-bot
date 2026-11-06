"""Тесты на синтетических данных. Реальная база не используется."""
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["TZ"] = "Europe/Moscow"
os.environ["CHECKIN_TIMES"] = "10:00,16:00,22:00"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

import access  # noqa: E402
import bot  # noqa: E402
import charts  # noqa: E402
import config  # noqa: E402
import dashboard  # noqa: E402
import doctor  # noqa: E402
import stats  # noqa: E402
import storage  # noqa: E402

OWNER, VIEWER, STRANGER = 111, 222, 333
NOW = datetime(2026, 11, 6, 23, 0, tzinfo=config.TZ)


def make_v1_db(path: Path) -> None:
    """База в том виде, в каком её создаёт текущая версия бота (до миграции)."""
    conn = sqlite3.connect(path)
    conn.executescript(storage.BASE_SCHEMA)
    start = NOW - timedelta(days=13)
    conn.execute("INSERT INTO participants VALUES (?, 'Тест', NULL, ?, 1)",
                 (OWNER, start.replace(hour=9).isoformat()))
    for i in range(14):
        day = (start + timedelta(days=i)).date()
        for j, hour in enumerate((10, 16, 22)):
            if i % 4 == 1 and j == 1:   # пропуск днём
                continue
            if i == 6:                  # целый день без отметок
                continue
            at = datetime(day.year, day.month, day.day, hour, 5, tzinfo=config.TZ)
            conn.execute(
                """INSERT INTO checkins(chat_id, started_at, completed_at, period, mood, energy,
                   anxiety, sleep_hours, meds, side_effects, note, safety)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yes', '', ?, 'not_asked')""",
                (OWNER, at.isoformat(), (at + timedelta(minutes=2)).isoformat(),
                 ("утро", "день", "вечер")[j], 3 + (i + j) % 6, 4 + i % 5, 2 + j,
                 7.5 if j == 0 else None, "тестовая заметка" if j == 2 else ""),
            )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    path = tmp_path / "mood.sqlite3"
    make_v1_db(path)
    monkeypatch.setattr(storage, "DB_PATH", path)
    return path


def test_migration_backs_up_and_keeps_rows(fresh_db):
    before = sqlite3.connect(fresh_db).execute("SELECT * FROM checkins ORDER BY id").fetchall()
    backup = storage.migrate()
    assert backup is not None and backup.exists()
    after = sqlite3.connect(fresh_db).execute("SELECT * FROM checkins ORDER BY id").fetchall()
    assert before == after
    copy = sqlite3.connect(backup).execute("SELECT * FROM checkins ORDER BY id").fetchall()
    assert copy == before
    assert storage.migrate() is None  # повторный запуск ничего не копирует


def test_gaps_are_gaps_not_zeros():
    storage.migrate()
    days = stats.load_days(OWNER, NOW.date() - timedelta(days=13), NOW.date(), NOW)
    empty = days[6]
    assert empty.checkins == [] and empty.count("missed") == 3
    partial = days[1]
    assert partial.count("filled") == 2 and partial.count("missed") == 1
    s = stats.summarize(days)
    assert s.missed == 3 + sum(1 for i in range(14) if i % 4 == 1 and i != 6)
    assert s.metrics["mood"].lo >= 3  # ноль за пропуск не подставлен
    assert s.metrics["mood"].n == s.checkins


def test_future_and_paused_slots_not_counted_as_missed():
    storage.migrate()
    with storage.db() as conn:
        storage.log_event(conn, OWNER, "pause", (NOW - timedelta(days=2, hours=13)).isoformat())
        storage.log_event(conn, OWNER, "resume", (NOW - timedelta(days=1, hours=13)).isoformat())
        conn.execute("DELETE FROM checkins WHERE started_at LIKE ?",
                     ((NOW - timedelta(days=2)).date().isoformat() + "%",))
    morning = NOW.replace(hour=12)
    today = stats.load_days(OWNER, NOW.date(), NOW.date(), morning)[0]
    assert today.slots[1].status == "upcoming"  # 16:00 ещё не наступило — это не пропуск
    paused_day = stats.load_days(OWNER, NOW.date() - timedelta(days=2), NOW.date() - timedelta(days=2), NOW)[0]
    assert paused_day.count("paused") == 3 and paused_day.count("missed") == 0


def test_sharing_requires_owner_confirmation_and_can_be_revoked():
    storage.migrate()
    assert access.resolve_target(VIEWER) is None
    code = access.create_invite(OWNER)
    assert access.accept_invite(code, OWNER, "сама") is None          # себе нельзя
    assert access.accept_invite(code, VIEWER, "Тестовый") == OWNER
    assert access.accept_invite(code, STRANGER, "Чужой") is None      # код одноразовый
    assert access.resolve_target(VIEWER) is None                      # до подтверждения
    access.set_enabled(OWNER, True)
    t = access.resolve_target(VIEWER)
    assert t and t.owner_chat_id == OWNER and t.role == "viewer" and not t.show_private
    access.set_share_notes(OWNER, True)
    assert access.resolve_target(VIEWER).show_private
    access.set_enabled(OWNER, False)
    assert access.resolve_target(VIEWER) is None
    access.set_enabled(OWNER, True)
    access.remove_viewer(OWNER)
    assert access.resolve_target(VIEWER) is None
    assert access.resolve_target(STRANGER) is None


def test_expired_invite_rejected():
    storage.migrate()
    code = access.create_invite(OWNER)
    with storage.db() as conn:
        conn.execute("UPDATE sharing SET invite_expires_at=? WHERE owner_chat_id=?",
                     ((datetime.now(config.TZ) - timedelta(minutes=1)).isoformat(), OWNER))
    assert access.accept_invite(code, VIEWER, "Тест") is None


def test_viewer_does_not_see_notes():
    storage.migrate()
    days = stats.load_days(OWNER, NOW.date() - timedelta(days=13), NOW.date(), NOW)
    hidden = dashboard.notes_text(access.Target(OWNER, "viewer", False), days)
    shown = dashboard.notes_text(access.Target(OWNER, "owner", True), days)
    assert "тестовая заметка" not in hidden and "тестовая заметка" in shown


def test_charts_pdf_and_xlsx(tmp_path):
    storage.migrate()
    days = stats.load_days(OWNER, NOW.date() - timedelta(days=13), NOW.date(), NOW)
    assert charts.scales_png(days, "t", stats.METRICS).startswith(b"\x89PNG")
    assert charts.sleep_png(days, "t").startswith(b"\x89PNG")
    empty = stats.load_days(OWNER, NOW.date() - timedelta(days=60), NOW.date() - timedelta(days=50), NOW)
    assert charts.scales_png(empty, "t").startswith(b"\x89PNG")
    pdf = doctor.build_pdf(OWNER, 30, True, NOW)
    assert pdf.startswith(b"%PDF")
    out = tmp_path / "r.xlsx"
    bot.create_xlsx(OWNER, out, show_private=False)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Наблюдения", "По дням"]
    values = [c.value for row in wb["По дням"].iter_rows() for c in row]
    assert "нет отметок" in values
    assert "тестовая заметка" not in [c.value for row in wb["Наблюдения"].iter_rows() for c in row]


FORBIDDEN = ("эпизод", "прогноз", "предсказ", "мани", "депресс", "гипомани", "диагноз:", "фаза")


def test_texts_state_only_facts():
    storage.migrate()
    days = stats.load_days(OWNER, NOW.date() - timedelta(days=29), NOW.date(), NOW)
    s = stats.summarize(days)
    texts = [dashboard.period_text(days, "30 дней"), dashboard.daily_ranges_text(days),
             "\n".join(doctor.summary_lines(s, days, True))]
    for text in texts:
        low = text.lower()
        for word in FORBIDDEN:
            assert word not in low, (word, text)


def test_token_redacted_in_logs(caplog):
    record = logging.LogRecord("x", logging.INFO, "", 0, "url https://api/bot%s/getMe", ("123:SECRET",), None)
    bot.RedactSecrets("123:SECRET").filter(record)
    assert "SECRET" not in record.getMessage()
    assert logging.getLogger("httpx").level >= logging.WARNING


def test_application_builds_without_network():
    app = bot.build_application("123456:TEST_TOKEN_NOT_REAL")
    assert app.handlers
