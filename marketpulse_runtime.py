import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_TZ = os.environ.get("MARKETPULSE_TZ", "Asia/Kolkata")
DEFAULT_LOG_DIR = Path(os.environ.get("MARKETPULSE_LOG_DIR", os.environ.get("LOG_DIR", "logs")))
DEFAULT_STATE_DIR = Path(os.environ.get("MARKETPULSE_STATE_DIR", "briefings"))
DEFAULT_REPORT_DIR = Path(os.environ.get("MARKETPULSE_REPORT_DIR", str(DEFAULT_STATE_DIR.parent / "reports")))


def market_tz(tz_name: str | None = None):
    return ZoneInfo(tz_name or DEFAULT_TZ)


def now_market(tz_name: str | None = None) -> dt.datetime:
    return dt.datetime.now(market_tz(tz_name))


def market_date(now: dt.datetime | None = None, tz_name: str | None = None) -> dt.date:
    current = now or now_market(tz_name)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(market_tz(tz_name)).date()


def market_close_countdown(
    now: dt.datetime | None = None,
    close_time: dt.time = dt.time(15, 30),
    tz_name: str | None = None,
) -> str:
    try:
        tz = market_tz(tz_name)
        current = now or dt.datetime.now(tz)
        if current.tzinfo is None:
            current = current.replace(tzinfo=tz)
        current = current.astimezone(tz)
        close_dt = current.replace(
            hour=close_time.hour,
            minute=close_time.minute,
            second=0,
            microsecond=0,
        )
        mins_left = int((close_dt - current).total_seconds() / 60)
        if mins_left <= 0:
            return "already closed"
        if mins_left >= 60:
            return f"~{mins_left / 60:.1f} hrs"
        return f"~{mins_left} min"
    except Exception:
        return "time unavailable"


def resolve_log_dir(value: str | os.PathLike | None = None) -> Path:
    return Path(value) if value else DEFAULT_LOG_DIR


def resolve_state_dir(value: str | os.PathLike | None = None) -> Path:
    return Path(value) if value else DEFAULT_STATE_DIR


def resolve_report_dir(value: str | os.PathLike | None = None) -> Path:
    return Path(value) if value else DEFAULT_REPORT_DIR
