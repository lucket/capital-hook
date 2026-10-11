"""PriceStream against a real local WebSocket server speaking the Capital.com message shapes."""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from capital_hook import CapitalClient, PriceStream, Quote, StreamError
from capital_hook.testing import FakeCapital


class FakeStreamServer:
    def __init__(self):
        self.messages = []        # every JSON message received
        self.connections = []     # server-side connection objects, in connect order
        self.drop_after_subscribe = 0   # close this many connections right after their first subscribe

    async def handler(self, ws):
        self.connections.append(ws)
        async for raw in ws:
            msg = json.loads(raw)
            self.messages.append(msg)
            if msg["destination"] == "marketData.subscribe":
                for epic in msg["payload"]["epics"]:
                    await ws.send(json.dumps({"status": "OK", "destination": "marketData.subscribe", "payload": {"subscriptions": {epic: "PROCESSED"}}}))
                    await ws.send(json.dumps({"status": "OK", "destination": "quote", "payload": {"epic": epic, "bid": 10.0, "ofr": 10.5, "timestamp": 123}}))
                if self.drop_after_subscribe:
                    self.drop_after_subscribe -= 1
                    await ws.close()
            elif msg["destination"] == "ping":
                await ws.send(json.dumps({"status": "OK", "destination": "ping", "payload": {}}))

    def subs(self):
        return [m["payload"]["epics"][0] for m in self.messages if m["destination"] == "marketData.subscribe"]


@pytest.fixture
async def server():
    srv = FakeStreamServer()
    async with serve(srv.handler, "127.0.0.1", 0) as s:
        srv.url = f"ws://127.0.0.1:{s.sockets[0].getsockname()[1]}"
        yield srv


@pytest.fixture
async def make_stream(server):
    quotes, streams = [], []
    fake = FakeCapital()
    client = CapitalClient("me", "pw", "key", http=fake.client())
    await client.login()

    def make(**kw):
        kw.setdefault("reconnect_delay", 0.05)
        kw.setdefault("ping_every", None)
        stream = PriceStream(client, quotes.append, url=server.url, **kw)
        streams.append(stream)
        return stream

    make.quotes, make.fake = quotes, fake
    yield make
    for s in streams:
        await s.aclose()


async def until(cond, timeout=3.0):
    end = asyncio.get_event_loop().time() + timeout
    while not cond():
        assert asyncio.get_event_loop().time() < end, "condition not reached in time"
        await asyncio.sleep(0.01)


async def test_subscribe_delivers_rest_seed_then_live_quotes(make_stream, server):
    stream = make_stream()
    await stream.subscribe("GOLD")
    assert make_stream.quotes[0] == Quote(epic="GOLD", bid=2000.0, ask=2001.0, timestamp=0)      # REST seed, before any tick
    await until(lambda: Quote(epic="GOLD", bid=10.0, ask=10.5, timestamp=123) in make_stream.quotes)
    assert stream.subscribed_epics == {"GOLD"}
    m = server.messages[0]
    assert m["cst"] == "cst-1" and m["securityToken"] == "sec-1"            # session tokens come from the client


async def test_subscribing_twice_is_a_noop(make_stream, server):
    stream = make_stream()
    await stream.subscribe("GOLD")
    await stream.subscribe("GOLD")
    await until(lambda: server.subs() == ["GOLD"])
    await asyncio.sleep(0.1)
    assert server.subs() == ["GOLD"]


async def test_epics_are_spread_over_connections_of_max_size(make_stream, server):
    stream = make_stream(max_per_connection=2, seed_from_rest=False)
    for epic in ("A", "B", "C", "D", "E"):
        await stream.subscribe(epic)
    await until(lambda: len(server.subs()) == 5)
    assert len(server.connections) == 3 and stream.subscribed_epics == {"A", "B", "C", "D", "E"}


async def test_reconnects_and_resubscribes_after_the_server_drops_the_connection(make_stream, server):
    server.drop_after_subscribe = 1
    stream = make_stream(seed_from_rest=False)
    await stream.subscribe("GOLD")
    await until(lambda: len(server.connections) == 2 and server.subs() == ["GOLD", "GOLD"])
    await until(lambda: len(make_stream.quotes) == 2)                       # a quote arrived on each connection


async def test_unsubscribe(make_stream, server):
    stream = make_stream(seed_from_rest=False)
    await stream.subscribe("GOLD")
    await stream.unsubscribe("GOLD")
    await until(lambda: any(m["destination"] == "marketData.unsubscribe" for m in server.messages))
    assert stream.subscribed_epics == set()


async def test_ping_all_sends_keepalives(make_stream, server):
    stream = make_stream(seed_from_rest=False)
    await stream.subscribe("GOLD")
    await stream.ping_all()
    await until(lambda: any(m["destination"] == "ping" for m in server.messages))


