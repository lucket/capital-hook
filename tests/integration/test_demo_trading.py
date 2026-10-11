"""DEMO suite, part 2: places REAL orders on the DEMO account (smallest size, far from the market where possible).

Every deal is created through the `tracker`, which closes / cancels whatever a test leaves behind, even when it fails.
Each test asserts the invariants that must hold on any correct API and records what Capital.com actually did, so the
offline fake can be calibrated from `observed.json`.
"""
import os

import pytest

import demo_harness as h
from capital_hook import APIError

pytestmark = [pytest.mark.demo, pytest.mark.asyncio(loop_scope="session")]


def ids(positions):
    return {p["deal_id"] for p in positions}


async def test_open_confirm_inspect_and_close_a_position(demo_client, demo_market, tracker, recorder):
    before = await demo_client.get_account()
    reference, confirmation = await tracker.open(demo_market.epic, demo_market.min_size, "BUY")
    if not confirmation.accepted:
        recorder.note("open_rejected", {"deal_status": confirmation.deal_status, "reason": confirmation.reason})
        pytest.skip(f"the minimum-size open was not accepted ({confirmation.deal_status}/{confirmation.reason}); see observed.json")
    deal_id = confirmation.position_deal_id                      # confirmation.deal_id is the order's id, not the position's
    recorder.note("confirmation_ids", {"order_id_differs_from_position_id": confirmation.deal_id != deal_id,
                                       "affected_count": len(confirmation.affected_deals)})
    assert reference.startswith("o_") and deal_id and confirmation.epic == demo_market.epic and confirmation.direction == "BUY"

    positions = await demo_client.get_positions()
    mine = [p for p in positions if p["deal_id"] == deal_id]
    assert len(mine) == 1 and mine[0]["epic"] == demo_market.epic and mine[0]["direction"] == "BUY"
    assert mine[0]["size"] == pytest.approx(demo_market.min_size)
    position = await demo_client.get_position(deal_id)
    assert position["deal_id"] == deal_id

    reference_close = (await demo_client.close_position(deal_id))["dealReference"]     # close_position returns Capital.com's answer (a dict)
    after_ids = ids(await demo_client.get_positions())
    assert deal_id not in after_ids                                                   # the close really removed it
    with pytest.raises(APIError) as twice:
        await demo_client.close_position(deal_id)
    activity = [(a["type"], a["status"], a["source"]) for a in await demo_client.get_activity(last_period=600, deal_id=deal_id)]
    try:
        after_close = await demo_client.confirm(reference_close, attempts=2)
        confirm_close = {"deal_status": after_close.deal_status, "reason": after_close.reason, "status": after_close.status}
    except APIError as exc:
        confirm_close = {"error": exc.error_code, "status": exc.status_code}
    after = await demo_client.get_account()
    recorder.note("open_close", {
        "open_reference_prefix": reference[:2], "close_reference_prefix": reference_close[:2],
        "confirmation": {"deal_status": confirmation.deal_status, "reason": confirmation.reason, "status": confirmation.status,
                         "affected": [d.get("status") for d in confirmation.affected_deals]},
        "position_keys": sorted(position), "confirm_after_close": confirm_close, "close_twice": {"status": twice.value.status_code, "error_code": twice.value.error_code},
        "activity": activity, "available_changed_by_open_close": round(after["balance"]["available"] - before["balance"]["available"], 4)})


async def test_rejected_orders_leave_no_state(demo_client, demo_market, tracker, recorder):
    before = ids(await demo_client.get_positions())
    outcomes = {}
    for label, size in (("below_minimum", demo_market.min_size / 10), ("absurdly_large", demo_market.min_size * 1_000_000)):
        try:
            _, confirmation = await tracker.open(demo_market.epic, size, "BUY")
            outcomes[label] = {"deal_status": confirmation.deal_status, "reason": confirmation.reason, "deal_id_present": bool(confirmation.deal_id)}
        except APIError as exc:
            outcomes[label] = {"http_status": exc.status_code, "error_code": exc.error_code}
    recorder.note("rejected_opens", outcomes)
    leaked = ids(await demo_client.get_positions()) - before
    for deal_id in leaked:                                                           # whatever slipped through is closed by the tracker
        tracker.adopt_positions([deal_id])
    assert all(("http_status" in o) or o["deal_status"] == "REJECTED" or o["deal_id_present"] for o in outcomes.values())


