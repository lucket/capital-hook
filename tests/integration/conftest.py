"""Fixtures of the opt-in DEMO suite. Read `tests/demo_harness.py` first: it holds the safety rules.

Run:  CAPITAL_DEMO_IDENTITY=... CAPITAL_DEMO_PASSWORD=... CAPITAL_DEMO_API_KEY=... CAPITAL_DEMO_ALLOW=1 pytest -m demo -v
"""
import os
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

import demo_harness as h
from capital_hook import DEMO_HOST, CapitalClient, TradeMode
from capital_hook.testing import FakeCapital


@pytest.fixture(scope="session")
def demo_gate():
    reason = h.missing_requirements()
    if reason:
        pytest.skip(reason)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def recorder():
    rec = h.Recorder()
    yield rec
    rec.dump(h.OBSERVED_PATH, h.SHAPES_PATH if os.environ.get("CAPITAL_DEMO_WRITE_SHAPES") == "1" else None)


@pytest.fixture(scope="session")
def journal():
    return h.Journal()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def demo_fake(demo_gate):
    """Only in the harness's own dry run: the fake that stands in for the demo account."""
    if not h.dry_run():
        yield None
        return
    fake = FakeCapital()
    fake.markets = {"GOLD": "COMMODITIES", "US100": "INDICES"}
    yield fake


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def demo_client(demo_gate, recorder, journal, demo_fake):
    throttle = h.Throttle(min_interval=0.0, session_interval=0.0) if demo_fake else h.Throttle()
    hooks = {"request": [throttle.on_request], "response": [recorder.on_response]}
    if demo_fake:
        http = httpx.AsyncClient(transport=httpx.MockTransport(demo_fake.handler), event_hooks=hooks)
        identity, password, api_key = "dry-run", "dry-run", "dry-run"
    else:
        http = httpx.AsyncClient(timeout=30, event_hooks=hooks)
        identity, password, api_key = (os.environ[name] for name in h.REQUIRED_ENV)
    client = CapitalClient(identity, password, api_key, mode=TradeMode.DEMO, http=http)
    # hard guard: this suite can only ever talk to the demo environment
    assert client.mode is TradeMode.DEMO and client.host == DEMO_HOST and "demo" in client.host
    await client.login()
    swept = await h.sweep(client, journal)
    recorder.note("swept_from_previous_run", swept)
    yield client
    await h.sweep(client, journal)
    try:
        await client.logout()
    finally:
        await http.aclose()


@dataclass
class Market:
    epic: str
    min_size: float
    bid: float
    ask: float
    decimals: int
    instrument_type: str

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def demo_market(demo_client, recorder):
    """The one market the suite trades. It must be tradeable and completely untouched by the user."""
    epic = os.environ.get("CAPITAL_DEMO_EPIC") or ("GOLD" if h.dry_run() else "BTCUSD")
    market = await demo_client.get_market(epic)
    snapshot, rules = market["snapshot"], market["dealingRules"]
    if snapshot.get("marketStatus") != "TRADEABLE":
        pytest.skip(f"{epic} is not tradeable right now (marketStatus={snapshot.get('marketStatus')})")
    busy = [p for p in await demo_client.get_positions() if p["epic"] == epic] + [o for o in await demo_client.get_working_orders() if o["epic"] == epic]
    if busy:
        pytest.skip(f"{epic} already has {len(busy)} position(s)/working order(s) on the demo account: refusing to trade it "
                    "(with hedging off, a new deal could merge into them). Choose another market with CAPITAL_DEMO_EPIC.")
    found = Market(epic=epic, min_size=float(rules["minDealSize"]["value"]), bid=float(snapshot["bid"]), ask=float(snapshot["offer"]),
                   decimals=int(snapshot.get("decimalPlacesFactor", 2)), instrument_type=market["instrument"].get("type", ""))
    recorder.note("market", {"instrument_type": found.instrument_type, "min_deal_size": found.min_size, "decimals": found.decimals,
                             "dealing_rules": {k: v for k, v in rules.items() if isinstance(v, dict)}})
    return found


@pytest_asyncio.fixture(loop_scope="session")
async def tracker(demo_client, demo_market, journal):
    t = h.Tracker(demo_client, journal, epic=demo_market.epic)
    yield t
    await t.cleanup()
