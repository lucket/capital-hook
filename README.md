# capital-hook

Async Python client and live price streaming for the [Capital.com](https://capital.com) REST and WebSocket API.

It is a **library**: no web server, no database, no global state, no `.env` handling. Applications
(for example [`tradingserver`](https://github.com/lucket/tradingserver), which turns TradingView alerts into trades)
build on it and keep their own configuration and storage.

- `CapitalClient` – login, accounts, markets, prices, positions (open / confirm / update / close), working orders (create / update / cancel),
  account activity, preferences. DEMO and LIVE.
- `PriceStream` – live quotes over WebSocket, spread over connections of 40 epics, automatic reconnect + resubscribe, keep-alive pings.
- `capital_hook.hours` – pure helpers: *is the market closed / closing for the day / closing for the week*.
- `capital_hook.testing.FakeCapital` – an in-memory fake of the API (an `httpx.MockTransport` handler) for testing your own code.

Requires Python 3.10+, `httpx`, `websockets`.

## Install

```bash
pip install -e /path/to/capital-hook            # local development
pip install "capital-hook @ git+https://github.com/lucket/capital-hook@<tag>"
```

## Quick start

```python
import asyncio
from capital_hook import CapitalClient, PriceStream, TradeDirection, TradeMode

async def main():
    client = CapitalClient("me@example.com", "password", "api-key", mode=TradeMode.DEMO)
    await client.login()                                   # raises AuthError if rejected

    print((await client.get_account())["balance"])
    ask, bid = await client.get_prices("GOLD")

    await client.open_position("GOLD", 1.0, TradeDirection.BUY)
    deal_id = await client.find_deal_id("GOLD", 1.0, TradeDirection.BUY)   # the open call only returns a reference
    await client.close_position(deal_id)

    stream = PriceStream(client, on_quote=lambda q: print(q.epic, q.bid, q.ask))
    await stream.subscribe("GOLD")
    await asyncio.sleep(30)
    await stream.aclose()
    await client.aclose()

asyncio.run(main())
```

## Behaviour worth knowing

- **Opening a position returns only a reference.** `open_position` / `create_working_order` answer 200 with a `dealReference`
  even when the deal is later rejected. Call `await client.confirm(reference)`: it returns a `Confirmation` (`accepted` / `rejected`).
  `confirmation.deal_id` is the id of the *order*; the id of the position it opened (the one `get_positions`, `close_position` and
  `update_position` use) is `confirmation.position_deal_id`.
- **After a timeout, reconcile instead of retrying.** If an open times out the server may still have executed it; look at
  `get_positions()` (or `find_deal_id`) before sending it again.
- **Market lists need a filter.** `get_markets()` with no argument is answered with HTTP 500 by Capital.com; use
  `get_markets(search_term=...)` or `get_markets(epics=[...])` (at most 50), or `get_instrument_types(epics)`.
- **Errors are raised, never swallowed.** Non-success answers raise `APIError` (with `status_code`, `error_code`);
  a failed login raises `AuthError`. Retry policy belongs to the caller.
- **Sessions renew themselves.** If a request is rejected with 401 the client logs in once more and repeats it.
- **Switching environment:** `await client.set_mode(TradeMode.LIVE)` drops the session, switches host and logs in again.
  A client talks to one environment at a time – use two clients for DEMO and LIVE side by side.
- **`PriceStream.subscribe(epic)`** returns once the stream is connected and the epic requested, and (by default)
  delivers the latest REST price to `on_quote` immediately so a price exists before the first tick.
  `StreamError` is raised if the stream cannot be reached within `connect_timeout`.
- **Pass your own `httpx.AsyncClient`** (`http=`) to share a connection pool or inject a mock transport.
- Logging goes to the standard `logging` loggers `capital_hook.client` and `capital_hook.stream`.

## Opening hours

```python
from capital_hook import is_market_closed, is_closing_for_day, is_closing_for_week

hours = await client.get_opening_hours("GOLD")      # {"mon": ["00:00 - 21:55"], ...}  (UTC)
is_closing_for_day(hours, minutes=2)                # last session over, or ends within 2 minutes
```

## Testing your code

```python
from capital_hook import CapitalClient
from capital_hook.testing import FakeCapital

fake = FakeCapital()
client = CapitalClient("me", "pw", "key", http=fake.client())
await client.open_position("GOLD", 1, "BUY")
assert len(fake.positions) == 1
fake.expire_session()        # next call gets a 401 -> the client logs in again
fake.fail_open = True        # POST /positions now answers 400
```

`FakeCapital` is a small state machine, not a canned responder: positions carry profit and loss, margin and balance follow, stop and
profit levels trigger when you move the market (`fake.set_quote(...)`), working orders fill and expire (`fake.now` is the clock), opens can be
rejected after the fact, and you can inject faults (`fake.fail_next(...)`, `fake.lose_response_next(...)` - the request executes but the
client sees a timeout). `fake.check_invariants()` verifies its own books. Anything it assumes that Capital.com has not been observed to do is
listed in `capital_hook.testing.ASSUMPTIONS`.

## Development

```bash
python -m venv venv && ./venv/bin/pip install -e ".[dev]"
./venv/bin/python -m pytest                       # offline, a few seconds
./venv/bin/python -m pytest --cov                 # with branch coverage; fails below 95%
```

### What the tests check

- every public function and property has a test named after it (`tests/test_every_function_is_tested.py` fails when one is added without);
- trade lifecycles - open, confirm, price move, close, cancel, fill, expire, session expiry, server errors, lost responses - compare three
  views after every step: what the client reports, what the fake holds, and an independent expectation (`tests/test_lifecycle.py`);
- a randomized Hypothesis state machine does the same over arbitrary sequences of operations (`tests/test_stateful.py`);
- the fake may not return a field the real API does not have (`tests/test_shape_contract.py`, using `tests/fixtures/shapes.json`).

### Tests against the real DEMO account (opt-in)

`pytest -m demo` trades the smallest allowed size on a Capital.com **DEMO** account and records what the real API does. It is skipped
unless you set `CAPITAL_DEMO_IDENTITY`, `CAPITAL_DEMO_PASSWORD`, `CAPITAL_DEMO_API_KEY` **and** `CAPITAL_DEMO_ALLOW=1`. It only ever uses the
DEMO host, refuses to trade an epic that already has positions or orders (default `BTCUSD`, override with `CAPITAL_DEMO_EPIC`), journals every
deal it creates so a crashed run is cleaned up on the next start, and writes `tests/integration/observed.json` (no credentials). To look at or
clean up what is left on the test epic: `python tests/integration/demo_cleanup.py [--yes]`.

Not affiliated with Capital.com. Trading CFDs carries a high risk of losing money.
