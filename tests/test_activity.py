from datetime import datetime, timedelta, timezone

import pytest

from capital_hook import APIError


async def test_get_activity_is_empty_on_a_fresh_account(client):
    assert await client.get_activity() == []


async def test_get_activity_records_the_life_of_a_position(client, fake):
    await client.open_position("GOLD", 1, "BUY")
    (deal_id,) = fake.positions
    await client.close_position(deal_id)
    activities = await client.get_activity()
    assert [(a["type"], a["status"], a["dealId"], a["epic"], a["source"]) for a in activities] == [("POSITION", "ACCEPTED", deal_id, "GOLD", "USER")] * 2


async def test_get_activity_records_the_life_of_a_working_order(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    (deal_id,) = fake.orders
    await client.update_working_order(deal_id, level=1800)
    await client.cancel_working_order(deal_id)
    assert [(a["type"], a["status"]) for a in await client.get_activity(deal_id=deal_id)] == [
        ("WORKING_ORDER", "CREATED"), ("WORKING_ORDER", "MODIFIED"), ("WORKING_ORDER", "CANCELLED")]


async def test_get_activity_can_be_limited_to_one_deal(client, fake):
    await client.open_position("GOLD", 1, "BUY")
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    (order_id,) = fake.orders
    assert {a["dealId"] for a in await client.get_activity(deal_id=order_id)} == {order_id}


async def test_get_activity_filter_supports_equals_not_equals_and_lists(client, fake):
    await client.open_position("GOLD", 1, "BUY")
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    (order_id,) = fake.orders
    cancelled = await client.cancel_working_order(order_id)
    assert cancelled
    assert [a["status"] for a in await client.get_activity(filter="status==CANCELLED")] == ["CANCELLED"]
    assert {a["type"] for a in await client.get_activity(filter="type!=POSITION")} == {"WORKING_ORDER"}
    assert len(await client.get_activity(filter="status==CANCELLED,CREATED;type==WORKING_ORDER")) == 2


async def test_get_activity_details_only_when_asked(client, fake):
    await client.open_position("GOLD", 2, "SELL")
    (plain,) = await client.get_activity()
    (detailed,) = await client.get_activity(detailed=True)
    assert "details" not in plain
    assert detailed["details"]["direction"] == "SELL" and detailed["details"]["size"] == 2.0 and detailed["details"]["dealReference"].startswith("o_")


async def test_get_activity_sends_its_parameters_in_capital_coms_format(make_client):
    seen = []

    def spy(request):
        if request.url.path.endswith("/history/activity"):
            seen.append(dict(request.url.params))

    client = make_client(spy)
    await client.get_activity(from_=datetime(2026, 1, 2, 3, 4, 5), to=datetime(2026, 1, 3, 3, 4, 5, tzinfo=timezone(timedelta(hours=1))),
                              last_period=3600, detailed=True, deal_id="D1", filter="status==REJECTED")
    assert seen == [{"from": "2026-01-02T03:04:05", "to": "2026-01-03T02:04:05", "lastPeriod": "3600", "detailed": "true",
                     "dealId": "D1", "filter": "status==REJECTED"}]
    await client.get_activity()
    assert seen[-1] == {}                                                          # nothing is sent unless asked for


async def test_get_activity_error_propagates(make_client):
    import httpx

    client = make_client(lambda r: httpx.Response(400, json={"errorCode": "error.invalid.from"}) if r.url.path.endswith("/history/activity") else None)
    with pytest.raises(APIError) as err:
        await client.get_activity(from_="garbage")
    assert (err.value.status_code, err.value.error_code) == (400, "error.invalid.from")
