"""DEMO suite, part 1: read-only. Nothing is created, changed or closed."""
import pytest

import demo_harness as h
from capital_hook import APIError

pytestmark = [pytest.mark.demo, pytest.mark.asyncio(loop_scope="session")]


async def test_ping_account_and_preferences(demo_client, recorder):
    assert await demo_client.ping() == "OK"
    accounts = await demo_client.get_accounts()
    assert accounts and {"balance", "deposit", "profitLoss", "available"} <= set((await demo_client.get_account())["balance"])
    preferences = await demo_client.get_preferences()
    assert isinstance(preferences["hedgingMode"], bool)
    recorder.note("account", {"accounts": len(accounts), "hedging_mode": preferences["hedgingMode"],
                              "leverage_categories": sorted(preferences.get("leverages", {}))})


async def test_markets_prices_and_opening_hours(demo_client, demo_market, recorder):
    markets = await demo_client.get_markets()
    assert markets and {"epic", "instrumentType"} <= set(markets[0])
    types = await demo_client.get_instrument_types()
    assert types[demo_market.epic] == demo_market.instrument_type or not demo_market.instrument_type
    ask, bid = await demo_client.get_prices(demo_market.epic)
    assert ask >= bid > 0

    hours = await demo_client.get_opening_hours(demo_market.epic)
    sample = [m["epic"] for m in markets[:8]]
    wrapping = {}
    for epic in dict.fromkeys([demo_market.epic, *sample]):
        epic_hours = hours if epic == demo_market.epic else await demo_client.get_opening_hours(epic)
        found = h.wrapping_sessions(epic_hours)
        if found:
            wrapping[epic] = found
    recorder.note("opening_hours", {"market_hours_published": bool(hours), "example_range": next(iter((hours or {}).values()), [None])[:1] if hours else None,
                                    "epics_checked": len(set([demo_market.epic, *sample])), "sessions_that_wrap_midnight": wrapping})


async def test_positions_orders_and_activity_are_readable(demo_client, recorder):
    assert isinstance(await demo_client.get_positions(), list)
    assert isinstance(await demo_client.get_working_orders(), list)
    activities = await demo_client.get_activity(last_period=3600, detailed=True)
    assert isinstance(activities, list)
    recorder.note("activity", {"sample_keys": sorted(activities[0]) if activities else None,
                               "statuses_seen": sorted({a.get("status") for a in activities}), "types_seen": sorted({a.get("type") for a in activities})})


async def test_unknown_ids_report_documented_errors(demo_client, recorder):
    found = {}
    calls = {
        "get_position": lambda: demo_client.get_position("does-not-exist"),
        "update_position": lambda: demo_client.update_position("does-not-exist", stop_level=1.0),
        "close_position": lambda: demo_client.close_position("does-not-exist"),
        "update_working_order": lambda: demo_client.update_working_order("does-not-exist", level=1.0),
        "cancel_working_order": lambda: demo_client.cancel_working_order("does-not-exist"),
        "confirm": lambda: demo_client.confirm("o_does-not-exist", attempts=1),
    }
    for name, call in calls.items():
        with pytest.raises(APIError) as err:
            await call()
        found[name] = {"status": err.value.status_code, "error_code": err.value.error_code}
    recorder.note("errors_for_unknown_ids", found)
    assert all(v["status"] in (400, 404) for v in found.values())
