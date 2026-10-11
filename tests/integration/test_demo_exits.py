"""DEMO suite, exits: how Capital.com closes a position by a stop / profit level, and how that shows up afterwards.

The stop and the profit level are attributes of the position (set at opening or edited later), not separate orders. This test
puts one just a few basis points from the price of a market that trades around the clock, waits for the market to hit it, and
records what ``GET /history/activity`` says: who closed it (``SL`` / ``TP``), at what level, and how far from the configured level.
Nothing is left open: the tracker closes whatever remains.

Waiting time per test: ``CAPITAL_DEMO_EXIT_WAIT`` seconds (default 240). A market that does not move far enough in that time is not an error.
"""
import asyncio
import os
import time

import pytest

import demo_harness as h
from capital_hook import APIError

pytestmark = [pytest.mark.demo, pytest.mark.asyncio(loop_scope="session")]

WAIT = float(os.environ.get("CAPITAL_DEMO_EXIT_WAIT", "240"))
POLL = 2.0


async def position_gone(client, deal_id) -> bool:
    try:
        await client.get_position(deal_id)
        return False
    except APIError as exc:
        if exc.status_code == 404:
            return True
        raise


async def wait_for_exit(client, deal_id, demo_fake, move) -> float | None:
    """Seconds until the position disappeared, or None if it is still open after ``WAIT``. In the harness's own dry run the fake market is moved."""
    started = time.monotonic()
    if demo_fake is not None:
        move()
    while time.monotonic() - started < WAIT:
        if await position_gone(client, deal_id):
            return round(time.monotonic() - started, 1)
        await asyncio.sleep(0 if demo_fake is not None else POLL)
    return None


@pytest.mark.parametrize("kind", ["profit_target", "stop_loss"])
async def test_a_level_just_beside_the_price_is_executed_by_the_market_and_reported(demo_client, demo_market, demo_fake, tracker, recorder, kind):
    ask, bid = demo_market.ask, demo_market.bid
    gap = 0.0004                                                              # 0.04 % - above the 0.01 % minimum distance
    if kind == "profit_target":
        options = {"profit_level": h.level(ask, 1 + gap, demo_market.decimals), "stop_level": h.level(ask, 0.7, demo_market.decimals)}
        configured, move = options["profit_level"], lambda: demo_fake.set_quote(demo_market.epic, configured + 5, configured + 6)
    else:
        options = {"stop_level": h.level(bid, 1 - gap, demo_market.decimals), "profit_level": h.level(ask, 1.3, demo_market.decimals)}
        configured, move = options["stop_level"], lambda: demo_fake.set_quote(demo_market.epic, configured - 5, configured - 4)
    _, confirmation = await tracker.open(demo_market.epic, demo_market.min_size, "BUY", **options)
    if not confirmation.accepted:
        recorder.note(f"exit_{kind}", {"open_rejected": {"deal_status": confirmation.deal_status, "reason": confirmation.reason}})
        pytest.skip("the open was not accepted; see observed.json")
    deal_id = confirmation.position_deal_id
    position = await demo_client.get_position(deal_id)
    note = {"configured_level": configured, "position_reports": {"stop_level": position.get("stop_level"), "profit_level": position.get("profit_level")}}

    seconds = await wait_for_exit(demo_client, deal_id, demo_fake, move)
    note["seconds_until_closed"] = seconds
    if seconds is None:
        note["outcome"] = f"not triggered within {WAIT:g} s"
        recorder.note(f"exit_{kind}", note)
        pytest.skip(f"the market did not reach the level within {WAIT:g} s (nothing wrong); see observed.json")

    event = None
    for _ in range(10):                                                       # the activity can lag behind the position list
        event = await demo_client.get_close_event(deal_id)
        if event is not None:
            break
        await asyncio.sleep(0 if demo_fake is not None else 1.5)
    activity = await demo_client.get_activity(deal_id=deal_id, detailed=True, last_period=3600)
    note["activity"] = [{"type": a.get("type"), "status": a.get("status"), "source": a.get("source"), "level": (a.get("details") or {}).get("level")}
                        for a in activity]
    note["close_event"] = None if event is None else {"source": event.source, "level": event.level, "open_level": event.open_level,
                                                      "level_vs_configured": event.level_vs(configured), "by_stop": event.by_stop, "by_profit_target": event.by_profit_target}
    recorder.note(f"exit_{kind}", note)
    assert event is not None, "the position vanished but the activity history has no close entry for it"
    assert event.source == ("TP" if kind == "profit_target" else "SL"), f"closed by {event.source!r}, expected the {kind} level to do it"
    assert event.level is not None


async def test_a_close_request_is_reported_as_user_with_its_level(demo_client, demo_market, tracker, recorder):
    _, confirmation = await tracker.open(demo_market.epic, demo_market.min_size, "BUY")
    if not confirmation.accepted:
        pytest.skip("the open was not accepted; see observed.json")
    deal_id = confirmation.position_deal_id
    assert await demo_client.get_close_event(deal_id) is None                 # still open: no close event
    await demo_client.close_position(deal_id)
    event = None
    for _ in range(10):
        event = await demo_client.get_close_event(deal_id)
        if event is not None:
            break
        await asyncio.sleep(1.5)
    recorder.note("exit_close_request", {"close_event": None if event is None else {"source": event.source, "level": event.level, "open_level": event.open_level}})
    assert event is not None and event.source == "USER"


async def test_a_bad_stop_edit_is_refused_and_what_the_activity_shows_is_recorded(demo_client, demo_market, tracker, recorder):
    _, confirmation = await tracker.open(demo_market.epic, demo_market.min_size, "BUY", stop_level=h.level(demo_market.ask, 0.9, demo_market.decimals))
    if not confirmation.accepted:
        pytest.skip("the open was not accepted; see observed.json")
    deal_id = confirmation.position_deal_id
    outcome = {}
    try:
        await demo_client.update_position(deal_id, stop_level=h.level(demo_market.ask, 1.2, demo_market.decimals))       # above the market for a BUY
        outcome["edit"] = "accepted"
    except APIError as exc:
        outcome["edit"] = {"status": exc.status_code, "error_code": exc.error_code}
    await asyncio.sleep(1)
    outcome["activity"] = [{"type": a.get("type"), "status": a.get("status"), "source": a.get("source")}
                           for a in await demo_client.get_activity(deal_id=deal_id, detailed=True, last_period=600)]
    outcome["stop_after"] = (await demo_client.get_position(deal_id)).get("stop_level")
    recorder.note("exit_bad_stop_edit", outcome)
    assert outcome["edit"] != "accepted"