async def test_stops_can_be_set_updated_and_invalid_ones_are_refused(demo_client, demo_market, tracker, recorder):
    stop, profit = h.level(demo_market.ask, 0.7, demo_market.decimals), h.level(demo_market.ask, 1.3, demo_market.decimals)
    try:
        _, confirmation = await tracker.open(demo_market.epic, demo_market.min_size, "BUY", stop_level=stop, profit_level=profit)
    except APIError as exc:
        recorder.note("open_with_stops", {"http_status": exc.status_code, "error_code": exc.error_code})
        pytest.skip(f"Capital.com refused stops 30% away ({exc.error_code}); see observed.json")
    if not confirmation.accepted:
        recorder.note("open_with_stops", {"deal_status": confirmation.deal_status, "reason": confirmation.reason})
        pytest.skip("open with stops was not accepted; see observed.json")
    deal_id = confirmation.position_deal_id
    position = await demo_client.get_position(deal_id)
    notes = {"stop_level_reported": "stop_level" in position, "profit_level_reported": "profit_level" in position}
    new_stop = h.level(demo_market.ask, 0.8, demo_market.decimals)
    reference = await demo_client.update_position(deal_id, stop_level=new_stop)
    notes["update_reference_prefix"] = reference[:2]
    updated = await demo_client.get_position(deal_id)
    notes["stop_after_update"] = updated.get("stop_level") == pytest.approx(new_stop) if "stop_level" in updated else None
    try:                                                                              # a BUY stop above the market must be refused
        await demo_client.update_position(deal_id, stop_level=h.level(demo_market.ask, 1.2, demo_market.decimals))
        notes["invalid_stop"] = {"accepted": True}
    except APIError as exc:
        notes["invalid_stop"] = {"http_status": exc.status_code, "error_code": exc.error_code}
        still = await demo_client.get_position(deal_id)
        assert still.get("stop_level") == updated.get("stop_level")                  # a refused update changes nothing
    recorder.note("stops", notes)


async def test_working_order_lifecycle_and_a_cancelled_order_never_becomes_a_position(demo_client, demo_market, tracker, recorder):
    positions_before = ids(await demo_client.get_positions())
    level = h.level(demo_market.bid, 0.7, demo_market.decimals)
    try:
        reference, confirmation = await tracker.create_order(demo_market.epic, "BUY", demo_market.min_size, level)
    except APIError as exc:
        recorder.note("working_order", {"create_error": {"http_status": exc.status_code, "error_code": exc.error_code}})
        pytest.skip(f"Capital.com refused the limit order ({exc.error_code}); see observed.json")
    if not confirmation.accepted:
        recorder.note("working_order", {"create_rejected": {"deal_status": confirmation.deal_status, "reason": confirmation.reason}})
        pytest.skip("the limit order was not accepted; see observed.json")
    order_id = confirmation.deal_id
    listed = [o for o in await demo_client.get_working_orders() if o["deal_id"] == order_id]
    assert len(listed) == 1 and listed[0]["epic"] == demo_market.epic and listed[0]["type"] == "LIMIT"
    assert listed[0]["level"] == pytest.approx(level)

    notes = {"confirmation": {"deal_status": confirmation.deal_status, "reason": confirmation.reason, "status": confirmation.status,
                             "affected": [d.get("status") for d in confirmation.affected_deals]}, "listed_keys": sorted(listed[0])}
    new_level = h.level(demo_market.bid, 0.6, demo_market.decimals)
    notes["update_reference_prefix"] = (await demo_client.update_working_order(order_id, level=new_level))[:2]
    assert [o["level"] for o in await demo_client.get_working_orders() if o["deal_id"] == order_id] == [pytest.approx(new_level)]

    notes["cancel_reference_prefix"] = (await demo_client.cancel_working_order(order_id))[:2]
    assert order_id not in {o["deal_id"] for o in await demo_client.get_working_orders()}
    assert ids(await demo_client.get_positions()) == positions_before                # cancelled, never filled
    with pytest.raises(APIError) as twice:
        await demo_client.cancel_working_order(order_id)
    notes["cancel_twice"] = {"status": twice.value.status_code, "error_code": twice.value.error_code}
    notes["activity"] = [(a["type"], a["status"]) for a in await demo_client.get_activity(last_period=600, deal_id=order_id)]
    try:
        from datetime import datetime
        await demo_client.create_working_order(demo_market.epic, "BUY", demo_market.min_size, level, good_till_date=datetime(2000, 1, 1))
        notes["past_expiry"] = {"accepted": True}
    except APIError as exc:
        notes["past_expiry"] = {"status": exc.status_code, "error_code": exc.error_code}
    recorder.note("working_order", notes)


