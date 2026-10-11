"""FakeCapital's own behaviour. Everything the lifecycle tests assume about the fake is established here first."""
from datetime import datetime, timedelta

import httpx
import pytest

from capital_hook import APIError, CapitalClient
from capital_hook.testing import ASSUMPTIONS, FakeCapital


async def open_ok(client, epic="GOLD", size=1.0, direction="BUY", **options):
    reference = await client.open_position(epic, size, direction, **options)
    confirmation = await client.confirm(reference, attempts=1, delay=0)
    assert confirmation.accepted, confirmation.reason
    return confirmation.position_deal_id


# ---------------------------------------------------------------- money
async def test_open_reserves_margin_but_leaves_the_balance(client, fake):
    deal_id = await open_ok(client, size=10)                                      # 10 x 2001 / 20 = 1000.5 margin
    account = (await client.get_account())["balance"]
    assert account["balance"] == 10000.0 and fake.margin() == pytest.approx(1000.5)
    assert account["available"] == pytest.approx(10000.0 + account["profitLoss"] - 1000.5)
    assert account["profitLoss"] == pytest.approx(-10.0)                           # bought at the ask 2001, marked at the bid 2000
    assert fake.positions[deal_id]["level"] == 2001.0


async def test_buy_is_marked_to_the_bid_and_sell_to_the_ask(client, fake):
    fake.preferences["hedgingMode"] = True                                       # otherwise the SELL would close the BUY
    buy = await open_ok(client, size=2, direction="BUY")
    sell = await open_ok(client, size=3, direction="SELL")
    fake.quote["GOLD"] = (2010.0, 2011.0)
    assert fake.upl(buy) == pytest.approx((2010 - 2001) * 2) and fake.upl(sell) == pytest.approx((2000 - 2011) * 3)
    positions = {p["deal_id"]: p["pnl"] for p in await client.get_positions()}
    assert positions == {buy: pytest.approx(18.0), sell: pytest.approx(-33.0)}
    assert (await client.get_account())["balance"]["profitLoss"] == pytest.approx(18.0 - 33.0)


async def test_close_realises_the_profit_into_the_balance_and_the_ledger(client, fake):
    deal_id = await open_ok(client, size=2)
    fake.quote["GOLD"] = (2050.0, 2051.0)
    await client.close_position(deal_id)
    assert (await client.get_account())["balance"]["balance"] == pytest.approx(10000.0 + (2050 - 2001) * 2)
    (record,) = fake.closed
    assert (record["dealId"], record["source"], record["open_level"], record["close_level"], record["pnl"]) == (deal_id, "USER", 2001.0, 2050.0, 98.0)
    assert fake.deleted == [deal_id] and fake.positions == {} and fake.margin() == 0


async def test_a_losing_sell_reduces_the_balance(client, fake):
    deal_id = await open_ok(client, size=1, direction="SELL")
    fake.quote["GOLD"] = (2100.0, 2101.0)
    await client.close_position(deal_id)
    assert fake.balance == pytest.approx(10000.0 + (2000 - 2101))


async def test_assigning_the_balance_restarts_the_books(client, fake):
    deal_id = await open_ok(client)
    fake.quote["GOLD"] = (2100.0, 2101.0)
    await client.close_position(deal_id)
    fake.balance = 500.0
    fake.check_invariants()                                                      # baseline moved with the assignment
    assert (await client.get_account())["balance"] == {"balance": 500.0, "deposit": 500.0, "profitLoss": 0.0, "available": 500.0}


# ---------------------------------------------------------------- rejections
async def test_a_closed_market_rejects_the_open_without_any_state_change(client, fake):
    fake.market_status["GOLD"] = "CLOSED"
    reference = await client.open_position("GOLD", 1, "BUY")                     # HTTP 200: the outcome is only in the confirmation
    confirmation = await client.confirm(reference, attempts=1, delay=0)
    assert confirmation.rejected and confirmation.reason == "MARKET_CLOSED" and confirmation.deal_id is None and confirmation.affected_deals == ()
    assert fake.positions == {} and fake.balance == 10000.0
    assert [(a["type"], a["status"]) for a in fake.activity] == [("POSITION", "REJECTED")]


