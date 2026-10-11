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
