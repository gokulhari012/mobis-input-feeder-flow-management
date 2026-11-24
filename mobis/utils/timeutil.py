"""Timestamp helpers.

All timestamps are stored in the database as ISO-8601 strings in UTC with
millisecond precision (e.g. ``2026-11-24T08:15:30.123+00:00``).  This keeps
lexicographic ordering identical to chronological ordering and avoids any
ambiguity around daylight saving changes.  Conversion to local time happens
only for display.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional

_date_format = "%Y-%m-%d"
_time_format = "%H:%M:%S"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime (naive values are assumed to be UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return ensure_utc(value).isoformat(timespec="milliseconds")


def from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return ensure_utc(datetime.fromisoformat(value))


def to_local(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return ensure_utc(value).astimezone()


def set_display_formats(date_format: str, time_format: str) -> None:
    """Configure the formats used by :func:`fmt_datetime` and friends."""
    global _date_format, _time_format
    try:
        datetime(2000, 1, 2, 3, 4, 5).strftime(f"{date_format} {time_format}")
    except (ValueError, TypeError):
        return
    _date_format = date_format or "%Y-%m-%d"
    _time_format = time_format or "%H:%M:%S"


def display_formats() -> tuple[str, str]:
    return _date_format, _time_format


def fmt_datetime(value: Optional[datetime]) -> str:
    local = to_local(value)
    return local.strftime(f"{_date_format} {_time_format}") if local else ""


def fmt_date(value: Optional[datetime]) -> str:
    local = to_local(value)
    return local.strftime(_date_format) if local else ""


def fmt_time(value: Optional[datetime]) -> str:
    local = to_local(value)
    return local.strftime(_time_format) if local else ""


def local_day_start_utc(day: date) -> datetime:
    """UTC instant corresponding to local midnight at the start of ``day``."""
    return datetime.combine(day, dtime.min).astimezone().astimezone(timezone.utc)


def local_day_end_utc(day: date) -> datetime:
    """UTC instant corresponding to local midnight at the end of ``day``."""
    return local_day_start_utc(day + timedelta(days=1))


def local_stamp() -> str:
    """Compact local timestamp for file names (``20261124_141530``)."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")
