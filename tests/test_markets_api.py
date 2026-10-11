"""``get_markets`` / ``get_market`` / ``get_instrument_types``: filters are sent as the documented query parameters."""


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
