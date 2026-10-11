import httpx
import pytest

from capital_hook import APIError, AuthError, CapitalClient, TradeDirection, TradeMode
from capital_hook.testing import FakeCapital


@pytest.fixture
def fake():
    return FakeCapital()


@pytest.fixture
def client(fake):
    return CapitalClient("me@example.com", "pw", "api-key", http=fake.client())


async def test_login_stores_session_tokens(client, fake):
    assert not client.authenticated
    await client.login()
    assert client.authenticated and client.session_tokens == ("cst-1", "sec-1")


async def test_login_failure_raises_instead_of_failing_silently(client, fake):
    fake.login_ok = False
    with pytest.raises(AuthError, match="401"):
        await client.login()
    assert not client.authenticated


async def test_login_without_tokens_in_answer_is_an_error():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(AuthError, match="no session tokens"):
        await CapitalClient("a", "b", "c", http=http).login()


async def test_first_call_logs_in_automatically(client, fake):
    assert await client.get_accounts()
    assert fake.logins == 1


async def test_expired_session_is_renewed_once_and_the_call_succeeds(client, fake):
    await client.get_accounts()
    fake.expire_session()
    assert await client.get_accounts()
    assert fake.logins == 2


async def test_relogin_does_not_loop_when_credentials_are_bad(client, fake):
    await client.get_accounts()
    fake.expire_session()
    fake.login_ok = False
    with pytest.raises(AuthError):
        await client.get_accounts()
    assert fake.logins == 2


async def test_host_follows_mode_and_set_mode_logs_in_again(client, fake):
    await client.get_accounts()
    assert fake.requests[-1][2] == "demo-api-capital.backend-capital.com"
    await client.set_mode(TradeMode.LIVE)
    assert client.mode == TradeMode.LIVE and fake.logins == 2
    await client.get_accounts()
    assert fake.requests[-1][2] == "api-capital.backend-capital.com"


async def test_open_find_and_close_a_position(client, fake):
    reference = await client.open_position("GOLD", 1.5, TradeDirection.BUY)
    assert reference.startswith("o_")
    deal_id = await client.find_deal_id("GOLD", 1.5, "BUY", delay=0)
    assert deal_id and (await client.get_positions())[0] == {
        "epic": "GOLD", "size": 1.5, "direction": "BUY", "pnl": 0.0, "open_price": 2001.0,
        "deal_id": deal_id, "created_date": "2026-07-12T10:00:00"}
    assert await client.find_deal_id("GOLD", 1.5, "BUY", exclude=[deal_id], attempts=2, delay=0) is None
    assert await client.find_deal_id("GOLD", 9, "BUY", attempts=1) is None
    assert (await client.close_position(deal_id))["dealReference"] == f"c_{deal_id}"
    assert fake.positions == {}


async def test_api_errors_carry_status_and_code(client, fake):
    with pytest.raises(APIError) as err:
        await client.close_position("nope")
    assert err.value.status_code == 404 and err.value.error_code == "error.not-found"
    fake.fail_open = True
    with pytest.raises(APIError) as err:
        await client.open_position("GOLD", 1, "BUY")
    assert err.value.status_code == 400


async def test_markets_prices_hours_account(client, fake):
    assert await client.get_instrument_types() == {"GOLD": "COMMODITIES", "US100": "INDICES"}
    assert await client.get_prices("GOLD") == (2001.0, 2000.0)          # (ask, bid)
    assert (await client.get_opening_hours("GOLD"))["fri"] == ["00:00 - 21:55"]
    assert (await client.get_account())["balance"]["balance"] == 10000.0


async def test_set_preferences_keeps_what_you_do_not_pass(client, fake):
    await client.set_preferences(hedging_mode=True)
    assert fake.preferences["hedgingMode"] is True and "COMMODITIES" in fake.preferences["leverages"]
    await client.set_preferences(leverages={"INDICES": {"current": 5}})
    assert fake.preferences["leverages"] == {"INDICES": {"current": 5}} and fake.preferences["hedgingMode"] is True


async def test_prices_without_quote_is_an_error(client, fake):
    fake.quote["GOLD"] = (0, 0)
    from capital_hook import CapitalError
    with pytest.raises(CapitalError, match="no ask/bid"):
        await client.get_prices("GOLD")


async def test_find_deal_id_retries_through_transient_errors(client, fake):
    await client.open_position("GOLD", 1, "BUY")
    fake.positions_failures = 2
    assert await client.find_deal_id("GOLD", 1, "BUY", attempts=4, delay=0)


async def test_find_deal_id_raises_if_the_last_attempt_also_failed(client, fake):
    await client.open_position("GOLD", 1, "BUY")
    fake.positions_failures = 5
    with pytest.raises(APIError):
        await client.find_deal_id("GOLD", 1, "BUY", attempts=3, delay=0)


async def test_close_can_be_made_to_fail(client, fake):
    await client.open_position("GOLD", 1, "BUY")
    deal_id = await client.find_deal_id("GOLD", 1, "BUY", delay=0)
    fake.fail_close = True
    with pytest.raises(APIError) as err:
        await client.close_position(deal_id)
    assert err.value.status_code == 500 and deal_id in fake.positions
    fake.fail_close = False
    await client.close_position(deal_id)
    assert fake.positions == {}
