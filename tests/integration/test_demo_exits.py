"""DEMO suite, exits: how Capital.com closes a position by a stop / profit level, and how that shows up afterwards.

The stop and the profit level are attributes of the position (set at opening or edited later), not separate orders. This test
puts one just a few basis points from the price of a market that trades around the clock, waits for the market to hit it, and
records what ``GET /history/activity`` says: who closed it (``SL`` / ``TP``), at what level, and how far from the configured level.
Nothing is left open: the tracker closes whatever remains.

Each test opens the smallest position with BOTH a stop and a profit level just beside the price (twice the market's minimum distance), so whichever way the market
moves, one of them is soon reached; it then checks that the activity names the level that did it (``SL`` / ``TP``) and records the price it got.
Prices are read just before each order (a snapshot taken minutes earlier makes a tight level invalid); a level refused as too close is retried wider.
Waiting time per test: ``CAPITAL_DEMO_EXIT_WAIT`` seconds (default 150); the position is looked at every ``CAPITAL_DEMO_EXIT_POLL`` seconds (default 1).
A market that does not move far enough in that time is not an error (the skip says how far the price moved). A tight-spread, busy market is fast
(try ``CAPITAL_DEMO_EPIC=US100``); BTCUSD's spread is 0.06 %, so its profit target needs a move of about 0.08 %. Run with ``-s`` to see the progress.
"""
import asyncio
import os
import time

import pytest

import demo_harness as h
from capital_hook import APIError

pytestmark = [pytest.mark.demo, pytest.mark.asyncio(loop_scope="session")]

WAIT = float(os.environ.get("CAPITAL_DEMO_EXIT_WAIT", "150"))
POLL = float(os.environ.get("CAPITAL_DEMO_EXIT_POLL", "1"))
HEARTBEAT = 15.0


async def position_gone(client, deal_id) -> bool:
    try:
        await client.get_position(deal_id)
        return False
    except APIError as exc:
        if exc.status_code == 404:
            return True
        raise


@pytest.fixture
def say(request):
    """Write a line to the terminal at once, even while pytest captures output."""
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    return (lambda text: reporter.write_line(f"    {text}")) if reporter else print


async def wait_for_exit(client, deal_id, demo_fake, move, say=print) -> float | None:
    """Seconds until the position disappeared, or None if it is still open after ``WAIT``. In the harness's own dry run the fake market is moved."""
    started = last_report = time.monotonic()
    if demo_fake is not None:
        move()
    while time.monotonic() - started < WAIT:
        if await position_gone(client, deal_id):
            return round(time.monotonic() - started, 1)
        if demo_fake is None and time.monotonic() - last_report >= HEARTBEAT:
            last_report = time.monotonic()
            say(f"... {time.monotonic() - started:.0f} s of {WAIT:g}: still open")
        await asyncio.sleep(0 if demo_fake is not None else POLL)
    return None


def bracket(direction: str, bid: float, ask: float, gap: float, decimals: int) -> dict:
    """A stop and a profit level just beside the price, each on the side its direction needs. Whichever way the market moves, one of them is soon reached."""
    if direction == "BUY":                                   # sells to close: the stop is below the bid, the target above the ask
        return {"stop_level": h.level(bid, 1 - gap, decimals), "profit_level": h.level(ask, 1 + gap, decimals)}
    return {"stop_level": h.level(ask, 1 + gap, decimals), "profit_level": h.level(bid, 1 - gap, decimals)}


async def fresh_quote(client, epic) -> tuple[float, float]:
    """The price NOW. (The session's market snapshot is read once, at the start: after minutes it is stale and a tight level computed from it is refused.)"""
    snapshot = (await client.get_market(epic))["snapshot"]
    return float(snapshot["bid"]), float(snapshot["offer"])


