"""Trade lifecycles: after EVERY step the state must be the same in three places.

* what the client reports (``get_positions`` / ``get_working_orders`` / ``get_account``),
* what the fake holds internally, and
* what the test expects, kept in ``Book`` - a tiny model that never looks at the fake's own arithmetic.

Then the fake's own invariants are checked as well (``fake`` fixture teardown + explicit call in ``coherent``).
"""
from datetime import datetime, timedelta

import httpx
import pytest

from book import START, Book, coherent
from capital_hook import APIError


@pytest.fixture
def book(fake):
    return Book(fake)


async def open_(client, book, size=1.0, direction="BUY", epic="GOLD", **options):
    reference = await client.open_position(epic, size, direction, **options)
    confirmation = await client.confirm(reference, attempts=1, delay=0)
    assert confirmation.accepted, confirmation.reason
    book.opened(confirmation.position_deal_id, epic, size, direction)
    return confirmation.position_deal_id


async def close_(client, book, deal_id):
    reference = (await client.close_position(deal_id))["dealReference"]
    book.closed(deal_id)
    return reference


# ---------------------------------------------------------------- open -> price move -> close
async def test_a_long_trade_realises_its_profit(client, fake, book):
    await coherent(client, fake, book)
    deal_id = await open_(client, book, size=2)
    await coherent(client, fake, book)
    fake.set_quote("GOLD", 2050.0, 2051.0)
    await coherent(client, fake, book)                           # unrealised profit shows in the account, balance unchanged
    assert fake.balance == START
    await close_(client, book, deal_id)
    await coherent(client, fake, book)
    assert fake.balance == pytest.approx(START + 98.0) and book.positions == {}


async def test_a_short_trade_realises_its_loss(client, fake, book):
    deal_id = await open_(client, book, size=3, direction="SELL")
    fake.set_quote("GOLD", 2010.0, 2011.0)
    await coherent(client, fake, book)
    await close_(client, book, deal_id)
    await coherent(client, fake, book)
    assert fake.balance == pytest.approx(START - 33.0)


async def test_a_confirmed_close_reports_the_profit_the_account_received(client, fake, book):
    deal_id = await open_(client, book, size=2)
    fake.set_quote("GOLD", 2050.0, 2051.0)
    reference = await close_(client, book, deal_id)
    confirmation = await client.confirm(reference, attempts=1, delay=0)
    assert confirmation.raw["profit"] == pytest.approx(book.realised) and confirmation.status == "CLOSED"
    await coherent(client, fake, book)


# ---------------------------------------------------------------- rejections and errors leave no trace
async def test_a_rejected_open_changes_nothing(client, fake, book):
    fake.market_status["GOLD"] = "CLOSED"
    reference = await client.open_position("GOLD", 1, "BUY")
    confirmation = await client.confirm(reference, attempts=1, delay=0)
    assert confirmation.rejected and not confirmation.accepted and confirmation.position_deal_id is None
    await coherent(client, fake, book)                           # still nothing open, balance untouched
    assert ("POSITION", "REJECTED") in [(a["type"], a["status"]) for a in fake.activity]
    fake.market_status["GOLD"] = "TRADEABLE"
    await open_(client, book)                                    # and trading works again afterwards
    await coherent(client, fake, book)


async def test_not_enough_funds_rejects_without_side_effects(client, fake, book):
    fake.balance = 1100.0
    book_ = Book(fake)
    first = await open_(client, book_, size=10)                  # uses ~1000 of margin
    second = await client.confirm(await client.open_position("GOLD", 10, "BUY"), attempts=1, delay=0)
    assert second.rejected and not second.accepted and second.reason == "INSUFFICIENT_FUNDS"
    assert set(fake.positions) == {first}
    fake.check_invariants()


async def test_closing_twice_fails_the_second_time_and_changes_nothing(client, fake, book):
    deal_id = await open_(client, book)
    await close_(client, book, deal_id)
    with pytest.raises(APIError) as exc:
        await client.close_position(deal_id)
    assert exc.value.status_code == 404 and fake.deleted == [deal_id]
    await coherent(client, fake, book)


async def test_closing_an_unknown_deal_changes_nothing(client, fake, book):
    deal_id = await open_(client, book)
    with pytest.raises(APIError) as exc:
        await client.close_position("NOT-A-DEAL")
    assert exc.value.status_code == 404
    await coherent(client, fake, book)
    assert deal_id in fake.positions


