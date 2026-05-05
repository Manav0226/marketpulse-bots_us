from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> dt.date:
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (occurrence - 1) * 7
    return dt.date(year, month, day)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        cursor = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        cursor = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= dt.timedelta(days=1)
    return cursor


def _observe_fixed_holiday(day: dt.date) -> dt.date:
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def us_market_holidays(year: int) -> set[dt.date]:
    return {
        _observe_fixed_holiday(dt.date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _observe_fixed_holiday(dt.date(year, 6, 19)),
        _observe_fixed_holiday(dt.date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observe_fixed_holiday(dt.date(year, 12, 25)),
    }


def is_us_trading_day(now: dt.datetime | None = None) -> bool:
    current = (now or dt.datetime.now(UTC)).astimezone(ET)
    return current.weekday() < 5 and current.date() not in us_market_holidays(current.year)


def is_us_market_open(now: dt.datetime | None = None) -> bool:
    current = (now or dt.datetime.now(UTC)).astimezone(ET)
    if not is_us_trading_day(current):
        return False
    start = current.replace(hour=9, minute=30, second=0, microsecond=0)
    end = current.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= current <= end


def market_window_label(now: dt.datetime | None = None) -> str:
    current = (now or dt.datetime.now(UTC)).astimezone(ET)
    if not is_us_trading_day(current):
        return "closed"
    if current.hour < 9 or (current.hour == 9 and current.minute < 30):
        return "premarket"
    if current.hour < 12:
        return "open"
    if current.hour < 15:
        return "midday"
    if current.hour < 16:
        return "eod"
    return "closed"
