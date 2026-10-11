"""How a position ended (stop, profit target, a close request, the broker) and whether the price jumped past its level."""
import pytest

from capital_hook import CloseEvent


async def open_deal(client, **options):
    return (await client.confirm(await client.open_position("GOLD", 2, "BUY", **options), attempts=1, delay=0)).position_deal_id


async def test_get_close_event_is_none_while_the_position_is_open(client, fake):
    deal_id = await open_deal(client, stop_level=1900)
    assert await client.get_close_event(deal_id) is None


async def test_get_close_event_is_none_for_a_deal_it_never_heard_of(client):
    assert await client.get_close_event("NOT-A-DEAL") is None


async def test_get_close_event_reports_a_close_request_as_user(client, fake):
    deal_id = await open_deal(client)
    fake.set_quote("GOLD", 2050.0, 2051.0)
    await client.close_position(deal_id)
    event = await client.get_close_event(deal_id)
    assert (event.source, event.level, event.open_level, event.deal_id) == ("USER", 2050.0, 2001.0, deal_id)
    assert not event.by_stop and not event.by_profit_target and not event.by_broker


async def test_get_close_event_reports_a_stop_that_jumped_past_its_level(client, fake):
    deal_id = await open_deal(client, stop_level=1990, profit_level=2100)
    fake.set_quote("GOLD", 1970.0, 1971.0)                        # the price gapped 20 below the stop before anything could execute
    event = await client.get_close_event(deal_id)
    assert event.by_stop and event.source == "SL" and event.level == 1970.0
    assert event.level_vs(1990) == -20.0                          # filled 20 worse than the stop level
    assert fake.deleted == []                                     # nobody asked for it: the market did it


async def test_get_close_event_reports_a_profit_target_and_its_overshoot(client, fake):
    deal_id = await open_deal(client, stop_level=1900, profit_level=2100)
    fake.set_quote("GOLD", 2130.0, 2131.0)
    event = await client.get_close_event(deal_id)
    assert event.by_profit_target and event.level == 2130.0 and event.level_vs(2100) == 30.0


async def test_get_close_event_reports_a_close_out_by_the_broker(client, fake):
    deal_id = await open_deal(client)
    fake.set_quote("GOLD", 1500.0, 1501.0)
    fake.close_out(deal_id)                                       # margin call
    event = await client.get_close_event(deal_id)
    assert event.by_broker and event.source == "CLOSE_OUT" and event.level == 1500.0 and fake.deleted == []


async def test_close_out_closes_the_position_realises_the_loss_and_keeps_the_books_straight(client, fake):
    deal_id = await open_deal(client)
    fake.set_quote("GOLD", 1500.0, 1501.0)
    fake.close_out(deal_id)
    assert fake.positions == {} and [c["source"] for c in fake.closed] == ["CLOSE_OUT"] and fake.balance == pytest.approx(10_000 + (1500 - 2001) * 2)


async def test_get_close_event_of_a_position_that_came_from_a_working_order(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1950)
    fake.set_quote("GOLD", 1940.0, 1941.0)                        # the order fills: a position opens
    (deal_id,) = fake.positions
    assert await client.get_close_event(deal_id) is None
    await client.close_position(deal_id)
    assert (await client.get_close_event(deal_id)).source == "USER"


async def test_get_close_event_asks_for_the_details_of_that_deal_only(client, fake):
    deal_id = await open_deal(client)
    await client.get_close_event(deal_id, last_period=3600)
    assert fake.queries[-1][2] == {"dealId": deal_id, "detailed": "true", "lastPeriod": "3600"}


def test_close_event_from_activity_tolerates_missing_details():
    event = CloseEvent.from_activity({"dealId": "D", "source": "SL", "dateUTC": "2026-01-01T00:00:00"})
    assert (event.deal_id, event.source, event.level, event.open_level) == ("D", "SL", None, None) and event.level_vs(10.0) is None and event.by_stop


@pytest.mark.parametrize("source, stop, target, broker", [("SL", True, False, False), ("TP", False, True, False), ("USER", False, False, False),
                                                           ("CLOSE_OUT", False, False, True), ("SYSTEM", False, False, True), ("DEALER", False, False, True)])
def test_close_event_classifies_its_source(source, stop, target, broker):
    event = CloseEvent.from_activity({"dealId": "D", "source": source})
    assert (event.by_stop, event.by_profit_target, event.by_broker) == (stop, target, broker)


# ---------------------------------------------------------------- reject_next
async def test_reject_next_rejects_the_next_open_with_the_given_reason_and_nothing_opens(client, fake):
    fake.reject_next("MARKET_CLOSED")
    confirmation = await client.confirm(await client.open_position("GOLD", 1, "BUY"), attempts=1, delay=0)
    assert confirmation.rejected and confirmation.reason == "MARKET_CLOSED" and fake.positions == {}
    assert ("POSITION", "REJECTED") in [(a["type"], a["status"]) for a in fake.activity]


async def test_reject_next_applies_to_that_many_opens_and_then_stops(client, fake):
    fake.reject_next("INSUFFICIENT_FUNDS", times=2)
    reasons = [(await client.confirm(await client.open_position("GOLD", 1, "BUY"), attempts=1, delay=0)).reason for _ in range(3)]
    assert reasons == ["INSUFFICIENT_FUNDS", "INSUFFICIENT_FUNDS", None] and len(fake.positions) == 1


async def test_reject_next_defaults_to_market_closed(client, fake):
    fake.reject_next()
    assert (await client.confirm(await client.open_position("GOLD", 1, "SELL"), attempts=1, delay=0)).reason == "MARKET_CLOSED"