# ---------------------------------------------------------------- stops
async def test_invalid_stop_updates_are_refused_and_the_old_stop_stays(client, fake, book):
    deal_id = await open_(client, book, stop_level=1900, profit_level=2100)
    with pytest.raises(APIError) as exc:
        await client.update_position(deal_id, stop_level=2500)         # above the market for a BUY
    assert exc.value.status_code == 400 and exc.value.error_code.startswith("error.invalid.stoploss")
    assert (await client.get_position(deal_id))["stop_level"] == 1900
    await client.update_position(deal_id, stop_level=1950)
    assert (await client.get_position(deal_id))["stop_level"] == 1950
    await coherent(client, fake, book)


async def test_a_stop_loss_closes_the_position_and_the_balance_follows(client, fake, book):
    deal_id = await open_(client, book, size=2, stop_level=1950, profit_level=2100)
    fake.set_quote("GOLD", 1940.0, 1941.0)
    book.closed(deal_id)                                           # the market closed it at the bid, not at the stop level
    await coherent(client, fake, book)
    assert [(c["source"], c["dealId"]) for c in fake.closed] == [("SL", deal_id)] and fake.deleted == []
    assert fake.balance == pytest.approx(START + (1940 - 2001) * 2)


async def test_a_take_profit_on_a_short_closes_it(client, fake, book):
    deal_id = await open_(client, book, direction="SELL", profit_level=1900, stop_level=2100)
    fake.set_quote("GOLD", 1890.0, 1891.0)
    book.closed(deal_id)
    await coherent(client, fake, book)
    assert [c["source"] for c in fake.closed] == ["TP"]


async def test_a_stop_set_after_opening_still_triggers(client, fake, book):
    deal_id = await open_(client, book)
    await client.update_position(deal_id, stop_level=1990)
    fake.set_quote("GOLD", 1985.0, 1986.0)
    book.closed(deal_id)
    await coherent(client, fake, book)


# ---------------------------------------------------------------- working orders
async def create_order(client, book, fake, direction="BUY", level=1950, kind="LIMIT", size=1.0, **options):
    reference = await client.create_working_order("GOLD", direction, size, level, kind, **options)
    confirmation = await client.confirm(reference, attempts=1, delay=0)
    assert confirmation.accepted
    book.orders[confirmation.deal_id] = {"level": level}
    return confirmation.deal_id


async def test_create_update_cancel_an_order_and_it_never_becomes_a_position(client, fake, book):
    order_id = await create_order(client, book, fake)
    await coherent(client, fake, book)
    await client.update_working_order(order_id, level=1900)
    assert fake.orders[order_id]["level"] == 1900
    await coherent(client, fake, book)
    await client.cancel_working_order(order_id)
    book.orders.clear()
    await coherent(client, fake, book)
    fake.set_quote("GOLD", 1850.0, 1851.0)                         # price goes right through the old level
    await coherent(client, fake, book)
    assert fake.positions == {} and fake.balance == START


async def test_cancelling_an_order_twice_fails_the_second_time(client, fake, book):
    order_id = await create_order(client, book, fake)
    await client.cancel_working_order(order_id)
    book.orders.clear()
    with pytest.raises(APIError) as exc:
        await client.cancel_working_order(order_id)
    assert exc.value.status_code == 404
    await coherent(client, fake, book)


async def test_an_order_that_fills_becomes_exactly_one_position(client, fake, book):
    order_id = await create_order(client, book, fake, level=1950, stop_level=1900)
    fake.set_quote("GOLD", 1940.0, 1941.0)
    (deal_id,) = fake.positions
    del book.orders[order_id]
    book.opened(deal_id, "GOLD", 1.0, "BUY")
    await coherent(client, fake, book)
    assert fake.positions[deal_id]["level"] == 1941.0              # filled at the ask when the price reached it
    await close_(client, book, deal_id)
    await coherent(client, fake, book)


async def test_a_filled_order_cannot_be_cancelled_afterwards(client, fake, book):
    order_id = await create_order(client, book, fake)
    fake.set_quote("GOLD", 1940.0, 1941.0)
    with pytest.raises(APIError) as exc:
        await client.cancel_working_order(order_id)
    assert exc.value.status_code == 404 and len(fake.positions) == 1
    fake.check_invariants()


async def test_an_expired_order_is_gone_and_does_not_fill_later(client, fake, book):
    clock = [datetime(2030, 1, 1, 12, 0, 0)]
    fake.now = lambda: clock[0]
    await create_order(client, book, fake, good_till_date=datetime(2030, 1, 1, 13, 0, 0))
    await coherent(client, fake, book)
    clock[0] += timedelta(hours=2)
    book.orders.clear()
    await coherent(client, fake, book)
    fake.set_quote("GOLD", 1900.0, 1901.0)
    await coherent(client, fake, book)
    assert fake.positions == {}