async def test_below_the_minimum_size_is_rejected(client, fake):
    fake.min_deal_size["GOLD"] = 0.5
    confirmation = await client.confirm(await client.open_position("GOLD", 0.1, "BUY"), attempts=1, delay=0)
    assert confirmation.rejected and confirmation.reason == "MINIMUM_ORDER_SIZE_ERROR" and fake.positions == {}
    assert (await client.confirm(await client.open_position("GOLD", 0.5, "BUY"), attempts=1, delay=0)).accepted


async def test_insufficient_funds_is_rejected_and_margin_is_counted_across_positions(client, fake):
    fake.balance = 1100.0
    first = await open_ok(client, size=10)                                       # margin 1000.5 fits into 1100
    second = await client.confirm(await client.open_position("GOLD", 5, "BUY"), attempts=1, delay=0)
    assert second.rejected and second.reason == "INSUFFICIENT_FUNDS"             # only ~90 of equity is left
    assert list(fake.positions) == [first]
    huge = await client.confirm(await client.open_position("GOLD", 100000, "BUY"), attempts=1, delay=0)
    assert huge.rejected and huge.reason == "INSUFFICIENT_FUNDS" and list(fake.positions) == [first]


async def test_fail_open_is_still_an_http_400(client, fake):
    fake.fail_open = True
    with pytest.raises(APIError) as err:
        await client.open_position("GOLD", 1, "BUY")
    assert (err.value.status_code, err.value.error_code) == (400, "error.invalid.size") and fake.positions == {}


async def test_an_unknown_epic_is_a_404(client, fake):
    with pytest.raises(APIError) as err:
        await client.open_position("NOPE", 1, "BUY")
    assert (err.value.status_code, err.value.error_code) == (404, "error.not-found.epic")


@pytest.mark.parametrize("option", ["guaranteed_stop", "trailing_stop", "stop_distance", "profit_distance", "stop_amount", "profit_amount"])
async def test_options_the_fake_does_not_model_are_refused_loudly(client, fake, option):
    with pytest.raises(APIError) as err:
        await client.open_position("GOLD", 1, "BUY", **{option: 5 if "stop_" in option or "profit_" in option else True})
    assert err.value.error_code == "error.unsupported.in.fake" and fake.positions == {}


# ---------------------------------------------------------------- stop / profit levels
@pytest.mark.parametrize("direction, options, code", [
    ("BUY", {"stop_level": 2005}, "error.invalid.stoploss.maxvalue: 2001.0"),
    ("BUY", {"profit_level": 2000}, "error.invalid.takeprofit.minvalue: 2001.0"),
    ("SELL", {"stop_level": 1990}, "error.invalid.stoploss.minvalue: 2000.0"),
    ("SELL", {"profit_level": 2010}, "error.invalid.takeprofit.maxvalue: 2000.0")])
async def test_levels_on_the_wrong_side_are_refused_with_the_limit(client, fake, direction, options, code):
    with pytest.raises(APIError) as err:
        await client.open_position("GOLD", 1, direction, **options)
    assert (err.value.status_code, err.value.error_code) == (400, code) and fake.positions == {}


async def test_an_invalid_update_changes_nothing_and_a_valid_one_applies(client, fake):
    deal_id = await open_ok(client, stop_level=1900, profit_level=2100)
    with pytest.raises(APIError) as err:
        await client.update_position(deal_id, stop_level=2500)
    assert err.value.error_code.startswith("error.invalid.stoploss.maxvalue")
    assert (await client.get_position(deal_id))["stop_level"] == 1900
    await client.update_position(deal_id, stop_level=1950)
    assert (await client.get_position(deal_id))["stop_level"] == 1950


