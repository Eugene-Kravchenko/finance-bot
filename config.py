"""Общие настройки бота. Все значения берутся из переменных окружения."""
import os
from datetime import time
from zoneinfo import ZoneInfo

TZ_NAME = os.getenv("TZ", "Europe/Moscow")
TZ = ZoneInfo(TZ_NAME)

CHECKIN_TIMES = [
    item.strip() for item in os.getenv("CHECKIN_TIMES", "10:00,16:00,22:00").split(",")
    if item.strip()
]


def slot_times() -> list[time]:
    result = []
    for item in CHECKIN_TIMES:
        hour, minute = map(int, item.split(":"))
        result.append(time(hour, minute))
    return sorted(result)


DISCLAIMER = (
    "Здесь только записи из дневника. Это не диагноз и не медицинское заключение."
)

MEDS_LABELS = {"yes": "да", "partial": "частично", "no": "нет", "na": "не применимо"}

CONTEXT_TAGS = {
    "road": "дорога",
    "illness": "болезнь",
    "conflict": "конфликт",
    "load": "нагрузка",
}
