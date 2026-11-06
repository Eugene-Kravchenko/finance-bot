"""Сбор фактов из записей: дни, слоты расписания, пропуски, простые сводки.

Принципы:
- пропуск — это отсутствие записи, а не ноль; в средних он не участвует;
- считаются только описательные величины (число записей, среднее, минимум, максимум);
- никаких выводов о состоянии, эпизодах или прогнозов.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import config
import storage

METRICS = ("mood", "energy", "anxiety")
METRIC_LABELS = {"mood": "Настроение", "energy": "Энергия", "anxiety": "Тревога"}


@dataclass
class Checkin:
    at: datetime
    period: str
    mood: int | None
    energy: int | None
    anxiety: int | None
    sleep_hours: float | None
    meds: str | None
    side_effects: str
    note: str
    safety: str


@dataclass
class Slot:
    at: datetime
    status: str  # filled | missed | paused | upcoming | before_start
    checkins: list[Checkin] = field(default_factory=list)


@dataclass
class Day:
    day: date
    slots: list[Slot]
    checkins: list[Checkin]
    context: list[str]
    notes: list[tuple[datetime, str]]

    def count(self, status: str) -> int:
        return sum(1 for s in self.slots if s.status == status)

    @property
    def expected(self) -> int:
        return self.count("filled") + self.count("missed")

    def values(self, metric: str) -> list[float]:
        return [getattr(c, metric) for c in self.checkins if getattr(c, metric) is not None]

    @property
    def sleep(self) -> float | None:
        """Сон за день: ответ утреннего опроса, иначе первый ответ за день."""
        with_sleep = [c for c in self.checkins if c.sleep_hours is not None]
        if not with_sleep:
            return None
        morning = [c for c in with_sleep if c.period == "утро"]
        return (morning or with_sleep)[0].sleep_hours


@dataclass
class Stat:
    n: int
    mean: float | None
    lo: float | None
    hi: float | None


def describe(values: list[float]) -> Stat:
    if not values:
        return Stat(0, None, None, None)
    return Stat(len(values), sum(values) / len(values), min(values), max(values))


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt.astimezone(config.TZ)


def slot_index(moment: time, times: list[time]) -> int:
    """Номер слота: границы — середины между соседними временами опросов."""
    minutes = moment.hour * 60 + moment.minute
    marks = [t.hour * 60 + t.minute for t in times]
    bounds = [(marks[i] + marks[i + 1]) / 2 for i in range(len(marks) - 1)]
    return sum(1 for b in bounds if minutes >= b)


def _pause_intervals(events: list[tuple[str, datetime]]) -> list[tuple[datetime, datetime | None]]:
    intervals: list[tuple[datetime, datetime | None]] = []
    start = None
    for kind, at in events:
        if kind == "pause" and start is None:
            start = at
        elif kind == "resume" and start is not None:
            intervals.append((start, at))
            start = None
    if start is not None:
        intervals.append((start, None))
    return intervals


def _in_pause(at: datetime, intervals) -> bool:
    return any(s <= at and (e is None or at < e) for s, e in intervals)


def load_days(chat_id: int, start: date, end: date, now: datetime | None = None) -> list[Day]:
    """Дни с start по end включительно (в часовом поясе бота)."""
    now = (now or datetime.now(config.TZ)).astimezone(config.TZ)
    times = config.slot_times()
    with storage.db() as conn:
        part = conn.execute(
            "SELECT consented_at FROM participants WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM checkins WHERE chat_id = ? ORDER BY started_at", (chat_id,)
        ).fetchall()
        ev_rows = conn.execute(
            "SELECT kind, at FROM events WHERE chat_id = ? AND kind IN ('pause','resume') ORDER BY at",
            (chat_id,),
        ).fetchall()
        entry_rows = conn.execute(
            """SELECT day, kind, value, created_at FROM day_entries
               WHERE chat_id = ? AND removed_at IS NULL AND day BETWEEN ? AND ?
               ORDER BY created_at""",
            (chat_id, start.isoformat(), end.isoformat()),
        ).fetchall()

    checkins = [
        Checkin(
            at=_parse(r["started_at"]), period=r["period"], mood=r["mood"],
            energy=r["energy"], anxiety=r["anxiety"], sleep_hours=r["sleep_hours"],
            meds=r["meds"], side_effects=(r["side_effects"] or "").strip(),
            note=(r["note"] or "").strip(), safety=r["safety"] or "",
        )
        for r in rows
    ]
    starts = [c.at for c in checkins]
    if part:
        starts.append(_parse(part["consented_at"]))
    tracking_start = min(starts) if starts else None
    pauses = _pause_intervals([(r["kind"], _parse(r["at"])) for r in ev_rows])

    days = []
    current = start
    while current <= end:
        day_checkins = [c for c in checkins if c.at.date() == current]
        slots = []
        for i, t in enumerate(times):
            at = datetime.combine(current, t, tzinfo=config.TZ)
            assigned = [c for c in day_checkins if slot_index(c.at.time(), times) == i]
            if assigned:
                status = "filled"
            elif at > now:
                status = "upcoming"
            elif tracking_start is None or at < tracking_start:
                status = "before_start"
            elif _in_pause(at, pauses):
                status = "paused"
            else:
                status = "missed"
            slots.append(Slot(at, status, assigned))
        context, notes = [], []
        for r in entry_rows:
            if r["day"] != current.isoformat():
                continue
            if r["kind"] == "context" and r["value"] not in context:
                context.append(r["value"])
            elif r["kind"] == "note":
                notes.append((_parse(r["created_at"]), r["value"]))
        days.append(Day(current, slots, day_checkins, context, notes))
        current += timedelta(days=1)
    return days


def last_days(chat_id: int, n: int, now: datetime | None = None) -> list[Day]:
    now = (now or datetime.now(config.TZ)).astimezone(config.TZ)
    end = now.date()
    return load_days(chat_id, end - timedelta(days=n - 1), end, now)


@dataclass
class Summary:
    days: int
    checkins: int
    expected: int
    filled: int
    missed: int
    paused: int
    metrics: dict[str, Stat]
    sleep: Stat
    sleep_days: int
    meds: Counter
    context: Counter
    safety_yes: list[datetime]


def summarize(days: list[Day]) -> Summary:
    all_checkins = [c for d in days for c in d.checkins]
    sleeps = [d.sleep for d in days if d.sleep is not None]
    return Summary(
        days=len(days),
        checkins=len(all_checkins),
        expected=sum(d.expected for d in days),
        filled=sum(d.count("filled") for d in days),
        missed=sum(d.count("missed") for d in days),
        paused=sum(d.count("paused") for d in days),
        metrics={m: describe([v for d in days for v in d.values(m)]) for m in METRICS},
        sleep=describe(sleeps),
        sleep_days=len(sleeps),
        meds=Counter(c.meds for c in all_checkins if c.meds),
        context=Counter(tag for d in days for tag in d.context),
        safety_yes=[c.at for c in all_checkins if c.safety == "yes"],
    )


def fmt_num(value: float | None) -> str:
    if value is None:
        return "—"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def fmt_stat(stat: Stat, unit: str = "") -> str:
    if stat.n == 0:
        return "нет записей"
    if stat.lo == stat.hi:
        return f"{fmt_num(stat.mean)}{unit} (записей: {stat.n})"
    return (
        f"в среднем {stat.mean:.1f}{unit}, от {fmt_num(stat.lo)} до {fmt_num(stat.hi)} "
        f"(записей: {stat.n})"
    )


def fmt_range(values: list[float]) -> str:
    if not values:
        return "—"
    lo, hi = min(values), max(values)
    return fmt_num(lo) if lo == hi else f"{fmt_num(lo)}–{fmt_num(hi)}"