async def test_the_stop_loss_triggers_at_the_bid_and_realises_the_loss(client, fake):
    deal_id = await open_ok(client, size=2, stop_level=1950)
    fake.quote["GOLD"] = (1940.0, 1941.0)                                        # a test moves the market directly...
    assert deal_id not in {p["deal_id"] for p in await client.get_positions()}   # ...and the next request sees the stop hit
    (record,) = fake.closed
    assert (record["source"], record["close_level"], record["pnl"]) == ("SL", 1940.0, (1940 - 2001) * 2)
    assert fake.balance == pytest.approx(10000 + (1940 - 2001) * 2) and fake.deleted == []     # not a DELETE request


async def test_the_take_profit_of_a_sell_triggers_at_the_ask(client, fake):
    deal_id = await open_ok(client, size=1, direction="SELL", profit_level=1900)
    fake.set_quote("GOLD", 1890.0, 1895.0)                                       # set_quote reacts immediately
    assert deal_id not in fake.positions
    (record,) = fake.closed
    assert (record["source"], record["close_level"], record["pnl"]) == ("TP", 1895.0, 2000 - 1895)


async def test_no_trigger_while_the_price_stays_between_the_levels(client, fake):
    deal_id = await open_ok(client, stop_level=1900, profit_level=2100)
    fake.set_quote("GOLD", 2050.0, 2051.0)
    assert deal_id in fake.positions and fake.closed == []


async def test_only_the_level_that_was_reached_closes_the_position(client, fake):
    deal_id = await open_ok(client, stop_level=1900, profit_level=2100)
    fake.set_quote("GOLD", 2150.0, 2151.0)
    assert [c["source"] for c in fake.closed] == ["TP"] and deal_id not in fake.positions


# ---------------------------------------------------------------- working orders
@pytest.mark.parametrize("direction, kind, level, quote, fills", [
    ("BUY", "LIMIT", 1950, (1940.0, 1941.0), True), ("BUY", "LIMIT", 1950, (2000.0, 2001.0), False),
    ("BUY", "STOP", 2050, (2060.0, 2061.0), True), ("BUY", "STOP", 2050, (2000.0, 2001.0), False),
    ("SELL", "LIMIT", 2050, (2060.0, 2061.0), True), ("SELL", "LIMIT", 2050, (2000.0, 2001.0), False),
    ("SELL", "STOP", 1950, (1940.0, 1941.0), True), ("SELL", "STOP", 1950, (2000.0, 2001.0), False)])
async def test_working_orders_fill_exactly_when_the_price_reaches_them(client, fake, direction, kind, level, quote, fills):
    reference = await client.create_working_order("GOLD", direction, 1, level, kind)
    order_id = (await client.confirm(reference)).deal_id
    assert order_id in fake.orders and fake.positions == {}
    fake.set_quote("GOLD", *quote)
    assert (order_id not in fake.orders) is fills and bool(fake.positions) is fills
    if fills:
        (position,) = fake.positions.values()
        assert position["direction"] == direction and position["level"] == (quote[1] if direction == "BUY" else quote[0])
        assert ("WORKING_ORDER", "EXECUTED") in [(a["type"], a["status"]) for a in fake.activity if a["dealId"] == order_id]


async def test_a_filled_order_keeps_its_stops_and_becomes_a_normal_position(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1950, stop_level=1900, profit_level=2000)
    fake.set_quote("GOLD", 1940.0, 1941.0)
    (deal_id,) = fake.positions
    position = await client.get_position(deal_id)
    assert (position["stop_level"], position["profit_level"]) == (1900, 2000)
    fake.set_quote("GOLD", 1890.0, 1891.0)                                       # and the stop works
    assert [c["source"] for c in fake.closed] == ["SL"]


