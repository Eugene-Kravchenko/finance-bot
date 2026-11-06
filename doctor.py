"""PDF-сводка для приёма у врача: только факты из записей, без интерпретаций."""
import io
import textwrap
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

import charts  # noqa: E402
import config  # noqa: E402
import stats  # noqa: E402

A4 = (8.27, 11.69)
ROWS_PER_PAGE = 32


def _lines_page(pdf, title: str, lines: list[str]) -> None:
    per_page = 60
    for start in range(0, max(len(lines), 1), per_page):
        fig = plt.figure(figsize=A4)
        fig.text(0.07, 0.95, title, fontsize=13, weight="bold")
        y = 0.92
        for line in lines[start:start + per_page]:
            fig.text(0.07, y, line, fontsize=8.5, va="top")
            y -= 0.0145
        pdf.savefig(fig)
        plt.close(fig)


def summary_lines(s: stats.Summary, days: list[stats.Day], show_private: bool) -> list[str]:
    lines = [
        f"Период: {days[0].day:%d.%m.%Y} – {days[-1].day:%d.%m.%Y} ({s.days} дн.)",
        f"Расписание опросов: {', '.join(config.CHECKIN_TIMES)} ({config.TZ_NAME})",
        "",
        f"Отметок по расписанию ожидалось: {s.expected}; получено: {s.filled}; "
        f"пропущено: {s.missed}" + (f"; на паузе: {s.paused}" if s.paused else ""),
        f"Всего отметок, включая дополнительные: {s.checkins}",
        "Пропуски в средних не учитываются и нулями не считаются.",
        "",
    ]
    for m in stats.METRICS:
        lines.append(f"{stats.METRIC_LABELS[m]} (1–10): {stats.fmt_stat(s.metrics[m])}")
    lines.append(f"Сон: {stats.fmt_stat(s.sleep, ' ч')}; дней с данными о сне: {s.sleep_days} из {s.days}")
    meds = ", ".join(f"{config.MEDS_LABELS.get(k, k)} — {v}" for k, v in s.meds.items()) or "нет записей"
    lines.append(f"Лекарства по назначению (ответы): {meds}")
    ctx = ", ".join(f"{config.CONTEXT_TAGS.get(k, k)} — {v} дн." for k, v in s.context.items()) or "не отмечен"
    lines.append(f"Контекст дня: {ctx}")
    if show_private and s.safety_yes:
        dates = ", ".join(f"{d:%d.%m %H:%M}" for d in s.safety_yes)
        lines.append(f"Ответ «да» на вопрос о безопасности: {len(s.safety_yes)} ({dates})")
    return lines


def day_rows(days: list[stats.Day]) -> list[list[str]]:
    rows = []
    for d in days:
        due = d.expected
        if due == 0 and not d.checkins:
            marks = "пауза" if d.count("paused") else "—"
        else:
            marks = f"{d.count('filled')} из {due}"
        rows.append([
            f"{d.day:%d.%m}",
            marks,
            stats.fmt_range(d.values("mood")),
            stats.fmt_range(d.values("energy")),
            stats.fmt_range(d.values("anxiety")),
            stats.fmt_num(d.sleep) if d.sleep is not None else "нет данных",
            ", ".join(config.CONTEXT_TAGS.get(t, t) for t in d.context),
        ])
    return rows


def note_lines(days: list[stats.Day]) -> list[str]:
    lines = []
    for d in days:
        items = []
        for c in d.checkins:
            if c.note:
                items.append((c.at, "заметка", c.note))
            if c.side_effects:
                items.append((c.at, "побочные эффекты", c.side_effects))
        items += [(at, "заметка", text) for at, text in d.notes]
        for at, kind, text in sorted(items, key=lambda x: x[0]):
            wrapped = textwrap.wrap(f"{at:%d.%m %H:%M} — {kind}: {text}", 105)
            lines.append(wrapped[0])
            lines += ["      " + w for w in wrapped[1:]]
    return lines or ["Заметок за период нет."]


def build_pdf(owner_chat_id: int, n_days: int, show_private: bool, now: datetime | None = None) -> bytes:
    now = (now or datetime.now(config.TZ)).astimezone(config.TZ)
    days = stats.load_days(owner_chat_id, now.date() - timedelta(days=n_days - 1), now.date(), now)
    s = stats.summarize(days)
    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        fig = plt.figure(figsize=A4)
        fig.text(0.07, 0.955, "Сводка дневника самочувствия", fontsize=15, weight="bold")
        fig.text(0.07, 0.935, f"Сформировано {now:%d.%m.%Y %H:%M}. {config.DISCLAIMER}",
                 fontsize=8, color="#555555")
        y = 0.905
        for line in summary_lines(s, days, show_private):
            for part in textwrap.wrap(line, 110) or [""]:
                fig.text(0.07, y, part, fontsize=9, va="top")
                y -= 0.017
        ax1 = fig.add_axes([0.08, 0.33, 0.86, 0.2])
        ax1.set_title("Настроение, энергия, тревога (каждая отметка; пунктир — граница дня)",
                      fontsize=9, loc="left", pad=20)
        charts.draw_scales(ax1, days, metrics=stats.METRICS)
        ax2 = fig.add_axes([0.08, 0.06, 0.86, 0.17])
        ax2.set_title("Сон по дням", fontsize=9, loc="left", pad=20)
        charts.draw_sleep(ax2, days)
        pdf.savefig(fig)
        plt.close(fig)

        header = ["Дата", "Отметок", "Настроение", "Энергия", "Тревога", "Сон, ч", "Контекст"]
        rows = day_rows(days)
        for start in range(0, len(rows), ROWS_PER_PAGE):
            fig = plt.figure(figsize=A4)
            fig.text(0.07, 0.955, "По дням (диапазон за день: мин–макс)", fontsize=12, weight="bold")
            ax = fig.add_axes([0.05, 0.05, 0.9, 0.87])
            ax.axis("off")
            chunk = rows[start:start + ROWS_PER_PAGE]
            table = ax.table(cellText=chunk, colLabels=header, loc="upper center",
                             colWidths=[0.08, 0.1, 0.13, 0.11, 0.11, 0.12, 0.3], cellLoc="left")
            table.auto_set_font_size(False)
            table.set_fontsize(8)
            table.scale(1, 1.35)
            for (r, _), cell in table.get_celld().items():
                cell.set_edgecolor("#DDDDDD")
                if r == 0:
                    cell.set_facecolor("#EEF3F8")
            pdf.savefig(fig)
            plt.close(fig)

        if show_private:
            _lines_page(pdf, "Заметки и побочные эффекты", note_lines(days))
    return buf.getvalue()