# ---------------------------------------------------------------- sessions and faults
async def test_the_session_expiring_in_the_middle_of_a_lifecycle_is_invisible(client, fake, book):
    deal_id = await open_(client, book)
    fake.expire_session()
    await coherent(client, fake, book)                             # re-login happens inside; state is intact
    fake.expire_session()
    await close_(client, book, deal_id)
    await coherent(client, fake, book)
    assert fake.logins >= 3


async def test_a_transient_server_error_on_close_leaves_the_position_open_and_a_retry_works(client, fake, book):
    deal_id = await open_(client, book)
    fake.fail_next("DELETE", "/api/v1/positions", status=503, error_code="error.unavailable")
    with pytest.raises(APIError) as exc:
        await client.close_position(deal_id)
    assert exc.value.status_code == 503
    await coherent(client, fake, book)                             # nothing happened
    await close_(client, book, deal_id)
    await coherent(client, fake, book)


async def test_a_lost_response_on_open_is_reconciled_without_a_second_position(client, fake, book):
    fake.lose_response_next("POST", "/api/v1/positions")
    with pytest.raises(httpx.ReadTimeout):
        await client.open_position("GOLD", 1, "BUY")                # the server opened it; we never saw the reference
    deal_id = await client.find_deal_id("GOLD", 1, "BUY", attempts=1, delay=0)
    assert deal_id in fake.positions
    book.opened(deal_id, "GOLD", 1.0, "BUY")
    await coherent(client, fake, book)
    assert len(fake.positions) == 1


async def test_blindly_retrying_a_lost_open_doubles_the_exposure(client, fake, book):
    """Why callers must reconcile (find_deal_id / get_positions) instead of retrying an open that timed out."""
    fake.lose_response_next("POST", "/api/v1/positions")
    with pytest.raises(httpx.ReadTimeout):
        await client.open_position("GOLD", 1, "BUY")
    await client.open_position("GOLD", 1, "BUY")
    assert len(fake.positions) == 2
    fake.check_invariants()


async def test_a_lost_response_on_close_is_reconciled_from_the_positions_list(client, fake, book):
    deal_id = await open_(client, book)
    fake.set_quote("GOLD", 2030.0, 2031.0)
    fake.lose_response_next("DELETE", "/api/v1/positions")
    with pytest.raises(httpx.ReadTimeout):
        await client.close_position(deal_id)
    assert deal_id not in {p["deal_id"] for p in await client.get_positions()}          # it is gone: the close did happen
    book.closed(deal_id)
    await coherent(client, fake, book)
    with pytest.raises(APIError) as exc:
        await client.close_position(deal_id)                         # and closing again is a clean 404
    assert exc.value.status_code == 404


async def test_a_lost_response_on_an_order_cancel_is_reconciled_from_the_orders_list(client, fake, book):
    order_id = await create_order(client, book, fake)
    fake.lose_response_next("DELETE", "/api/v1/workingorders")
    with pytest.raises(httpx.ReadTimeout):
        await client.cancel_working_order(order_id)
    assert await client.get_working_orders() == []
    book.orders.clear()
    await coherent(client, fake, book)


# ---------------------------------------------------------------- netting and several instruments
async def test_an_offsetting_deal_closes_the_oldest_position_and_the_books_balance(client, fake, book):
    first = await open_(client, book, size=1)
    fake.set_quote("GOLD", 2020.0, 2021.0)
    second = await open_(client, book, size=1)
    fake.set_quote("GOLD", 2040.0, 2041.0)
    await client.confirm(await client.open_position("GOLD", 1, "SELL"), attempts=1, delay=0)
    book.closed(first)                                              # oldest first, at the bid
    await coherent(client, fake, book)
    assert set(fake.positions) == {second}


async def test_two_instruments_and_an_order_end_flat_with_the_right_balance(client, fake, book):
    fake.set_quote("US100", 15000.0, 15001.0)
    gold = await open_(client, book, size=2)
    index = await open_(client, book, size=1, direction="SELL", epic="US100")
    order_id = await create_order(client, book, fake, level=1900)
    await coherent(client, fake, book)
    fake.set_quote("GOLD", 2020.0, 2021.0)
    fake.set_quote("US100", 14900.0, 14901.0)
    await coherent(client, fake, book)
    await close_(client, book, gold)
    await client.cancel_working_order(order_id)
    book.orders.clear()
    await close_(client, book, index)
    await coherent(client, fake, book)
    assert fake.positions == {} and fake.orders == {} and fake.margin() == 0
    assert fake.balance == pytest.approx(START + (2020 - 2001) * 2 + (15000 - 14901) * 1)
