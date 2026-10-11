"""VIX, as Capital.com publishes it (tests/fixtures/vix.json, recorded on DEMO by tests/integration/test_demo_vix.py).

Facts: "Volatility Index", an INDICES CFD with no expiry (a position can be held for weeks without rolling), hours in UTC:
Sunday opens 22:00; Monday-Thursday 00:00-21:00, a one-hour pause, 22:00-24:00; Friday 00:00-21:00; Saturday closed.
The overnight fee is charged at 21:00 UTC, when the daily pause starts.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from book import Book, coherent

from capital_hook import APIError, TradeInstrument, is_closing_for_day, is_closing_for_week, is_market_closed

VIX = json.loads((Path(__file__).parent / "fixtures" / "vix.json").read_text())
HOURS = VIX["opening_hours"]
SUN, MON, TUE, WED, THU, FRI, SAT = (datetime(2026, 10, 11) + timedelta(days=i) for i in range(7))      # 11 Oct 2026 is a Sunday
SUN2, MON2 = SUN + timedelta(days=7), MON + timedelta(days=7)


def at(day, hhmm, seconds=0):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=hour, minute=minute, second=seconds)


@pytest.fixture
def vix(fake):
    """The fake configured as the VIX market: real type, hours, size rules and price, following its own schedule."""
    rules = VIX["dealing_rules"]
    fake.markets["VIX"] = VIX["type"]
    fake.set_quote("VIX", VIX["price_sample"]["bid"], VIX["price_sample"]["offer"])
    fake.min_deal_size["VIX"] = rules["minDealSize"]["value"]
    fake.max_deal_size["VIX"] = rules["maxDealSize"]["value"]
    fake.hours, fake.follow_opening_hours = HOURS, True
    fake.now = lambda: fake.clock
    fake.clock = at(MON, "22:30")
    return fake


# ---------------------------------------------------------------- what VIX is
def test_vix_identity():
    assert (VIX["epic"], VIX["name"], VIX["symbol"], VIX["type"], VIX["currency"]) == ("VIX", "Volatility Index", "VIX", "INDICES", "USD")
    assert TradeInstrument(VIX["type"]) is TradeInstrument.INDICES
    assert VIX["country"] == "United States of America"


def test_vix_has_no_expiry_so_a_position_never_needs_rolling():
    assert VIX["expiry"] == "-"


def test_vix_is_not_one_of_the_etfs_that_share_its_name():
    assert VIX["epic"] == "VIX" and "VIX" not in VIX["other_epics_matching_search"]
    assert {"VIXM", "UVIX", "UVXY", "SVXY"} & set(VIX["other_epics_matching_search"])         # similar names, different (SHARES) instruments


def test_vix_size_rules_are_whole_contracts_from_1_to_25000():
    rules = VIX["dealing_rules"]
    assert (rules["minDealSize"]["value"], rules["minSizeIncrement"]["value"], rules["maxDealSize"]["value"]) == (1, 1, 25000)
    assert VIX["lot_size"] == 1 and VIX["decimals"] == 2 and rules["minStepDistance"]["value"] == 0.01


def test_vix_margin_is_10_percent_and_trailing_stops_are_not_available():
    assert VIX["margin_factor"] == [10, "PERCENTAGE"] and VIX["dealing_rules"]["trailingStopsPreference"] == "NOT_AVAILABLE"
    assert VIX["streaming_prices"] is True and VIX["guaranteed_stop_allowed"] is True


def test_vix_overnight_fee_is_charged_daily_when_the_pause_starts():
    fee = VIX["overnight_fee"]
    assert fee["swapChargeInterval"] == 1440                                                  # every 24 hours
    charged = datetime.fromtimestamp(fee["swapChargeTimestamp"] / 1000, timezone.utc)
    assert (charged.hour, charged.minute) == (21, 0)                                          # 21:00 UTC = the start of the daily pause
    assert fee["longRate"] == fee["shortRate"] < 0                                            # a cost, whichever direction you hold


async def test_the_fake_serves_vix_exactly_as_captured(vix, client):
    market = await client.get_market("VIX")
    assert market["instrument"]["type"] == "INDICES" and market["instrument"]["openingHours"] == HOURS
    assert (market["dealingRules"]["minDealSize"]["value"], market["dealingRules"]["maxDealSize"]["value"]) == (1, 25000)
    assert await client.get_instrument_types(["VIX"]) == {"VIX": "INDICES"}
    assert await client.get_opening_hours("VIX") == HOURS
    assert await client.get_prices("VIX") == (VIX["price_sample"]["offer"], VIX["price_sample"]["bid"])


# ---------------------------------------------------------------- the schedule, minute by minute
def test_every_trading_day_is_published_in_utc():
    assert HOURS["zone"] == "UTC" and HOURS["sat"] == []
    assert HOURS["sun"] == ["22:00 - 00:00"] and HOURS["fri"] == ["00:00 - 21:00"]
    assert all(HOURS[d] == ["00:00 - 21:00", "22:00 - 00:00"] for d in ("mon", "tue", "wed", "thu"))


@pytest.mark.parametrize("when, closed", [
    (at(SUN, "12:00"), True), (at(SUN, "21:59", 59), True), (at(SUN, "22:00"), False), (at(SUN, "23:00"), False),          # the week opens Sunday 22:00
    (at(MON, "00:00"), False), (at(MON, "12:00"), False), (at(MON, "20:59", 59), False), (at(MON, "21:00"), True),          # daily pause: 21:00-22:00
    (at(MON, "21:30"), True), (at(MON, "21:59", 59), True), (at(MON, "22:00"), False), (at(MON, "23:59", 59), False),        # reopens 22:00 sharp
    (at(THU, "21:30"), True), (at(THU, "22:00"), False),
    (at(FRI, "12:00"), False), (at(FRI, "20:59", 59), False), (at(FRI, "21:00"), True), (at(FRI, "22:00"), True),            # the week closes Friday 21:00
    (at(SAT, "00:00"), True), (at(SAT, "12:00"), True), (at(SAT, "23:59", 59), True)])
def test_is_market_closed_follows_the_published_hours_exactly(when, closed):
    assert is_market_closed(HOURS, minutes=0, now=when) is closed


@pytest.mark.parametrize("when, closed", [
    (at(MON, "20:57"), False), (at(MON, "20:58", 30), True),                       # the default 2-minute margin: "closed" includes "closing within 2 minutes"
    (at(MON, "22:00"), False), (at(MON, "22:00", 1), False),                       # but right after the pause it is open - no margin applies at the start
    (at(MON, "23:57"), False), (at(MON, "23:58", 30), True),
    (at(SUN, "22:00"), False), (at(FRI, "20:58", 30), True)])
def test_the_two_minute_margin_only_applies_before_a_session_ends(when, closed):
    assert is_market_closed(HOURS, now=when) is closed


@pytest.mark.parametrize("when, closing", [
    (at(MON, "20:59"), False), (at(MON, "21:30"), False), (at(MON, "22:30"), False),       # the daily PAUSE is not the end of the trading day
    (at(MON, "23:57"), False), (at(MON, "23:58", 30), True),                               # the day ends at midnight
    (at(FRI, "20:57"), False), (at(FRI, "20:58", 30), True), (at(FRI, "21:30"), True),
    (at(SAT, "12:00"), True), (at(SUN, "12:00"), False), (at(SUN, "22:30"), False), (at(SUN, "23:58", 30), True)])
def test_is_closing_for_day_means_the_last_session_of_the_day_not_the_pause(when, closing):
    assert is_closing_for_day(HOURS, now=when) is closing


@pytest.mark.parametrize("when, closing", [
    (at(FRI, "12:00"), False), (at(FRI, "20:57"), False), (at(FRI, "20:58", 30), True), (at(FRI, "20:59", 59), True), (at(FRI, "21:00"), False),
    (at(SAT, "23:59"), False), (at(SUN, "23:58", 30), False), (at(MON, "23:58", 30), False), (at(THU, "23:58", 30), False)])
def test_is_closing_for_week_only_fires_in_the_last_two_minutes_before_friday_21_00(when, closing):
    assert is_closing_for_week(HOURS, now=when) is closing


# ---------------------------------------------------------------- opening after the pause
async def test_an_order_in_the_pause_is_rejected_and_one_at_22_00_is_accepted(vix, client):
    vix.clock = at(MON, "21:59", 59)
    refused = await client.confirm(await client.open_position("VIX", 10, "BUY"), attempts=1, delay=0)
    assert refused.rejected and refused.reason == "MARKET_CLOSED" and vix.positions == {}
    vix.clock = at(MON, "22:00")
    accepted = await client.confirm(await client.open_position("VIX", 10, "BUY"), attempts=1, delay=0)
    assert accepted.accepted and len(vix.positions) == 1


async def test_the_week_opens_on_sunday_at_22_00(vix, client):
    vix.clock = at(SUN, "21:59", 59)
    assert (await client.confirm(await client.open_position("VIX", 10, "SELL"), attempts=1, delay=0)).rejected
    vix.clock = at(SUN, "22:00")
    assert (await client.confirm(await client.open_position("VIX", 10, "SELL"), attempts=1, delay=0)).accepted


@pytest.mark.parametrize("when", [at(FRI, "21:00"), at(SAT, "12:00"), at(SUN, "12:00")])
async def test_nothing_can_be_opened_between_friday_21_00_and_sunday_22_00(vix, client, when):
    vix.clock = when
    assert (await client.confirm(await client.open_position("VIX", 10, "BUY"), attempts=1, delay=0)).rejected
    assert vix.positions == {} and vix.balance == 10_000.0


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
async def test_either_direction_opens_at_the_market_price_of_its_side(vix, client, direction):
    reference = await client.open_position("VIX", 100, direction)
    deal_id = (await client.confirm(reference, attempts=1, delay=0)).position_deal_id
    bid, ask = VIX["price_sample"]["bid"], VIX["price_sample"]["offer"]
    assert vix.positions[deal_id]["level"] == (ask if direction == "BUY" else bid) and vix.positions[deal_id]["epic"] == "VIX"


@pytest.mark.parametrize("size, code", [(0.5, "error.invalid.size.minvalue"), (0.99, "error.invalid.size.minvalue"), (25001, "error.invalid.size.maxvalue")])
async def test_sizes_outside_1_to_25000_are_refused_at_once(vix, client, size, code):
    with pytest.raises(APIError) as exc:
        await client.open_position("VIX", size, "BUY")
    assert (exc.value.status_code, exc.value.error_code) == (400, code) and vix.positions == {}


@pytest.mark.parametrize("size", [1, 2, 113, 25000])
async def test_whole_contract_sizes_within_the_limits_are_accepted(vix, client, size):
    vix.balance = 1_000_000.0
    assert (await client.confirm(await client.open_position("VIX", size, "BUY"), attempts=1, delay=0)).accepted


# ---------------------------------------------------------------- holding for days and weeks, in one direction
@pytest.mark.parametrize("direction", ["BUY", "SELL"])
async def test_a_position_survives_pauses_weekends_and_weeks_untouched(vix, client, direction):
    book = Book(vix)
    vix.clock = at(MON, "22:05")
    reference = await client.open_position("VIX", 100, direction)
    book.opened((await client.confirm(reference, attempts=1, delay=0)).position_deal_id, "VIX", 100, direction)
    await coherent(client, vix, book)
    checkpoints = [(TUE, "21:30"), (TUE, "22:10"), (WED, "21:30"), (FRI, "20:59"), (FRI, "21:30"), (SAT, "12:00"), (SUN, "21:00"), (SUN, "22:30"),
                   (MON2, "21:30"), (MON2 + timedelta(days=7), "12:00"), (MON2 + timedelta(days=14), "22:30")]
    price = 17.6
    for day, hhmm in checkpoints:
        vix.clock = at(day, hhmm)
        if vix.status_of("VIX") == "TRADEABLE":                      # the real market only moves while it trades
            price = round(price + 0.35, 2)
            vix.set_quote("VIX", price, round(price + 0.14, 2))
        await coherent(client, vix, book)                           # still open, same size, same direction, balance untouched
        assert len(vix.positions) == 1 and vix.closed == [] and vix.balance == 10_000.0
    (position,) = vix.positions.values()
    assert (position["direction"], position["size"]) == (direction, 100.0)


async def test_a_weekend_gap_through_a_server_side_stop_closes_at_the_gap_price_not_the_stop(vix, client, ):
    book = Book(vix)
    vix.clock = at(WED, "22:30")
    reference = await client.open_position("VIX", 100, "SELL", stop_level=21.0, profit_level=12.0)          # a short with a stop 3.3 points above
    book.opened((await client.confirm(reference, attempts=1, delay=0)).position_deal_id, "VIX", 100, "SELL")
    vix.clock = at(FRI, "20:59")
    vix.set_quote("VIX", 18.0, 18.14)
    await coherent(client, vix, book)
    vix.clock = at(SUN2, "22:00")                                  # the market reopens far above the stop
    vix.set_quote("VIX", 26.0, 26.14)
    (deal_id,) = book.positions
    book.closed(deal_id)
    await coherent(client, vix, book)
    record = vix.closed[-1]
    assert record["source"] == "SL" and record["close_level"] == 26.14                                       # filled at the gap, 5.14 beyond the stop
    assert vix.balance == pytest.approx(10_000.0 + (17.6 - 26.14) * 100)


async def test_after_a_stop_or_profit_the_same_direction_can_be_opened_again(vix, client):
    book = Book(vix)
    for round_ in range(3):
        reference = await client.open_position("VIX", 50, "BUY", stop_level=16.0, profit_level=19.0)
        book.opened((await client.confirm(reference, attempts=1, delay=0)).position_deal_id, "VIX", 50, "BUY")
        vix.set_quote("VIX", 19.5, 19.64)                           # take profit
        (deal_id,) = book.positions
        book.closed(deal_id)
        await coherent(client, vix, book)
        vix.set_quote("VIX", 17.6, 17.74)
    assert [c["source"] for c in vix.closed] == ["TP", "TP", "TP"]


async def test_two_strategies_holding_the_same_direction_keep_separate_positions(vix, client):
    book = Book(vix)
    for _ in range(2):
        reference = await client.open_position("VIX", 20, "SELL")
        book.opened((await client.confirm(reference, attempts=1, delay=0)).position_deal_id, "VIX", 20, "SELL")
    await coherent(client, vix, book)
    assert len(vix.positions) == 2                                  # hedging is off but same-direction deals do not merge (observed)
