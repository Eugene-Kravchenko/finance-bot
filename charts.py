"""Простые графики по записям. Пропуски не превращаются в нули:
линия в месте пропуска прерывается, а сам пропуск отмечен серым крестиком внизу."""
import io
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

import config  # noqa: E402
from stats import METRIC_LABELS, Day  # noqa: E402

COLORS = {"mood": "#2F6DB5", "energy": "#D9822B", "anxiety": "#8A5BB8"}
GAP_COLOR = "#9AA0A6"
PAUSE_COLOR = "#E8EAED"


def _day_axis(ax, days: list[Day]) -> None:
    start = datetime.combine(days[0].day, datetime.min.time(), tzinfo=config.TZ)
    end = datetime.combine(days[-1].day, datetime.min.time(), tzinfo=config.TZ) + timedelta(days=1)
    ax.set_xlim(start, end)
    # Границы между днями — чтобы были видны колебания внутри дня.
    for d in days[1:]:
        ax.axvline(datetime.combine(d.day, datetime.min.time(), tzinfo=config.TZ),
                   color="#C4C7CC", linewidth=0.8, linestyle="--", zorder=0)
    step = 1 if len(days) <= 10 else (3 if len(days) <= 31 else 7)
    # Подпись даты — в середине дня, между пунктирными границами.
    ax.set_xticks([datetime.combine(d.day, datetime.min.time(), tzinfo=config.TZ) + timedelta(hours=12)
                   for d in days[::-1][::step][::-1]])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m", tz=config.TZ))
    ax.tick_params(axis="x", length=0)
    ax.tick_params(axis="x", labelsize=8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _pauses(ax, days: list[Day]) -> bool:
    shown = False
    for d in days:
        for s in d.slots:
            if s.status == "paused":
                ax.axvspan(s.at - timedelta(hours=2), s.at + timedelta(hours=2),
                           color=PAUSE_COLOR, zorder=0, linewidth=0)
                shown = True
    return shown


def draw_scales(ax, days: list[Day], metrics=("mood", "energy")) -> None:
    has_data = any(d.checkins for d in days)
    _day_axis(ax, days)
    ax.set_ylim(0, 10.5)
    ax.set_yticks(range(1, 11))
    ax.tick_params(axis="y", labelsize=8)
    ax.grid(axis="y", color="#EEEEEE", linewidth=0.6)
    if not has_data:
        ax.text(0.5, 0.5, "Нет записей за этот период", transform=ax.transAxes,
                ha="center", va="center", color=GAP_COLOR)
        return
    for metric in metrics:
        xs, ys = [], []
        for d in days:
            for s in d.slots:
                if s.status == "filled":
                    for c in s.checkins:
                        value = getattr(c, metric)
                        xs.append(c.at)
                        ys.append(float("nan") if value is None else value)
                elif s.status in ("missed", "paused"):
                    xs.append(s.at)
                    ys.append(float("nan"))  # разрыв линии, а не ноль
        ax.plot(xs, ys, marker="o", markersize=3.5, linewidth=1.6,
                color=COLORS[metric], label=METRIC_LABELS[metric])
    gaps = [s.at for d in days for s in d.slots if s.status == "missed"]
    if gaps:
        ax.plot(gaps, [0.4] * len(gaps), linestyle="none", marker="x", markersize=5,
                color=GAP_COLOR, label="пропуск (нет отметки)")
    if _pauses(ax, days):
        ax.fill_between([], [], color=PAUSE_COLOR, label="пауза")
    ax.legend(loc="upper left", fontsize=7, frameon=False, ncol=4,
              bbox_to_anchor=(0, 1.13))


def draw_sleep(ax, days: list[Day]) -> None:
    _day_axis(ax, days)
    ax.set_ylabel("часы", fontsize=8)
    ax.tick_params(axis="y", labelsize=8)
    ax.grid(axis="y", color="#EEEEEE", linewidth=0.6)
    xs, hs, missing = [], [], []
    for d in days:
        noon = datetime.combine(d.day, datetime.min.time(), tzinfo=config.TZ) + timedelta(hours=12)
        if d.sleep is not None:
            xs.append(noon)
            hs.append(d.sleep)
        elif d.count("missed") or d.count("filled"):
            missing.append(noon)
    top = max(hs + [10])
    ax.set_ylim(0, top + 1)
    if xs:
        ax.bar(xs, hs, width=0.7, color="#4C9A8A", label="сон, ч")
    if missing:
        ax.plot(missing, [0.25] * len(missing), linestyle="none", marker="x",
                markersize=5, color=GAP_COLOR, label="нет данных о сне")
    if not xs and not missing:
        ax.text(0.5, 0.5, "Нет записей за этот период", transform=ax.transAxes,
                ha="center", va="center", color=GAP_COLOR)
    else:
        ax.legend(loc="upper left", fontsize=7, frameon=False, ncol=2,
                  bbox_to_anchor=(0, 1.13))


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def scales_png(days: list[Day], title: str, metrics=("mood", "energy")) -> bytes:
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.set_title(title, fontsize=10, loc="left", pad=22)
    draw_scales(ax, days, metrics)
    return _png(fig)


def sleep_png(days: list[Day], title: str) -> bytes:
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.set_title(title, fontsize=10, loc="left", pad=22)
    draw_sleep(ax, days)
    return _png(fig)
