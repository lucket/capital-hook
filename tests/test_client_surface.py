"""Session, account and read-only functions of CapitalClient, plus Confirmation and FakeCapital's own helpers."""
import httpx
import pytest

from capital_hook import DEMO_HOST, LIVE_HOST, APIError, AuthError, CapitalClient, CapitalError, Confirmation, TradeMode


# ---------------------------------------------------------------- environment and session
def test_host_for_maps_each_mode_to_its_own_host():
    assert CapitalClient.host_for(TradeMode.DEMO) == DEMO_HOST and CapitalClient.host_for(TradeMode.LIVE) == LIVE_HOST
    assert DEMO_HOST != LIVE_HOST and "demo" in DEMO_HOST and "demo" not in LIVE_HOST


def test_mode_defaults_to_demo_and_decides_the_host():
    assert CapitalClient("a", "b", "c").mode is TradeMode.DEMO and CapitalClient("a", "b", "c").host == DEMO_HOST
    live = CapitalClient("a", "b", "c", mode=TradeMode.LIVE)
    assert live.mode is TradeMode.LIVE and live.host == LIVE_HOST


async def test_authenticated_follows_login_and_logout(client, fake):
    assert client.authenticated is False
    await client.login()
    assert client.authenticated is True
    await client.logout()
    assert client.authenticated is False


async def test_session_tokens_raise_when_logged_out_and_return_the_pair_after_login(client):
    with pytest.raises(AuthError):
        client.session_tokens
    await client.login()
    cst, token = client.session_tokens
    assert cst and token


async def test_set_mode_switches_host_drops_the_session_and_logs_in_on_the_new_one(client, fake):
    await client.login()
    old = client.session_tokens
    await client.set_mode(TradeMode.LIVE)
    assert client.mode is TradeMode.LIVE and client.authenticated and client.session_tokens != old
    assert fake.requests[-1][2] == LIVE_HOST.removeprefix("https://") and fake.logins == 2


async def test_set_mode_without_login_leaves_the_client_logged_out(client, fake):
    await client.login()
    await client.set_mode(TradeMode.LIVE, login=False)
    assert client.mode is TradeMode.LIVE and not client.authenticated and fake.logins == 1


async def test_set_mode_to_the_same_mode_keeps_the_session(client, fake):
    await client.login()
    tokens = client.session_tokens
    await client.set_mode(TradeMode.DEMO)
    assert client.session_tokens == tokens and fake.logins == 1


async def test_set_mode_surfaces_a_failed_login(client, fake):
    await client.login()
    fake.login_ok = False
    with pytest.raises(AuthError):
        await client.set_mode(TradeMode.LIVE)
    assert client.mode is TradeMode.LIVE and not client.authenticated          # the old session is gone, not silently kept


async def test_aclose_closes_the_http_client_it_created():
    client = CapitalClient("a", "b", "c")
    await client.aclose()
    assert client._http.is_closed


async def test_aclose_leaves_an_injected_http_client_open(fake):
    http = fake.client()
    await CapitalClient("a", "b", "c", http=http).aclose()
    assert not http.is_closed
    await http.aclose()


# ---------------------------------------------------------------- account
async def test_get_accounts_lists_every_account(client, fake):
    accounts = await client.get_accounts()
    assert len(accounts) == 1 and accounts[0]["balance"]["balance"] == 10000.0


async def test_get_account_returns_the_first_account(client, fake):
    account = await client.get_account()
    assert account["balance"]["balance"] == 10000.0 and {"available", "profitLoss", "deposit"} <= set(account["balance"])


async def test_get_account_raises_when_there_is_no_account(make_client):
    client = make_client(lambda r: httpx.Response(200, json={"accounts": []}) if r.url.path == "/api/v1/accounts" else None)
    with pytest.raises(CapitalError, match="no accounts"):
        await client.get_account()


async def test_get_accounts_surfaces_server_errors(make_client):
    client = make_client(lambda r: httpx.Response(500, json={"errorCode": "error.server"}) if r.url.path == "/api/v1/accounts" else None)
    with pytest.raises(APIError) as exc:
        await client.get_accounts()
    assert (exc.value.status_code, exc.value.error_code) == (500, "error.server")


async def test_get_preferences_returns_hedging_mode_and_leverages(client, fake):
    preferences = await client.get_preferences()
    assert preferences["hedgingMode"] is False and preferences["leverages"]["COMMODITIES"]["current"] == 20


# ---------------------------------------------------------------- markets and prices
async def test_get_prices_returns_ask_first_then_bid(client, fake):
    fake.set_quote("GOLD", 1999.0, 2001.0)                       # the fake stores (bid, ask) ...
    assert await client.get_prices("GOLD") == (2001.0, 1999.0)   # ... the client returns (ask, bid)


