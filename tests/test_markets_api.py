"""``get_markets`` / ``get_market`` / ``get_instrument_types``: filters are sent as the documented query parameters."""
import pytest


async def test_get_markets_without_filter_sends_no_query(fake, client):
    assert {m["epic"] for m in await client.get_markets()} == {"GOLD", "US100"}
    assert [r for r in fake.requests if r[1] == "/api/v1/markets"] == [("GET", "/api/v1/markets", "demo-api-capital.backend-capital.com")]


async def test_get_markets_sends_epics_comma_separated(fake, client):
    seen = []
    original = fake.handler

    def spy(request):
        if request.url.path == "/api/v1/markets":
            seen.append(dict(request.url.params))
        return original(request)

    client._http._transport.handler = spy
    markets = await client.get_markets(epics=["GOLD", "US100"])
    assert seen == [{"epics": "GOLD,US100"}] and len(markets) == 2


async def test_get_markets_search_term_wins_over_epics(fake, client):
    assert [m["epic"] for m in await client.get_markets(search_term="gol", epics=["US100"])] == ["GOLD"]


async def test_get_markets_epics_filter_and_unknown_epic(fake, client):
    assert [m["epic"] for m in await client.get_markets(epics=["US100"])] == ["US100"]
    assert await client.get_markets(epics=["NOPE"]) == []


async def test_get_instrument_types_for_given_epics(fake, client):
    assert await client.get_instrument_types(["US100"]) == {"US100": "INDICES"}


async def test_get_market_returns_instrument_rules_and_snapshot(fake, client):
    fake.set_quote("GOLD", 1999.0, 2001.0)
    market = await client.get_market("GOLD")
    assert market["instrument"]["epic"] == "GOLD" and market["snapshot"]["bid"] == 1999.0
    assert "minDealSize" in market["dealingRules"]


async def test_get_markets_by_epics_flattens_market_details(fake, client):
    """Capital.com answers ``epics`` with ``marketDetails`` (not ``markets``): the client hides the difference."""
    fake.set_quote("GOLD", 1999.0, 2001.0)
    [gold] = await client.get_markets(epics=["GOLD"])
    assert (gold["epic"], gold["instrumentType"], gold["bid"], gold["offer"], gold["marketStatus"]) == ("GOLD", "COMMODITIES", 1999.0, 2001.0, "TRADEABLE")
    assert "minDealSize" in gold["dealingRules"] and gold["instrument"]["epic"] == "GOLD"


async def test_get_markets_search_returns_summaries(fake, client):
    [gold] = await client.get_markets(search_term="GOL")
    assert gold["epic"] == "GOLD" and gold["instrumentType"] == "COMMODITIES"


async def test_get_markets_by_epics_both_response_keys_are_merged(client, monkeypatch):
    async def fake_request(method, path, **kw):
        import httpx
        return httpx.Response(200, json={"markets": [{"epic": "A", "instrumentType": "X"}],
                                         "marketDetails": [{"instrument": {"epic": "B", "type": "Y"}, "snapshot": {}}]})
    monkeypatch.setattr(client, "_request", fake_request)
    assert [(m["epic"], m["instrumentType"]) for m in await client.get_markets()] == [("A", "X"), ("B", "Y")]


async def test_unfiltered_markets_can_fail_like_the_demo_server(fake, client):
    from capital_hook import APIError
    fake.unfiltered_markets_fails = True
    with pytest.raises(APIError) as exc:
        await client.get_markets()
    assert exc.value.status_code == 500
    assert [m["epic"] for m in await client.get_markets(epics=["GOLD"])] == ["GOLD"]      # filtered calls keep working


async def test_the_fake_logs_the_query_parameters_it_receives(fake, client):
    await client.get_markets(epics=["GOLD", "US100"])
    await client.get_markets(search_term="GOL")
    assert [q for m, path, q in fake.queries if path == "/api/v1/markets"] == [{"epics": "GOLD,US100"}, {"searchTerm": "GOL"}]