async def test_an_order_that_is_already_marketable_fills_at_once(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 2500, "LIMIT")           # willing to pay up to 2500; the ask is 2001
    assert fake.orders == {} and len(fake.positions) == 1


async def test_updating_an_order_to_a_reachable_level_fills_it(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1500)
    (order_id,) = fake.orders
    await client.update_working_order(order_id, level=2500)
    assert fake.orders == {} and len(fake.positions) == 1


async def test_good_till_date_orders_expire_by_the_clock_and_never_fill_afterwards(client, fake):
    clock = [datetime(2030, 1, 1, 12, 0, 0)]
    fake.now = lambda: clock[0]
    await client.create_working_order("GOLD", "BUY", 1, 1950, good_till_date=datetime(2030, 1, 1, 13, 0, 0))
    (order_id,) = fake.orders
    clock[0] += timedelta(minutes=59)
    assert [o["deal_id"] for o in await client.get_working_orders()] == [order_id]
    clock[0] += timedelta(minutes=2)
    assert await client.get_working_orders() == []                               # expired on the next request
    assert ("WORKING_ORDER", "EXPIRED") in [(a["type"], a["status"]) for a in fake.activity if a["dealId"] == order_id]
    fake.set_quote("GOLD", 1900.0, 1901.0)
    assert fake.positions == {}                                                  # price went through the level, too late


async def test_a_cancelled_order_never_fills(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1950)
    (order_id,) = fake.orders
    await client.cancel_working_order(order_id)
    fake.set_quote("GOLD", 1900.0, 1901.0)
    assert fake.positions == {} and fake.orders == {}


# ---------------------------------------------------------------- clock
async def test_the_clock_dates_new_deals_and_orders(client, fake):
    fake.now = lambda: datetime(2031, 5, 6, 7, 8, 9)
    deal_id = await open_ok(client)
    await client.create_working_order("GOLD", "BUY", 1, 1500)
    assert (await client.get_position(deal_id))["created_date"] == "2031-05-06T07:08:09"
    assert (await client.get_working_orders())[0]["created_date"] == "2031-05-06T07:08:09"
    assert {a["date"] for a in fake.activity} == {"2031-05-06T07:08:09"}


# ---------------------------------------------------------------- fault injection
async def test_fail_next_answers_with_the_error_and_changes_nothing(client, fake):
    fake.fail_next("POST", "/api/v1/positions", status=503, error_code="error.unavailable", times=2)
    for _ in range(2):
        with pytest.raises(APIError) as err:
            await client.open_position("GOLD", 1, "BUY")
        assert (err.value.status_code, err.value.error_code) == (503, "error.unavailable")
    assert fake.positions == {}
    await open_ok(client)                                                        # the third attempt goes through
    assert len(fake.positions) == 1


async def test_a_lost_response_executes_the_request_but_the_client_sees_a_timeout(client, fake):
    fake.lose_response_next("POST", "/api/v1/positions")
    with pytest.raises(httpx.ReadTimeout):
        await client.open_position("GOLD", 1, "BUY")
    assert len(fake.positions) == 1                                              # the server DID open it


async def test_fail_next_only_matches_its_method_and_path(client, fake):
    fake.fail_next("DELETE", "/api/v1/positions")
    await open_ok(client)
    assert len(await client.get_positions()) == 1                                # GET is unaffected
    (deal_id,) = fake.positions
    with pytest.raises(APIError):
        await client.close_position(deal_id)
    await client.close_position(deal_id)                                         # the fault was consumed


async def test_positions_failures_switch_still_works(client, fake):
    fake.positions_failures = 1
    with pytest.raises(APIError):
        await client.get_positions()
    assert await client.get_positions() == []


# ---------------------------------------------------------------- sessions
async def test_logout_invalidates_the_tokens_server_side(client, fake):
    await client.login()
    stale = client.session_tokens
    await client.logout()
    assert fake.valid_cst is None and stale[0].startswith("cst-")


