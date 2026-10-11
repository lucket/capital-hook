from datetime import datetime, timedelta, timezone

import pytest

from capital_hook import APIError, TradeDirection, WorkingOrderType


# ---------------- create_working_order ----------------
async def test_create_working_order_returns_a_reference_and_lists_the_order(client, fake):
    reference = await client.create_working_order("GOLD", "BUY", 1.5, 1900.0)
    assert reference.startswith("o_")
    (order,) = await client.get_working_orders()
    assert (order["epic"], order["direction"], order["size"], order["level"], order["type"]) == ("GOLD", "BUY", 1.5, 1900.0, "LIMIT")
    assert order["time_in_force"] == "GOOD_TILL_CANCELLED" and order["good_till_date"] is None
    assert fake.positions == {}                                                    # an order is not a position


async def test_create_working_order_encodes_the_request(client, fake):
    await client.create_working_order("GOLD", TradeDirection.SELL, 0.00001, 2100, WorkingOrderType.STOP, stop_level=2200, profit_level=2000,
                                      good_till_date=datetime(2030, 1, 2, 3, 4, 5))
    assert fake.bodies[-1] == {"epic": "GOLD", "direction": "SELL", "size": "0.00001", "level": 2100, "type": "STOP",
                               "stopLevel": 2200, "profitLevel": 2000, "goodTillDate": "2030-01-02T03:04:05"}


@pytest.mark.parametrize("kind, expected", [("limit", "LIMIT"), ("STOP", "STOP"), (WorkingOrderType.LIMIT, "LIMIT")])
async def test_create_working_order_accepts_the_type_as_enum_or_any_case_string(client, fake, kind, expected):
    await client.create_working_order("GOLD", "BUY", 1, 1900, kind)
    assert fake.bodies[-1]["type"] == expected


async def test_create_working_order_defaults_to_a_limit_order(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    assert fake.bodies[-1]["type"] == "LIMIT"


@pytest.mark.parametrize("bad", [dict(type="MARKET"), dict(direction="NEUTRAL"), dict(direction="HOLD")])
async def test_create_working_order_refuses_bad_arguments_before_calling_the_api(client, fake, bad):
    args = {"epic": "GOLD", "direction": "BUY", "size": 1, "level": 1900, **bad}
    with pytest.raises(ValueError):
        await client.create_working_order(**args)
    assert fake.requests == []


async def test_create_working_order_with_a_date_in_the_past_is_a_400(client, fake):
    with pytest.raises(APIError) as err:
        await client.create_working_order("GOLD", "BUY", 1, 1900, good_till_date=datetime(2000, 1, 1))
    assert (err.value.status_code, err.value.error_code) == (400, "error.not-in-future.goodTillDate") and fake.orders == {}


async def test_create_working_order_converts_aware_datetimes_to_utc(client, fake):
    future = datetime.now(timezone.utc) + timedelta(days=2)
    await client.create_working_order("GOLD", "BUY", 1, 1900, good_till_date=future.astimezone(timezone(timedelta(hours=5))))
    assert fake.bodies[-1]["goodTillDate"] == future.strftime("%Y-%m-%dT%H:%M:%S")
    (order,) = await client.get_working_orders()
    assert order["time_in_force"] == "GOOD_TILL_DATE" and order["good_till_date"] == future.strftime("%Y-%m-%dT%H:%M:%S")


async def test_create_working_order_takes_a_ready_made_timestamp_string(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900, good_till_date="2099-01-01T00:00:00")
    assert fake.bodies[-1]["goodTillDate"] == "2099-01-01T00:00:00"


# ---------------- get_working_orders ----------------
async def test_get_working_orders_is_empty_without_orders(client):
    assert await client.get_working_orders() == []


async def test_get_working_orders_flattens_the_nested_shape(client, fake):
    fake.orders["WO1"] = {"epic": "GOLD", "direction": "SELL", "size": 2.0, "level": 1900.0, "type": "STOP", "goodTillDate": "2099-01-01T00:00:00", "created": "2026-07-12T10:00:00"}
    assert await client.get_working_orders() == [{
        "deal_id": "WO1", "epic": "GOLD", "direction": "SELL", "size": 2.0, "level": 1900.0, "type": "STOP",
        "time_in_force": "GOOD_TILL_DATE", "good_till_date": "2099-01-01T00:00:00", "created_date": "2026-07-12T10:00:00"}]


async def test_get_working_orders_includes_distances_when_reported(client, fake):
    fake.orders["WO1"] = {"epic": "GOLD", "direction": "BUY", "size": 1.0, "level": 1900.0, "type": "LIMIT", "goodTillDate": None, "created": "x"}
    original = fake._order_body
    fake._order_body = lambda d, o: (lambda b: (b["workingOrderData"].update(stopDistance=10, profitDistance=20), b)[1])(original(d, o))
    (order,) = await client.get_working_orders()
    assert (order["stop_distance"], order["profit_distance"]) == (10, 20)


# ---------------- update_working_order ----------------
async def test_update_working_order_moves_the_level(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    (order,) = await client.get_working_orders()
    reference = await client.update_working_order(order["deal_id"], level=1850.5)
    assert reference.startswith("o_") and fake.bodies[-1] == {"level": 1850.5}
    assert (await client.get_working_orders())[0]["level"] == 1850.5


async def test_update_working_order_changes_the_expiry(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    (order,) = await client.get_working_orders()
    await client.update_working_order(order["deal_id"], good_till_date=datetime(2099, 5, 6, 7, 8, 9))
    assert fake.bodies[-1] == {"goodTillDate": "2099-05-06T07:08:09"}
    assert (await client.get_working_orders())[0]["good_till_date"] == "2099-05-06T07:08:09"


async def test_update_working_order_with_a_past_date_is_a_400_and_changes_nothing(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    (deal_id,) = fake.orders
    with pytest.raises(APIError) as err:
        await client.update_working_order(deal_id, good_till_date=datetime(2000, 1, 1))
    assert err.value.error_code == "error.not-in-future.goodTillDate" and fake.orders[deal_id]["goodTillDate"] is None


async def test_update_working_order_without_any_setting_is_refused_before_calling_the_api(client, fake):
    with pytest.raises(ValueError, match="at least one"):
        await client.update_working_order("WO1")
    assert fake.requests == []


async def test_update_working_order_of_an_unknown_deal_is_a_404(client):
    with pytest.raises(APIError) as err:
        await client.update_working_order("NOPE", level=1)
    assert (err.value.status_code, err.value.error_code) == (404, "error.not-found.dealId")


# ---------------- cancel_working_order ----------------
async def test_cancel_working_order_removes_it_and_never_creates_a_position(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    (deal_id,) = fake.orders
    assert (await client.cancel_working_order(deal_id)).startswith("o_")
    assert await client.get_working_orders() == [] and fake.orders == {} and fake.positions == {}


async def test_cancel_working_order_twice_is_a_404_the_second_time(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    (deal_id,) = fake.orders
    await client.cancel_working_order(deal_id)
    with pytest.raises(APIError) as err:
        await client.cancel_working_order(deal_id)
    assert (err.value.status_code, err.value.error_code) == (404, "error.not-found.dealId")


async def test_cancel_working_order_of_an_unknown_deal_is_a_404_and_touches_nothing(client, fake):
    await client.create_working_order("GOLD", "BUY", 1, 1900)
    with pytest.raises(APIError):
        await client.cancel_working_order("NOPE")
    assert len(fake.orders) == 1