async def test_get_prices_raises_when_the_market_has_no_price(client, fake):
    with pytest.raises(CapitalError, match="US100"):             # US100 exists in the fake's market list but has no quote
        await client.get_prices("US100")


async def test_get_prices_for_an_unknown_epic_is_a_404(client):
    with pytest.raises(APIError) as exc:
        await client.get_prices("NOPE")
    assert exc.value.status_code == 404


async def test_get_opening_hours_returns_the_published_schedule(client, fake):
    fake.hours = {"mon": ["00:00 - 21:55"], "sat": []}
    assert await client.get_opening_hours("GOLD") == {"mon": ["00:00 - 21:55"], "sat": []}


async def test_get_opening_hours_is_none_when_nothing_is_published(client, fake):
    fake.hours = {}
    assert await client.get_opening_hours("GOLD") is None


# ---------------------------------------------------------------- positions
async def test_close_position_sends_a_delete_and_returns_the_close_reference(client, fake):
    reference = await client.open_position("GOLD", 1, "BUY")
    deal_id = (await client.confirm(reference)).position_deal_id
    answer = await client.close_position(deal_id)
    assert answer == {"dealReference": f"p_{deal_id}"} and ("DELETE", f"/api/v1/positions/{deal_id}", DEMO_HOST.removeprefix("https://")) in fake.requests
    assert fake.positions == {}


async def test_close_position_of_an_unknown_deal_raises_404(client):
    with pytest.raises(APIError) as exc:
        await client.close_position("UNKNOWN")
    assert (exc.value.status_code, exc.value.error_code) == (404, "error.not-found.dealId")


# ---------------------------------------------------------------- Confirmation
def sample(**changes):
    data = {"dealReference": "o_1", "dealStatus": "ACCEPTED", "status": "OPEN", "dealId": "ORDER", "epic": "GOLD", "direction": "BUY",
            "size": 2, "level": 2001.5, "affectedDeals": [{"dealId": "POS", "status": "OPENED"}]}
    data.update(changes)
    return data


def test_from_response_reads_every_field_and_keeps_the_raw_answer():
    c = Confirmation.from_response(sample())
    assert (c.deal_reference, c.deal_status, c.status, c.deal_id, c.epic, c.direction) == ("o_1", "ACCEPTED", "OPEN", "ORDER", "GOLD", "BUY")
    assert (c.size, c.level, c.reason) == (2.0, 2001.5, None) and c.affected_deals == ({"dealId": "POS", "status": "OPENED"},) and c.raw["dealId"] == "ORDER"


def test_from_response_tolerates_missing_fields():
    c = Confirmation.from_response({})
    assert (c.deal_reference, c.deal_status, c.deal_id, c.size, c.level, c.affected_deals) == ("", "", None, None, None, ())


def test_accepted_is_true_only_for_an_accepted_deal():
    assert Confirmation.from_response(sample()).accepted
    assert not Confirmation.from_response(sample(dealStatus="REJECTED")).accepted and not Confirmation.from_response({}).accepted


def test_rejected_is_true_only_for_a_rejected_deal():
    assert Confirmation.from_response(sample(dealStatus="REJECTED")).rejected
    assert not Confirmation.from_response(sample()).rejected and not Confirmation.from_response({}).rejected


def test_position_deal_id_is_the_opened_deal_not_the_order():
    c = Confirmation.from_response(sample())
    assert c.position_deal_id == "POS" and c.deal_id == "ORDER"


def test_position_deal_id_is_none_without_an_opened_deal():
    assert Confirmation.from_response(sample(affectedDeals=[])).position_deal_id is None
    assert Confirmation.from_response(sample(affectedDeals=[{"dealId": "X", "status": "CLOSED"}])).position_deal_id is None
    assert Confirmation.from_response(sample(affectedDeals=[{"dealId": "X", "status": "CLOSED"}, {"dealId": "Y", "status": "OPENED"}])).position_deal_id == "Y"


# ---------------------------------------------------------------- FakeCapital helpers
async def test_balance_setter_restarts_the_books(client, fake):
    fake.balance = 500.0
    assert (await client.get_account())["balance"]["balance"] == 500.0 and fake.balance == 500.0
    reference = await client.open_position("GOLD", 0.1, "BUY")
    deal_id = (await client.confirm(reference)).position_deal_id
    fake.set_quote("GOLD", 2010.0, 2011.0)
    await client.close_position(deal_id)
    assert fake.balance == pytest.approx(500.0 + (2010 - 2001) * 0.1)