async def open_with_bracket(client, tracker, epic, size, direction, decimals, first_gap, tries=3):
    """Open with a bracket at the current price; a level refused as too close is retried, 1.5x wider, at the price of that moment.
    Returns (confirmation or None, levels, refused attempts, (bid, ask) used, gap used)."""
    attempts, levels, bid, ask, gap = [], {}, 0.0, 0.0, first_gap
    for attempt in range(tries):
        bid, ask = await fresh_quote(client, epic)
        gap = first_gap * 1.5 ** attempt
        levels = bracket(direction, bid, ask, gap, decimals)
        try:
            _, confirmation = await tracker.open(epic, size, direction, **levels)
            return confirmation, levels, attempts, (bid, ask), gap
        except APIError as exc:
            attempts.append({"gap_pct": round(gap * 100, 4), "price": [bid, ask], "levels": levels, "refused": exc.error_code})
            if exc.status_code != 400 or "invalid" not in (exc.error_code or ""):
                raise
    return None, levels, attempts, (bid, ask), gap


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
async def test_a_stop_and_a_profit_level_beside_the_price_one_is_executed_by_the_market_and_reported(demo_client, demo_market, demo_fake, tracker, recorder, direction, say):
    epic = demo_market.epic
    first_gap = max(2 * demo_market.min_distance_pct / 100, 0.0002)           # twice the market's minimum distance from the price (0.02 % where it is 0.01 %)
    confirmation, levels, attempts, (bid, ask), gap = await open_with_bracket(demo_client, tracker, epic, demo_market.min_size, direction, demo_market.decimals, first_gap)
    note = {"direction": direction, "refused_attempts": attempts}
    if confirmation is None or not confirmation.accepted:
        note["open"] = "never accepted"
        recorder.note(f"exit_bracket_{direction.lower()}", note)
        pytest.skip(f"the levels were refused three times ({attempts[-1]['refused'] if attempts else confirmation.reason}); see observed.json")
    stop, profit = levels["stop_level"], levels["profit_level"]
    deal_id = confirmation.position_deal_id
    position = await demo_client.get_position(deal_id)
    note.update({"stop_level": stop, "profit_level": profit, "price_at_open": [bid, ask], "distance_pct": round(gap * 100, 4), "spread_pct": round((ask - bid) / bid * 100, 4),
                 "position_reports": {"stop_level": position.get("stop_level"), "profit_level": position.get("profit_level")}})
    triggered = stop - 5 if direction == "BUY" else stop + 5                  # (the harness's own dry run moves the fake market through the stop)
    move = lambda: demo_fake.set_quote(epic, triggered, triggered + (ask - bid))

    say(f"{epic} {direction}: stop {stop} / profit {profit} ({gap * 100:.3f} % from the price {bid} / {ask}, spread {(ask - bid) / bid * 100:.3f} %), waiting up to {WAIT:g} s")
    seconds = await wait_for_exit(demo_client, deal_id, demo_fake, move, say)
    note["seconds_until_closed"] = seconds
    if seconds is None:
        now_bid, now_ask = await fresh_quote(demo_client, epic)
        note["outcome"] = f"neither level reached within {WAIT:g} s; price now {now_bid} / {now_ask}"
        recorder.note(f"exit_bracket_{direction.lower()}", note)
        pytest.skip(f"the market did not reach either level within {WAIT:g} s (price {bid}/{ask} -> {now_bid}/{now_ask}; nothing wrong); see observed.json")

    event = None
    for _ in range(10):                                                       # the activity can lag behind the position list
        event = await demo_client.get_close_event(deal_id)
        if event is not None:
            break
        await asyncio.sleep(0 if demo_fake is not None else 1.5)
    activity = await demo_client.get_activity(deal_id=deal_id, detailed=True, last_period=3600)
    note["activity"] = [{"type": a.get("type"), "status": a.get("status"), "source": a.get("source"), "level": (a.get("details") or {}).get("level")} for a in activity]
    if event is not None and event.level is not None:
        nearer = "SL" if abs(event.level - stop) <= abs(event.level - profit) else "TP"
        note["close_event"] = {"source": event.source, "level": event.level, "open_level": event.open_level, "nearer_level": nearer,
                               "level_vs_stop": event.level_vs(stop), "level_vs_profit": event.level_vs(profit), "by_stop": event.by_stop, "by_profit_target": event.by_profit_target}
    recorder.note(f"exit_bracket_{direction.lower()}", note)
    assert event is not None, "the position vanished but the activity history has no close entry for it"
    assert event.level is not None
    assert event.source == nearer, f"closed at {event.level} (stop {stop}, profit {profit}) but the activity says {event.source!r}, expected {nearer!r}"


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
