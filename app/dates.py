from __future__ import annotations

from datetime import datetime


def parse_start_date(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_end_date(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0 and value.microsecond == 0:
            return value.replace(hour=23, minute=59, second=59, microsecond=999999)
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0 and parsed.microsecond == 0:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def default_period() -> tuple[datetime, datetime]:
    today = datetime.utcnow()
    start = datetime(today.year, today.month, 1)
    end = parse_end_date(today)
    return start, end