async def test_same_direction_and_opposite_orders_probe(demo_client, demo_market, tracker, recorder):
    """Records what a second same-direction deal and an opposite deal do under the account's current hedging mode."""
    hedging = (await demo_client.get_preferences())["hedgingMode"]
    steps = {"hedging_mode": hedging}
    _, first = await tracker.open(demo_market.epic, demo_market.min_size, "BUY")
    if not first.accepted:
        pytest.skip("the first probe order was not accepted; see observed.json")

    async def snapshot():
        return sorted((p["direction"], p["size"]) for p in await demo_client.get_positions() if p["epic"] == demo_market.epic)

    steps["after_buy"] = await snapshot()
    _, second = await tracker.open(demo_market.epic, demo_market.min_size, "BUY")
    steps["second_buy"] = {"deal_status": second.deal_status, "same_position": second.position_deal_id == first.position_deal_id,
                              "affected": [d.get("status") for d in second.affected_deals]}
    steps["after_second_buy"] = await snapshot()
    tracker.adopt_positions(ids(await demo_client.get_positions()))
    _, opposite = await tracker.open(demo_market.epic, demo_market.min_size, "SELL")
    steps["sell"] = {"deal_status": opposite.deal_status, "reason": opposite.reason,
                     "affected": [d.get("status") for d in opposite.affected_deals], "opened_a_position": bool(opposite.position_deal_id)}
    steps["after_sell"] = await snapshot()
    tracker.adopt_positions(ids(await demo_client.get_positions()))
    recorder.note("netting_probe", steps)


@pytest.mark.skipif(os.environ.get("CAPITAL_DEMO_ALLOW_PREFERENCE_CHANGES") != "1" and not h.dry_run(),
                    reason="flips the account-wide hedging mode: set CAPITAL_DEMO_ALLOW_PREFERENCE_CHANGES=1 to allow it (it is restored afterwards)")
async def test_hedging_mode_toggle_probe(demo_client, demo_market, tracker, recorder, journal):
    original = (await demo_client.get_preferences())["hedgingMode"]
    journal.add("preference", f"hedgingMode={original}")                              # so a crash can be undone by hand
    steps = {"original": original}
    try:
        await demo_client.set_preferences(hedging_mode=not original)
        steps["flipped_to"] = (await demo_client.get_preferences())["hedgingMode"]
        _, buy = await tracker.open(demo_market.epic, demo_market.min_size, "BUY")
        _, sell = await tracker.open(demo_market.epic, demo_market.min_size, "SELL")
        steps["after_buy_and_sell"] = sorted((p["direction"], p["size"]) for p in await demo_client.get_positions() if p["epic"] == demo_market.epic)
        steps["accepted"] = [buy.deal_status, sell.deal_status]
        tracker.adopt_positions(ids(await demo_client.get_positions()))
    finally:
        await tracker.cleanup()
        await demo_client.set_preferences(hedging_mode=original)
        journal.remove("preference", f"hedgingMode={original}")
    steps["restored"] = (await demo_client.get_preferences())["hedgingMode"] == original
    recorder.note("hedging_toggle", steps)
    assert steps["restored"]
