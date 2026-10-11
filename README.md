# capital-hook

Async Python client and live price streaming for the [Capital.com](https://capital.com) REST and WebSocket API.

It is a **library**: no web server, no database, no global state, no `.env` handling. Applications
(for example [`tradingserver`](https://github.com/lucket/tradingserver), which turns TradingView alerts into trades)
build on it and keep their own configuration and storage.

- `CapitalClient` – login, accounts, markets, prices, positions (open / find deal id / close), preferences. DEMO and LIVE.
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

## Development

```bash
python -m venv venv && ./venv/bin/pip install -e ".[dev]"
./venv/bin/python -m pytest
```

Not affiliated with Capital.com. Trading CFDs carries a high risk of losing money.
