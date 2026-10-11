from datetime import datetime

import pytest

from capital_hook import TradeInstrument, is_closing_for_day, is_closing_for_week, is_market_closed

HOURS = {
    "mon": ["00:00 - 21:55"], "tue": ["00:00 - 21:55"], "wed": ["00:00 - 21:55"], "thu": ["00:00 - 21:55"],
    "fri": ["00:00 - 21:55"], "sat": [], "sun": ["23:00 - 23:59"],
}
SPLIT = {"mon": ["00:00 - 12:00", "13:00 - 21:00"], "sat": [], "sun": ["22:00 - 05:00"]}


def at(day_of_july_2026, hh, mm):          # 2026-07-13 is a Monday
    return datetime(2026, 7, day_of_july_2026, hh, mm)


def test_closed_all_day():
    assert is_market_closed(HOURS, now=at(18, 12, 0))                 # Saturday
    assert is_closing_for_day(HOURS, now=at(18, 12, 0))


def test_open_midday_is_not_closing():
    now = at(15, 12, 0)                                               # Wednesday noon
    assert not is_market_closed(HOURS, now=now) and not is_closing_for_day(HOURS, now=now)


def test_final_minutes_of_the_session_count_as_closing():
    assert is_closing_for_day(HOURS, now=at(15, 21, 54))
    assert not is_closing_for_day(HOURS, now=at(15, 21, 40))
    assert is_closing_for_day(HOURS, minutes=30, now=at(15, 21, 40))
    assert is_closing_for_day(HOURS, now=at(15, 22, 30))             # after the last session


def test_gap_between_split_sessions_is_closed_but_not_end_of_day():
    lunch = at(13, 12, 30)                                            # Monday between sessions
    assert is_market_closed(SPLIT, now=lunch)
    assert not is_closing_for_day(SPLIT, now=lunch)                   # a later session still comes
    assert is_closing_for_day(SPLIT, now=at(13, 20, 59))


def test_session_running_past_midnight():
    assert not is_market_closed(SPLIT, now=at(19, 23, 0))             # Sunday 23:00 inside 22:00-05:00
    assert not is_closing_for_day(SPLIT, now=at(19, 23, 0))


def test_week_end_only_triggers_in_the_last_minutes_of_the_last_session():
    assert is_closing_for_week(HOURS, now=at(17, 21, 54))             # Friday 21:54: Friday holds the week's last session
    assert not is_closing_for_week(HOURS, now=at(17, 12, 0))
    assert not is_closing_for_week(HOURS, now=at(15, 21, 54))         # Wednesday
    assert not is_closing_for_week({}, now=at(17, 21, 54))


def test_instrument_enum_tolerates_unknown_types():
    assert TradeInstrument("INDICES") is TradeInstrument.INDICES
    assert TradeInstrument("BONDS") is TradeInstrument.UNKNOWN
    assert TradeInstrument.UNKONWN is TradeInstrument.UNKNOWN
