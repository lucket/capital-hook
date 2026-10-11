"""Pins what downstream code (the tradingserver test-suite) relies on in `FakeCapital` and in the client's shapes.

If one of these fails, a change is about to break an application that tests against the fake: keep the old behaviour
(additively) or coordinate the change with the downstream project first.
"""
import pytest

from capital_hook import APIError, AuthError, CapitalClient
from capital_hook.testing import FakeCapital


@pytest.fixture
def fake():
    return FakeCapital()


@pytest.fixture
def client(fake):
    return CapitalClient("me", "pw", "key", http=fake.client())


def test_fake_defaults_that_downstream_tests_assume():
    fake = FakeCapital()
    assert fake.positions == {} and fake.deleted == [] and fake.requests == []
    assert fake.balance == 10000.0
    assert fake.quote == {"GOLD": (2000.0, 2001.0)}                       # epic -> (bid, ask)
    assert fake.markets == {"GOLD": "COMMODITIES", "US100": "INDICES"}
    assert fake.logins == 0 and fake.login_ok is True
    assert fake.fail_open is False and fake.fail_close is False and fake.positions_failures == 0
    assert fake.preferences["leverages"]["COMMODITIES"]["current"] == 20
    assert fake.preferences["leverages"]["INDICES"]["current"] == 20
    assert fake.preferences["hedgingMode"] is False
    assert set(fake.hours) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


async def test_requests_are_method_path_host_tuples(client, fake):
    await client.get_accounts()
    assert fake.requests[-1] == ("GET", "/api/v1/accounts", "demo-api-capital.backend-capital.com")


async def test_an_opened_position_is_a_dict_with_epic_size_direction_level(client, fake):
    await client.open_position("GOLD", 1.5, "BUY")
    (deal_id, pos), = fake.positions.items()
    assert {"epic", "size", "direction", "level"} <= set(pos)
    assert (pos["epic"], pos["size"], pos["direction"], pos["level"]) == ("GOLD", 1.5, "BUY", 2001.0)   # BUY fills at the ask


async def test_positions_written_straight_into_the_fake_are_visible_and_closable(client, fake):
    fake.positions["D9"] = {"epic": "GOLD", "size": 1.0, "direction": "BUY", "level": 2001.0}
    (seen,) = await client.get_positions()
    assert seen["deal_id"] == "D9"
    await client.close_position("D9")
    assert fake.positions == {} and fake.deleted == ["D9"]


async def test_get_positions_keeps_its_flat_shape(client, fake):
    fake.positions["D1"] = {"epic": "GOLD", "size": 2.0, "direction": "SELL", "level": 2000.0}
    (pos,) = await client.get_positions()
    assert set(pos) == {"epic", "size", "direction", "pnl", "open_price", "deal_id", "created_date"}


async def test_state_assigned_directly_is_what_the_client_sees(client, fake):
    fake.quote["GOLD"] = (1000.0, 1001.0)
    assert await client.get_prices("GOLD") == (1001.0, 1000.0)             # client returns (ask, bid)
    fake.markets = {"BTCUSD": "CRYPTOCURRENCIES"}
    assert await client.get_instrument_types() == {"BTCUSD": "CRYPTOCURRENCIES"}
    fake.hours = {}
    assert await client.get_opening_hours("GOLD") is None
    fake.balance = 123.0
    assert (await client.get_account())["balance"]["balance"] == 123.0
    fake.preferences = {"hedgingMode": True, "leverages": {}}
    assert (await client.get_preferences())["hedgingMode"] is True


async def test_failure_switches(client, fake):
    fake.fail_open = True
    with pytest.raises(APIError):
        await client.open_position("GOLD", 1, "BUY")
    fake.fail_open = False
    await client.open_position("GOLD", 1, "BUY")
    fake.fail_close = True
    (deal_id,) = fake.positions
    with pytest.raises(APIError):
        await client.close_position(deal_id)
    fake.positions_failures = 1
    with pytest.raises(APIError):
        await client.get_positions()
    fake.login_ok = False
    fake.expire_session()
    with pytest.raises(AuthError):
        await client.get_accounts()


async def test_login_counter_and_session_expiry(client, fake):
    await client.login()
    assert fake.logins == 1
    fake.expire_session()
    await client.get_accounts()                                              # 401 -> transparent re-login
    assert fake.logins == 2


def test_client_factory_returns_an_httpx_client(fake):
    import httpx
    assert isinstance(fake.client(), httpx.AsyncClient)
