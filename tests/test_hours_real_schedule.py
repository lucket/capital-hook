"""Opening hours exactly as the DEMO API publishes them for BTCUSD (observed): two sessions a day, the second ending at 00:00."""
from datetime import datetime

import pytest

from capital_hook.hours import is_closing_for_day, is_closing_for_week, is_market_closed

DAY = ["00:00 - 21:00", "21:05 - 00:00"]
BTCUSD = {d: list(DAY) for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
TUESDAY = datetime(2024, 1, 2)             # a Tuesday


def at(hour, minute=0):
    return TUESDAY.replace(hour=hour, minute=minute)


@pytest.mark.parametrize("hour, minute, closed", [(0, 1, False), (12, 0, False), (20, 57, False), (20, 59, True), (21, 2, True),
                                                   (21, 4, True), (21, 10, False), (23, 30, False), (23, 59, True)])
def test_is_market_closed_around_the_sessions_and_the_gap(hour, minute, closed):
    assert is_market_closed(BTCUSD, now=at(hour, minute)) is closed       # "closed" includes the last 2 minutes before a session ends


def test_a_session_ending_at_midnight_runs_to_the_end_of_the_day():
    assert not is_market_closed(BTCUSD, now=at(22, 0)) and not is_market_closed(BTCUSD, now=at(23, 55))


def test_is_closing_for_day_only_fires_at_the_end_of_the_last_session():
    assert not is_closing_for_day(BTCUSD, now=at(20, 59))                 # first session ends, a second one follows
    assert not is_closing_for_day(BTCUSD, now=at(23, 30))
    assert is_closing_for_day(BTCUSD, now=at(23, 59))


def test_is_closing_for_week_uses_a_sunday_to_saturday_week():
    """For a market open all seven days the last session of the week is Saturday's (the week is modelled Sunday first, as for FX)."""
    saturday = datetime(2024, 1, 6)
    assert is_closing_for_week(BTCUSD, now=saturday.replace(hour=23, minute=59))
    assert not is_closing_for_week(BTCUSD, now=saturday.replace(hour=12))
    assert not is_closing_for_week(BTCUSD, now=datetime(2024, 1, 7, 23, 59))      # Sunday: a trading day, not the end of the week
    assert not is_closing_for_week(BTCUSD, now=at(23, 59))


class Localized(datetime):
    """A datetime whose %a is not English, as on a machine with an Italian / French locale."""

    NAMES = {"Mon": "lun", "Tue": "mar", "Wed": "mer", "Thu": "gio", "Fri": "ven", "Sat": "sab", "Sun": "dom"}

    def strftime(self, fmt):
        out = super().strftime(fmt)
        return self.NAMES.get(out, out) if fmt == "%a" else out


def test_the_weekday_does_not_depend_on_the_machines_locale():
    tuesday_noon = Localized(2024, 1, 2, 12, 0)
    assert tuesday_noon.strftime("%a") == "mar"                       # what an Italian machine would give
    assert not is_market_closed(BTCUSD, now=tuesday_noon)
    assert not is_closing_for_day(BTCUSD, now=tuesday_noon)
    assert is_closing_for_week(BTCUSD, now=Localized(2024, 1, 6, 23, 59))
    only_tuesday = {"tue": ["09:00 - 17:00"]}
    assert not is_market_closed(only_tuesday, now=Localized(2024, 1, 2, 10, 0)) and is_market_closed(only_tuesday, now=Localized(2024, 1, 3, 10, 0))