async def test_expire_session_makes_the_next_request_log_in_again(client, fake):
    await client.login()
    fake.expire_session()
    await client.get_accounts()
    assert fake.logins == 2


async def test_handler_answers_an_unknown_path_with_404_unhandled(client):
    with pytest.raises(APIError) as exc:
        await client._request("GET", "/api/v1/nothing-here")
    assert (exc.value.status_code, exc.value.error_code) == (404, "error.unhandled")


async def test_handler_refuses_requests_without_a_session(fake):
    response = fake.handler(httpx.Request("GET", f"{DEMO_HOST}/api/v1/ping"))
    assert response.status_code == 401


async def test_handler_is_the_transport_behind_client(client, fake):
    assert await client.ping() == "OK" and fake.requests[-1][1] == "/api/v1/ping"


async def test_lose_response_next_expires_after_its_count(client, fake):
    fake.lose_response_next("POST", "/api/v1/positions", times=1)
    with pytest.raises(httpx.ReadTimeout):
        await client.open_position("GOLD", 1, "BUY")
    await client.open_position("GOLD", 1, "BUY")                  # the second request is answered normally
    assert len(fake.positions) == 2


async def test_margin_is_the_sum_of_size_times_level_over_leverage(client, fake):
    assert fake.margin() == 0
    await client.open_position("GOLD", 2, "BUY")                  # 2 x 2001 / 20
    assert fake.margin() == pytest.approx(2 * 2001 / 20)


async def test_set_quote_moves_the_market_and_settles_stops(client, fake):
    reference = await client.open_position("GOLD", 1, "BUY", stop_level=1990)
    await client.confirm(reference)
    fake.set_quote("GOLD", 1980.0, 1981.0)
    assert fake.quote["GOLD"] == (1980.0, 1981.0) and fake.positions == {}


async def test_settle_applies_what_a_direct_quote_change_implies(client, fake):
    reference = await client.open_position("GOLD", 1, "BUY", stop_level=1990)
    await client.confirm(reference)
    fake.quote["GOLD"] = (1980.0, 1981.0)                         # a test that assigns the quote directly ...
    assert len(fake.positions) == 1
    fake.settle()                                                 # ... or any request, applies the consequences
    assert fake.positions == {}


async def test_upl_marks_a_buy_to_the_bid_and_a_sell_to_the_ask(client, fake):
    fake.preferences["hedgingMode"] = True
    buy = (await client.confirm(await client.open_position("GOLD", 2, "BUY"))).position_deal_id
    sell = (await client.confirm(await client.open_position("GOLD", 1, "SELL"))).position_deal_id
    fake.quote["GOLD"] = (2010.0, 2011.0)
    assert fake.upl(buy) == pytest.approx((2010 - 2001) * 2) and fake.upl(sell) == pytest.approx(2000 - 2011)


# ---------------------------------------------------------------- sessions under stress and odd error bodies
async def test_login_failure_with_a_non_json_body_still_reports_the_status(make_client):
    client = make_client(lambda r: httpx.Response(502, text="<html>bad gateway</html>") if r.url.path == "/api/v1/session" else None)
    with pytest.raises(AuthError, match="502"):
        await client.login()


async def test_an_error_with_a_non_json_body_has_no_error_code(make_client):
    client = make_client(lambda r: httpx.Response(503, text="maintenance") if r.url.path == "/api/v1/accounts" else None)
    with pytest.raises(APIError) as exc:
        await client.get_accounts()
    assert exc.value.status_code == 503 and exc.value.error_code is None and "maintenance" in exc.value.body


async def test_concurrent_first_requests_log_in_only_once(client, fake):
    import asyncio
    await asyncio.gather(*(client.get_accounts() for _ in range(6)))
    assert fake.logins == 1


async def test_concurrent_requests_hitting_an_expired_session_log_in_again_once(client, fake):
    import asyncio
    await client.login()
    fake.expire_session()
    await asyncio.gather(*(client.get_accounts() for _ in range(6)))
    assert fake.logins == 2


async def test_a_session_that_keeps_being_rejected_gives_up_after_one_retry(make_client, fake):
    client = make_client(lambda r: httpx.Response(401, json={"errorCode": "error.invalid.session.token"}) if r.url.path == "/api/v1/accounts" else None)
    with pytest.raises(APIError) as exc:
        await client.get_accounts()
    assert exc.value.status_code == 401 and fake.logins == 2          # first login + exactly one re-login, then the error surfaces


async def test_auto_relogin_can_be_turned_off(make_client, fake):
    client = make_client(auto_relogin=False)
    await client.login()
    fake.expire_session()
    with pytest.raises(APIError) as exc:
        await client.get_accounts()
    assert exc.value.status_code == 401 and fake.logins == 1