async def test_automatic_pings(make_stream, server):
    stream = make_stream(seed_from_rest=False, ping_every=0.05)
    await stream.subscribe("GOLD")
    await until(lambda: sum(m["destination"] == "ping" for m in server.messages) >= 2)


async def test_async_callbacks_are_awaited(make_stream, server):
    got = []

    async def on_quote(q):
        got.append(q)

    stream = PriceStream(make_stream.fake and CapitalClient("me", "pw", "k", http=make_stream.fake.client()), on_quote,
                         url=server.url, ping_every=None, seed_from_rest=False)
    await stream.client.login()
    try:
        await stream.subscribe("GOLD")
        await until(lambda: got)
    finally:
        await stream.aclose()


async def test_unreachable_stream_raises_stream_error(make_stream):
    stream = make_stream(connect_timeout=0.3, seed_from_rest=False)
    stream.url = "ws://127.0.0.1:9"                      # nothing listens here
    with pytest.raises(StreamError):
        await stream.subscribe("GOLD")


# ---------------------------------------------------------------- defects found by reading the code (each failed before its fix)
async def test_concurrent_subscribes_never_exceed_the_per_connection_limit(make_stream, server):
    stream = make_stream(max_per_connection=40, seed_from_rest=False)
    await asyncio.gather(*(stream.subscribe(f"E{i:03d}") for i in range(100)))
    sizes = sorted(len(c.epics) for c in stream._connections)
    assert sizes == [20, 40, 40] and len(server.connections) == 3
    await until(lambda: len(server.subs()) == 100)
    assert sorted(server.subs()) == sorted(f"E{i:03d}" for i in range(100))             # every epic once, none twice


async def test_concurrent_subscribes_to_the_same_epic_send_one_request(make_stream, server):
    stream = make_stream(seed_from_rest=False)
    await asyncio.gather(*(stream.subscribe("GOLD") for _ in range(5)))
    await asyncio.sleep(0.1)
    assert server.subs() == ["GOLD"] and len(stream._connections) == 1


async def test_a_transport_error_while_seeding_does_not_fail_the_subscription(make_stream, server, monkeypatch):
    import httpx
    stream = make_stream()

    async def broken(epic):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(stream.client, "get_prices", broken)
    await stream.subscribe("GOLD")                                   # used to raise after GOLD had already been subscribed
    assert stream.subscribed_epics == {"GOLD"}
    await until(lambda: Quote(epic="GOLD", bid=10.0, ask=10.5, timestamp=123) in make_stream.quotes)


async def test_a_failed_subscribe_leaves_no_connection_behind(make_stream):
    stream = make_stream(connect_timeout=0.3, seed_from_rest=False)
    stream.url = "ws://127.0.0.1:9"                      # nothing listens here
    with pytest.raises(StreamError):
        await stream.subscribe("GOLD")
    assert stream._connections == [] and stream.subscribed_epics == set()
    with pytest.raises(StreamError):                                 # and a retry starts clean instead of piling up connections
        await stream.subscribe("GOLD")
    assert stream._connections == []


async def test_reconnect_uses_a_fresh_session_when_the_old_one_expired(make_stream, server):
    stream = make_stream(seed_from_rest=False)
    server.drop_after_subscribe = 1
    make_stream.fake.expire_session()                                # the REST session is dead, but the client does not know yet
    await stream.subscribe("GOLD")
    await until(lambda: len(server.subs()) == 2)                     # resubscribed after the reconnect
    first, second = [m for m in server.messages if m["destination"] == "marketData.subscribe"]
    assert first["cst"] != second["cst"] and make_stream.fake.logins == 2


# ---------------------------------------------------------------- aclose / subscribed_epics
async def test_subscribed_epics_lists_what_was_subscribed_across_connections(make_stream, server):
    stream = make_stream(max_per_connection=1, seed_from_rest=False)
    assert stream.subscribed_epics == set()
    await stream.subscribe("A")
    await stream.subscribe("B")
    assert stream.subscribed_epics == {"A", "B"} and len(stream._connections) == 2
    await stream.unsubscribe("A")
    assert stream.subscribed_epics == {"B"}


async def test_aclose_closes_every_connection_and_forgets_the_epics(make_stream, server):
    stream = make_stream(max_per_connection=1, seed_from_rest=False)
    await stream.subscribe("A")
    await stream.subscribe("B")
    await stream.aclose()
    assert stream._connections == [] and stream.subscribed_epics == set()
    await until(lambda: len(server.connections) == 2 and all(c.close_code is not None for c in server.connections))


async def test_aclose_is_safe_when_nothing_was_ever_subscribed(make_stream):
    await make_stream().aclose()


