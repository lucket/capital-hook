"""Market opening-hours helpers.

Pure functions over the weekly `hours` mapping returned by
:meth:`CapitalClient.get_opening_hours`, e.g. ``{"mon": ["00:00 - 21:55"], "sat": []}``.
Times are UTC; `now` is a naive UTC datetime (defaults to the current time).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _at(time_str: str, base: datetime) -> datetime:
    """`base` moved to the given HH:MM or HH:MM:SS."""
    fmt = "%H:%M:%S" if time_str.count(":") == 2 else "%H:%M"
    parsed = datetime.strptime(time_str, fmt).time()
    return base.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second, microsecond=0)


def _range(rng: str, base: datetime) -> tuple[datetime, datetime]:
    start_s, end_s = rng.split(" - ")
    start, end = _at(start_s, base), _at(end_s, base)
    if end < start:                       # session runs past midnight
        end += timedelta(days=1)
    return start, end


def is_market_closed(hours: dict, *, minutes: float = 2, now: datetime | None = None) -> bool:
    """True when the market is closed now, or closes within `minutes`."""
    now = now or _now_utc()
    day_hours = hours.get(now.strftime("%a").lower(), [])
    if not day_hours:
        return True                       # never opens today
    for rng in day_hours:
        start, end = _range(rng, now)
        if start <= now < end:
            return (end - now).total_seconds() / 60 <= minutes
    return True                           # between sessions


def is_closing_for_day(hours: dict, *, minutes: float = 2, now: datetime | None = None) -> bool:
    """True when today's *last* session is over, or ends within `minutes`."""
    now = now or _now_utc()
    day_hours = hours.get(now.strftime("%a").lower(), [])
    if not day_hours:
        return True                       # closed the whole day
    start, end = _range(day_hours[-1], now)
    if start <= now < end:
        return (end - now).total_seconds() / 60 <= minutes
    return now >= end                     # past the final session; before it starts the market is still "open"


def is_closing_for_week(hours: dict, *, minutes: float = 2, now: datetime | None = None) -> bool:
    """True during the last `minutes` of the last trading session of the week."""
    last_day, last_range = None, None
    for day in reversed(_WEEKDAYS):
        if hours.get(day):
            last_day, last_range = day, hours[day][-1]
            break
    if not last_range:
        return False

    now = now or _now_utc()
    today = now.strftime("%a").lower()
    if today != last_day:
        return False
    ref = now + timedelta(days=(_WEEKDAYS.index(last_day) - _WEEKDAYS.index(today)) % 7)
    start, end = _range(last_range, ref)
    if start <= now < end:
        return (end - now).total_seconds() / 60 <= minutes
    return False
