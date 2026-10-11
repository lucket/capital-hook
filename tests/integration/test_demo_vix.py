"""DEMO suite, VIX: read-only. Records what Capital.com publishes about the VIX market (name, type, trading hours, dealing rules)
and checks it against what the library and the trading server assume. Nothing is traded.

The epic is ``VIX`` (the trading server maps TradingView ``CBOE:AVIX`` to it); override with ``CAPITAL_DEMO_VIX_EPIC``.
"""
import os
import re
from datetime import datetime, timezone

import pytest

import demo_harness as h
from capital_hook import is_closing_for_day, is_closing_for_week, is_market_closed

pytestmark = [pytest.mark.demo, pytest.mark.asyncio(loop_scope="session")]

VIX_EPIC = os.environ.get("CAPITAL_DEMO_VIX_EPIC", "VIX")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RANGE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})? - \d{1,2}:\d{2}(:\d{2})?$")


async def test_vix_is_found_and_described(demo_client, recorder):
    found = await demo_client.get_markets(search_term="VIX")
    matches = [{"epic": m["epic"], "name": m.get("instrumentName"), "type": m.get("instrumentType"), "status": m.get("marketStatus"),
                "expiry": m.get("expiry")} for m in found]
    recorder.note("vix_search", {"term": "VIX", "matches": matches})
    assert VIX_EPIC in [m["epic"] for m in matches], f"no market with epic {VIX_EPIC!r}; a search for 'VIX' found {[m['epic'] for m in matches]}"

    market = await demo_client.get_market(VIX_EPIC)
    instrument, rules, snapshot = market["instrument"], market["dealingRules"], market["snapshot"]
    recorder.note("vix", {
        "epic": instrument.get("epic"), "name": instrument.get("name"), "symbol": instrument.get("symbol"), "type": instrument.get("type"),
        "currency": instrument.get("currency"), "country": instrument.get("country"), "expiry": instrument.get("expiry"), "lot_size": instrument.get("lotSize"),
        "margin_factor": [instrument.get("marginFactor"), instrument.get("marginFactorUnit")], "streaming_prices": instrument.get("streamingPricesAvailable"),
        "guaranteed_stop_allowed": instrument.get("guaranteedStopAllowed"), "overnight_fee": instrument.get("overnightFee"),
        "dealing_rules": rules, "decimals": snapshot.get("decimalPlacesFactor"), "scaling_factor": snapshot.get("scalingFactor"),
        "market_status": snapshot.get("marketStatus"), "market_modes": snapshot.get("marketModes"),
        "price_sample": {"bid": snapshot.get("bid"), "offer": snapshot.get("offer"), "high": snapshot.get("high"), "low": snapshot.get("low")},
        "opening_hours": instrument.get("openingHours")})

    assert instrument["epic"] == VIX_EPIC and instrument.get("name") and instrument.get("type")
    assert float(rules["minDealSize"]["value"]) > 0 and float(rules["maxDealSize"]["value"]) >= float(rules["minDealSize"]["value"])
    if snapshot.get("bid") is not None and snapshot.get("offer") is not None:
        assert 0 < float(snapshot["bid"]) <= float(snapshot["offer"])


async def test_vix_opening_hours_are_in_the_format_and_zone_the_library_assumes(demo_client, recorder):
    hours = await demo_client.get_opening_hours(VIX_EPIC)
    assert hours, f"{VIX_EPIC} publishes no opening hours"
    zone = hours.get("zone")
    ranges = {day: hours.get(day) for day in DAYS}
    bad = {day: r for day, r in ranges.items() if r is None or any(not RANGE.match(x) for x in r)}
    recorder.note("vix_hours", {"zone": zone, "days": ranges, "unparseable": bad, "wrapping_sessions": h.wrapping_sessions(hours),
                                "days_without_trading": [d for d, r in ranges.items() if not r]})
    assert not bad, f"hours the library cannot read: {bad}"
    # hours.py and the trading server treat these times as UTC and ignore "zone": anything else would shift every open/close decision
    assert zone in (None, "UTC"), f"openingHours are in zone {zone!r}, but the library assumes UTC"


async def test_vix_trading_hours_agree_with_the_market_status_right_now(demo_client, recorder):
    market = await demo_client.get_market(VIX_EPIC)
    status = market["snapshot"].get("marketStatus")
    hours = market["instrument"].get("openingHours") or {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    closed_now = is_market_closed(hours, minutes=0, now=now)
    recorder.note("vix_hours_vs_status", {"checked_at_utc": now.strftime("%a %H:%M"), "marketStatus": status, "library_says_closed": closed_now,
                                          "closing_for_day": is_closing_for_day(hours, minutes=0, now=now),
                                          "closing_for_week": is_closing_for_week(hours, minutes=0, now=now)})
    if status == "TRADEABLE":
        assert not closed_now, "Capital.com says VIX is TRADEABLE but the library's hours helper says it is closed: the app would refuse to trade it"
