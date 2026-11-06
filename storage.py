"""Подключение к SQLite, схема, миграция с резервной копией и журнал событий.

Миграция только ДОБАВЛЯЕТ таблицы. Существующие строки в participants и checkins
не изменяются и не удаляются. Перед миграцией делается копия файла базы.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "data/mood.sqlite3"))
SCHEMA_VERSION = 2

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    chat_id INTEGER PRIMARY KEY,
    first_name TEXT,
    username TEXT,
    consented_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    period TEXT NOT NULL,
    mood INTEGER NOT NULL,
    energy INTEGER NOT NULL,
    anxiety INTEGER NOT NULL,
    sleep_hours REAL,
    meds TEXT NOT NULL,
    side_effects TEXT,
    note TEXT,
    safety TEXT,
    FOREIGN KEY(chat_id) REFERENCES participants(chat_id)
);
CREATE INDEX IF NOT EXISTS idx_checkins_chat_time
ON checkins(chat_id, completed_at);
"""

MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Контекст дня (дорога, болезнь, конфликт, нагрузка) и заметки вне опроса.
CREATE TABLE IF NOT EXISTS day_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('context', 'note')),
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    removed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_day_entries_chat_day ON day_entries(chat_id, day);

-- Доступ второго человека. Включает и выключает только сама участница.
CREATE TABLE IF NOT EXISTS sharing (
    owner_chat_id INTEGER PRIMARY KEY,
    viewer_chat_id INTEGER,
    viewer_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    share_notes INTEGER NOT NULL DEFAULT 0,
    invite_code TEXT,
    invite_expires_at TEXT,
    updated_at TEXT NOT NULL
);

-- Журнал: отправленные напоминания, пауза/возобновление, изменения доступа.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_chat_at ON events(chat_id, at);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    """Соединение с автоматическим commit/rollback и закрытием."""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def current_version(conn: sqlite3.Connection) -> int:
    if _table_exists(conn, "schema_meta"):
        row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if row:
            return int(row["value"])
    if _table_exists(conn, "checkins"):
        return 1
    return 0


def backup_database(reason: str) -> Path:
    """Копия базы через SQLite backup API (безопасно при работающем боте)."""
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"mood-{stamp}-{reason}.sqlite3"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        for table in ("participants", "checkins"):
            if _table_exists(src, table):
                a = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                b = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if a != b:
                    raise RuntimeError(f"Резервная копия не совпадает по таблице {table}")
    finally:
        dst.close()
        src.close()
    LOG.info("Резервная копия базы создана: %s", target.name)
    return target


def migrate() -> Path | None:
    """Создаёт схему. Если база уже есть и схема старая — сначала делает копию."""
    had_data = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    with db() as conn:
        version = current_version(conn)
    backup = None
    if had_data and version < SCHEMA_VERSION:
        backup = backup_database(f"v{version}-to-v{SCHEMA_VERSION}")
    with db() as conn:
        conn.executescript(BASE_SCHEMA)
        if version < 2:
            conn.executescript(MIGRATION_2)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
    if version < SCHEMA_VERSION:
        LOG.info("Схема базы обновлена: v%s -> v%s", version, SCHEMA_VERSION)
    return backup


def log_event(conn: sqlite3.Connection, chat_id: int, kind: str, at: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events(chat_id, kind, detail, at) VALUES (?, ?, ?, ?)",
        (chat_id, kind, detail, at),
    )