async def test_a_new_login_invalidates_the_previous_session(fake):
    first = CapitalClient("a", "b", "c", http=fake.client(), auto_relogin=False)
    second = CapitalClient("a", "b", "c", http=fake.client(), auto_relogin=False)
    await first.login()
    await second.login()
    with pytest.raises(APIError) as err:
        await first.get_accounts()
    assert err.value.status_code == 401


# ---------------------------------------------------------------- bookkeeping
async def test_requests_and_bodies_stay_aligned(client, fake):
    await client.open_position("GOLD", 1, "BUY")
    assert len(fake.requests) == len(fake.bodies)
    assert fake.bodies[fake.requests.index(("POST", "/api/v1/positions", "demo-api-capital.backend-capital.com"))]["epic"] == "GOLD"


def test_the_assumptions_registry_documents_what_is_not_backed_by_the_official_collection():
    assert ASSUMPTIONS and all(isinstance(k, str) and isinstance(v, str) and len(v) > 20 for k, v in ASSUMPTIONS.items())
    assert {"netting", "rejection reasons", "fill price"} <= set(ASSUMPTIONS)


async def test_positions_written_straight_into_the_fake_are_tolerated_by_the_invariants(client, fake):
    fake.positions["D9"] = {"epic": "GOLD", "size": 1.0, "direction": "BUY", "level": 2001.0}
    fake.check_invariants()
    await client.close_position("D9")                                            # and can be closed like any other
    assert fake.closed[0]["dealId"] == "D9"


# ---------------------------------------------------------------- the invariant checker itself (mutation tests)
@pytest.fixture
def healthy():
    """A fake with some history, built without the autouse fixture so tests may corrupt it."""
    async def build():
        fake = FakeCapital()
        client = CapitalClient("me", "pw", "key", http=fake.client())
        deal = await open_ok(client, size=2, stop_level=1900)
        await client.create_working_order("GOLD", "BUY", 1, 1500)
        closed = await open_ok(client, size=1)
        await client.close_position(closed)
        return fake, deal
    return build


@pytest.mark.parametrize("corrupt, expected", [
    (lambda f, d: setattr(f, "_balance", f._balance + 1), "balance"),
    (lambda f, d: f.positions.update({"WO2": {"epic": "GOLD", "size": 1.0, "direction": "BUY", "level": 1.0}}) or f.orders.update({"WO2": dict(next(iter(f.orders.values())))}), "both a position and a working order"),
    (lambda f, d: f.positions.update({f.closed[0]["dealId"]: {"epic": "GOLD", "size": 1.0, "direction": "BUY", "level": 1.0}}), "closed but still open"),
    (lambda f, d: f.closed.append(dict(f.closed[0])), "closed more than once"),
    (lambda f, d: f.positions[d].update(size=0), "non-positive size"),
    (lambda f, d: f.meta.pop(d), "no deal reference"),
    (lambda f, d: f.activity.__setitem__(slice(None), [a for a in f.activity if a["dealId"] != d]), "without an activity record"),
    (lambda f, d: f.activity.__setitem__(slice(None), [a for a in f.activity if not (a["dealId"] == f.closed[0]["dealId"] and a["source"] == "USER")]), "without a close record"),
    (lambda f, d: f.confirmations.update({"o_ghost": {"dealStatus": "ACCEPTED", "dealId": "NOWHERE", "affectedDeals": []}}), "exists nowhere"),
    (lambda f, d: f.confirmations.update({"o_bad": {"dealStatus": "REJECTED", "dealId": d, "affectedDeals": []}}), "rejected yet names a deal"),
    (lambda f, d: next(iter(f.orders.values())).update(level=0), "non-positive level"),
    (lambda f, d: f._fills.append(("WO1", "DEALX")), "does not exist"),
    (lambda f, d: f.activity.append({"dealId": next(iter(f.orders)), "status": "CANCELLED", "type": "WORKING_ORDER", "source": "USER", "details": {}}), "cancelled/expired yet still pending")])