# ---------------------------------------------------------------- bad input and failures never kill the stream
async def test_a_malformed_message_is_logged_and_the_stream_keeps_working(make_stream, server, caplog):
    stream = make_stream(seed_from_rest=False)
    await stream.subscribe("GOLD")
    await until(lambda: server.connections)
    await server.connections[0].send("this is not json")
    await server.connections[0].send(json.dumps({"destination": "quote", "payload": {"epic": "GOLD"}}))          # a quote without prices
    await server.connections[0].send(json.dumps({"status": "OK", "destination": "quote", "payload": {"epic": "GOLD", "bid": 1.0, "ofr": 2.0, "timestamp": 5}}))
    await until(lambda: Quote(epic="GOLD", bid=1.0, ask=2.0, timestamp=5) in make_stream.quotes)
    assert caplog.text.count("could not process stream message") == 2


async def test_a_rejected_stream_message_is_logged_not_raised(make_stream, server, caplog):
    stream = make_stream(seed_from_rest=False)
    await stream.subscribe("GOLD")
    await until(lambda: server.connections)
    await server.connections[0].send(json.dumps({"status": "ERROR", "destination": "marketData.subscribe", "payload": {"errorCode": "bad session"}}))
    await until(lambda: "stream message rejected" in caplog.text)
    assert stream.subscribed_epics == {"GOLD"}


async def test_a_failing_quote_callback_does_not_stop_later_quotes(make_stream, server, caplog):
    seen = []

    def flaky(quote):
        seen.append(quote)
        if len(seen) == 1:
            raise RuntimeError("callback bug")

    client = CapitalClient("me", "pw", "key", http=make_stream.fake.client())
    await client.login()
    stream = PriceStream(client, flaky, url=server.url, reconnect_delay=0.05, ping_every=None, seed_from_rest=False)
    try:
        await stream.subscribe("GOLD")
        await until(lambda: len(seen) >= 1)
        await server.connections[0].send(json.dumps({"status": "OK", "destination": "quote", "payload": {"epic": "GOLD", "bid": 3.0, "ofr": 4.0, "timestamp": 6}}))
        await until(lambda: any(q.bid == 3.0 for q in seen))
        assert "callback bug" in caplog.text
    finally:
        await stream.aclose()


async def test_ping_all_logs_a_failing_connection_and_still_pings_the_others(make_stream, server, caplog):
    stream = make_stream(max_per_connection=1, seed_from_rest=False)
    await stream.subscribe("A")
    await stream.subscribe("B")
    broken, healthy = stream._connections

    async def boom():
        raise RuntimeError("socket gone")

    broken.ping = boom
    await stream.ping_all()
    await until(lambda: any(m["destination"] == "ping" for m in server.messages))
    assert "ping failed" in caplog.text


async def test_the_automatic_ping_loop_survives_a_failed_ping(make_stream, server, caplog):
    stream = make_stream(ping_every=0.05, seed_from_rest=False)
    await stream.subscribe("GOLD")
    conn = stream._connections[0]
    original = conn.ping
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first ping fails")
        await original()

    conn.ping = flaky
    await until(lambda: len(calls) >= 3)
    assert "ping failed" in caplog.text


async def test_reconnect_delay_doubles_up_to_a_cap_of_30_seconds(make_stream, monkeypatch):
    stream = make_stream(connect_timeout=0.5, reconnect_delay=10.0, seed_from_rest=False)
    stream.url = "ws://127.0.0.1:9"
    sleeps, real_sleep = [], asyncio.sleep

    async def recording(delay, *a, **kw):
        if delay >= 1:
            sleeps.append(delay)
            delay = 0.001
        await real_sleep(delay, *a, **kw)

    monkeypatch.setattr("capital_hook.streaming.asyncio.sleep", recording)
    with pytest.raises(StreamError):
        await stream.subscribe("GOLD")
    assert sleeps[:4] == [10.0, 20.0, 30.0, 30.0], sleeps[:6]   # the first retry waits the configured delay (it used to wait twice that)


async def test_a_client_that_is_not_logged_in_yet_gets_a_session_on_the_first_reconnect(server):
    fake = FakeCapital()
    client = CapitalClient("me", "pw", "key", http=fake.client())              # never logged in
    stream = PriceStream(client, lambda q: None, url=server.url, reconnect_delay=0.05, ping_every=None, seed_from_rest=False)
    try:
        await stream.subscribe("GOLD")                                          # attempt 1 fails (no session); the retry logs in and connects
        assert client.authenticated and stream.subscribed_epics == {"GOLD"}
        await until(lambda: server.subs() == ["GOLD"])
    finally:
        await stream.aclose()
