import httpx
import pytest

from capital_hook import APIError, TradeDirection


# ---------------- open_position ----------------
async def test_open_position_sends_exactly_epic_direction_and_size_by_default(client, fake):
    await client.open_position("GOLD", 2, TradeDirection.SELL)
    assert fake.bodies[-1] == {"epic": "GOLD", "direction": "SELL", "size": "2"}


@pytest.mark.parametrize("size, encoded", [(1.5, "1.5"), (5, "5"), (1.0, "1.0"), (0.00001, "0.00001"), (1e-05, "0.00001"), (0.00001234, "0.00001234"), (1e16, "10000000000000000")])
async def test_open_position_encodes_the_size_as_a_plain_decimal_string(client, fake, size, encoded):
    await client.open_position("GOLD", size, "BUY")
    assert fake.bodies[-1]["size"] == encoded


@pytest.mark.parametrize("direction", ["buy", "Buy", "BUY", TradeDirection.BUY])
async def test_open_position_accepts_the_direction_as_enum_or_any_case_string(client, fake, direction):
    await client.open_position("GOLD", 1, direction)
    assert fake.bodies[-1]["direction"] == "BUY"


@pytest.mark.parametrize("direction", [TradeDirection.NEUTRAL, "NEUTRAL", "HOLD", "", None])
async def test_open_position_rejects_a_direction_that_is_not_buy_or_sell_without_calling_the_api(client, fake, direction):
    with pytest.raises(ValueError, match="BUY or SELL"):
        await client.open_position("GOLD", 1, direction)
    assert fake.requests == []


async def test_open_position_sends_stop_and_profit_options_only_when_given(client, fake):
    await client.open_position("GOLD", 1, "BUY", stop_level=1900.5, profit_level=2100, guaranteed_stop=False, trailing_stop=True,
                               stop_distance=10, profit_distance=20, stop_amount=5, profit_amount=6)
    assert fake.bodies[-1] == {"epic": "GOLD", "direction": "BUY", "size": "1", "stopLevel": 1900.5, "profitLevel": 2100, "guaranteedStop": False,
                               "trailingStop": True, "stopDistance": 10, "profitDistance": 20, "stopAmount": 5, "profitAmount": 6}


async def test_open_position_returns_the_reference_and_creates_the_position(client, fake):
    reference = await client.open_position("GOLD", 1, "BUY", stop_level=1900, profit_level=2100)
    assert reference.startswith("o_")
    (deal_id,) = fake.positions
    position = await client.get_position(deal_id)
    assert position["stop_level"] == 1900 and position["profit_level"] == 2100


async def test_open_position_raises_api_error_with_status_and_code_when_rejected(client, fake):
    fake.fail_open = True
    with pytest.raises(APIError) as err:
        await client.open_position("GOLD", 1, "BUY")
    assert (err.value.status_code, err.value.error_code) == (400, "error.invalid.size") and fake.positions == {}


# ---------------- get_position ----------------
async def test_get_position_returns_the_flat_position_plus_reference_and_stops(client, fake):
    fake.positions["D1"] = {"epic": "GOLD", "size": 2.0, "direction": "SELL", "level": 2000.0}
    fake.meta["D1"] = {"dealReference": "p_D1", "stopLevel": 2100.0, "profitLevel": 1900.0}
    position = await client.get_position("D1")
    assert position == {"epic": "GOLD", "size": 2.0, "direction": "SELL", "pnl": 0.0, "open_price": 2000.0, "deal_id": "D1",
                        "created_date": "2026-07-12T10:00:00", "deal_reference": "p_D1", "stop_level": 2100.0, "profit_level": 1900.0}


async def test_get_position_leaves_out_the_stops_that_are_not_set(client, fake):
    fake.positions["D1"] = {"epic": "GOLD", "size": 1.0, "direction": "BUY", "level": 2001.0}
    position = await client.get_position("D1")
    assert "stop_level" not in position and "profit_level" not in position


async def test_get_position_of_an_unknown_deal_is_a_404(client):
    with pytest.raises(APIError) as err:
        await client.get_position("NOPE")
    assert (err.value.status_code, err.value.error_code) == (404, "error.not-found.dealId")


# ---------------- update_position ----------------
async def test_update_position_changes_the_stops_and_returns_the_p_reference(client, fake):
    await client.open_position("GOLD", 1, "BUY")
    (deal_id,) = fake.positions
    assert await client.update_position(deal_id, stop_level=1950, profit_level=2050) == f"p_{deal_id}"
    assert fake.bodies[-1] == {"stopLevel": 1950, "profitLevel": 2050}
    position = await client.get_position(deal_id)
    assert (position["stop_level"], position["profit_level"]) == (1950, 2050)


async def test_update_position_sends_only_the_given_fields(client, fake):
    await client.open_position("GOLD", 1, "BUY", stop_level=1900, profit_level=2100)
    (deal_id,) = fake.positions
    await client.update_position(deal_id, profit_level=2200)
    assert fake.bodies[-1] == {"profitLevel": 2200}
    position = await client.get_position(deal_id)
    assert (position["stop_level"], position["profit_level"]) == (1900, 2200)       # the stop was left alone


async def test_update_position_without_any_setting_is_refused_before_calling_the_api(client, fake):
    with pytest.raises(ValueError, match="at least one"):
        await client.update_position("D1")
    assert fake.requests == []


async def test_update_position_of_an_unknown_deal_is_a_404(client):
    with pytest.raises(APIError) as err:
        await client.update_position("NOPE", stop_level=1)
    assert (err.value.status_code, err.value.error_code) == (404, "error.not-found.dealId")


# ---------------- session: ping / logout ----------------
async def test_ping_returns_ok_and_keeps_the_session(client, fake):
    assert await client.ping() == "OK"
    assert fake.requests[-1][:2] == ("GET", "/api/v1/ping") and client.authenticated


async def test_logout_ends_the_session_and_the_next_call_logs_in_again(client, fake):
    await client.login()
    tokens = client.session_tokens
    await client.logout()
    assert not client.authenticated and fake.valid_cst is None
    assert fake.requests[-1][:2] == ("DELETE", "/api/v1/session")
    assert tokens != ("", "")                                                   # the old tokens are gone from the client
    await client.get_accounts()
    assert fake.logins == 2 and client.authenticated


async def test_logout_when_not_logged_in_does_nothing(client, fake):
    await client.logout()
    assert fake.requests == []


async def test_logout_of_an_already_expired_session_still_clears_the_tokens(client, fake):
    await client.login()
    fake.expire_session()
    await client.logout()                                                       # the server answers 401: nothing left to end
    assert not client.authenticated


async def test_logout_reports_a_server_error_but_still_forgets_the_tokens(make_client):
    def server_error(request):
        if request.method == "DELETE" and request.url.path == "/api/v1/session":
            return httpx.Response(500, json={"errorCode": "error.server"})

    client = make_client(server_error)
    await client.login()
    with pytest.raises(APIError):
        await client.logout()
    assert not client.authenticated