async def test_check_invariants_catches_corruption(healthy, corrupt, expected):
    fake, deal = await healthy()
    fake.check_invariants()                                                      # healthy to begin with
    corrupt(fake, deal)
    with pytest.raises(AssertionError, match=expected):
        fake.check_invariants()


async def test_check_invariants_names_every_problem_at_once(healthy):
    fake, deal = await healthy()
    fake._balance += 5
    fake.positions[deal]["size"] = 0
    with pytest.raises(AssertionError) as err:
        fake.check_invariants()
    assert "balance" in str(err.value) and "non-positive size" in str(err.value)


# ---------------------------------------------------------------- netting (hedging off) - calibrated on DEMO
async def test_same_direction_deals_open_separate_positions_even_with_hedging_off(client, fake):
    first, second = await open_ok(client, size=1), await open_ok(client, size=1)
    assert first != second and set(fake.positions) == {first, second}


async def test_an_opposite_deal_closes_the_position_instead_of_opening_one(client, fake):
    buy = await open_ok(client, size=2)
    fake.quote["GOLD"] = (2050.0, 2051.0)
    reference = await client.open_position("GOLD", 2, "SELL")
    confirmation = await client.confirm(reference, attempts=1, delay=0)
    assert confirmation.accepted and confirmation.affected_deals == () and confirmation.position_deal_id is None   # observed on DEMO
    assert buy not in fake.positions and fake.positions == {}
    assert [(c["dealId"], c["source"], c["pnl"]) for c in fake.closed] == [(buy, "USER", pytest.approx((2050 - 2001) * 2))]


async def test_an_opposite_deal_closes_the_oldest_position_first(client, fake):
    oldest, newest = await open_ok(client, size=1), await open_ok(client, size=1)
    await client.confirm(await client.open_position("GOLD", 1, "SELL"), attempts=1, delay=0)
    assert set(fake.positions) == {newest} and oldest not in fake.positions


async def test_an_opposite_deal_can_close_several_positions(client, fake):
    await open_ok(client, size=1), await open_ok(client, size=2)
    await client.confirm(await client.open_position("GOLD", 3, "SELL"), attempts=1, delay=0)
    assert fake.positions == {}


async def test_a_partial_offset_is_refused_instead_of_guessed(client, fake):
    buy = await open_ok(client, size=2)
    with pytest.raises(APIError) as exc:
        await client.open_position("GOLD", 1, "SELL")
    assert exc.value.error_code == "error.unsupported.in.fake" and set(fake.positions) == {buy}


async def test_with_hedging_on_an_opposite_deal_opens_its_own_position(client, fake):
    fake.preferences["hedgingMode"] = True
    buy, sell = await open_ok(client, size=1), await open_ok(client, size=1, direction="SELL")
    assert set(fake.positions) == {buy, sell}


async def test_opposite_deals_on_another_epic_do_not_offset(client, fake):
    fake.set_quote("US100", 15000.0, 15001.0)
    buy = await open_ok(client, size=1)
    sell = await open_ok(client, epic="US100", size=1, direction="SELL")
    assert set(fake.positions) == {buy, sell}


# ---------------------------------------------------------------- confirmation of a close - observed on DEMO
async def test_a_close_can_be_confirmed_and_reports_the_realised_profit(client, fake):
    deal_id = await open_ok(client, size=2)
    fake.quote["GOLD"] = (2050.0, 2051.0)
    reference = (await client.close_position(deal_id))["dealReference"]
    confirmation = await client.confirm(reference, attempts=1, delay=0)
    assert (confirmation.accepted, confirmation.status, confirmation.epic, confirmation.direction) == (True, "CLOSED", "GOLD", "SELL")
    assert confirmation.deal_id == deal_id and confirmation.raw["profit"] == pytest.approx((2050 - 2001) * 2)
