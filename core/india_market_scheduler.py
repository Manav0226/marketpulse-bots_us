from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
UTC = dt.timezone.utc


def is_india_trading_day(now: dt.datetime | None = None) -> bool:
    current = (now or dt.datetime.now(UTC)).astimezone(IST)
    return current.weekday() < 5


def is_india_market_open(now: dt.datetime | None = None) -> bool:
    current = (now or dt.datetime.now(UTC)).astimezone(IST)
    if not is_india_trading_day(current):
        return False
    start = current.replace(hour=9, minute=15, second=0, microsecond=0)
    end = current.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= current <= end


def india_window_label(now: dt.datetime | None = None) -> str:
    current = (now or dt.datetime.now(UTC)).astimezone(IST)
    if not is_india_trading_day(current):
        return "closed"
    if current.hour < 9 or (current.hour == 9 and current.minute < 15):
        return "premarket"
    if current.hour < 12:
        return "open"
    if current.hour < 15 or (current.hour == 15 and current.minute <= 30):
        return "midday"
    return "closed"
