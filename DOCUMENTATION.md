# Capital Hook — Usage & Feature Documentation

Capital Hook is a self-hosted **FastAPI** service that bridges **TradingView alerts** and the
**Capital.com** trading API. TradingView fires a webhook; Capital Hook opens the position on
Capital.com, then monitors it tick-by-tick against a configurable set of exit rules and closes it
automatically. A password-protected web dashboard shows live balance, open positions, and trade
history.

This document describes what the application actually does, endpoint by endpoint and feature by
feature, based on the current source. For install/quick-start steps see [`README.md`](README.md).

---

## 1. Architecture at a glance

```
TradingView Alert  ──POST──▶  /webhook/trading-view  ──▶  HookedTradeExecution (background task)
                                     │                              │
                              IP whitelist check          open_trade → Capital.com REST
                              epic + dedup check                     │
                                                          monitor loop (every 1.12s)
                                                                     │
                              Capital.com WebSocket ──ticks──▶  memory.market_data
                                                                     │
                                                   exit rule hit → close_trade → save history
Browser ──cookie session──▶  Dashboard / Config / History  ◀── SQLite (trades, positions, bot_config)
```

Key runtime pieces:

| Component | File | Responsibility |
|-----------|------|----------------|
| App entrypoint & auth middleware | [`main.py`](main.py) | FastAPI app, lifespan startup/shutdown, route mounting, session guard |
| Settings & credentials | [`settings.py`](settings.py) | Env config, DEMO/LIVE host selection, shared HTTP session |
| In-memory state | [`memory.py`](memory.py) | Positions, prices, epics, leverage, hooked-trade dedup |
| Trade execution | [`hook_trade.py`](hook_trade.py) | Open a position and run the monitor/exit loop |
| Resume execution | [`resume_trade.py`](resume_trade.py) | Re-attach the monitor loop to positions that survived a restart |
| Recalibration | [`recalibrate.py`](recalibrate.py) | Trailing profit-lock logic |
| Capital.com REST | [`service/capital_api.py`](service/capital_api.py) | Login, positions, markets, account, market-hours logic |
| Capital.com WebSocket | [`service/capital_socket.py`](service/capital_socket.py), [`service/socket_manager.py`](service/socket_manager.py) | Live price streaming, reconnect, 40-epics-per-socket pooling |
| Scheduled jobs | [`job.py`](job.py) | Keep-alive pings, auth refresh, market/hours refresh, resume trades |
| Persistence | [`database.py`](database.py) | SQLite schema + queries |
| Web routes | [`routes/`](routes) | `auth`, `view`, `api`, `webhook` routers |
| Auth primitives | [`auth.py`](auth.py) | HMAC-signed session cookie, password check |

---

## 2. Configuration & environment

Settings are loaded from a `.env` file (see [`.env.example`](.env.example)) via `python-dotenv`.

| Variable | Required | Purpose |
|----------|----------|---------|
| `CAPITAL_IDENTITY` | Yes | Capital.com account identifier (email/username) |
| `CAPITAL_PASSWORD` | Yes | Capital.com account password |
| `CAPITAL_API_KEY` | Yes | Capital.com API key (`X-CAP-API-KEY`) |
| `APP_PASSWORD` | Yes | Password to sign in to the dashboard and `/api/*` |
| `APP_SECRET_KEY` | Recommended | Secret used to sign session cookies. If unset, a random key is generated **per process**, so all sessions reset on every restart. |

Generate a strong secret:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Hosts are selected automatically by trade mode ([`settings.py`](settings.py)):

- **LIVE** → `https://api-capital.backend-capital.com`
- **DEMO** → `https://demo-api-capital.backend-capital.com`
- **Streaming** → `wss://api-streaming-capital.backend-capital.com/connect`

---

## 3. Running the app

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The webhook IP whitelist checks the **transport-level peer address** (`request.client.host`), not
the client-controlled `X-Forwarded-For` header, which is spoofable. Behind a reverse proxy, add
`--proxy-headers --forwarded-allow-ips=<proxy-ip>` so `request.client` reflects the real client
address.

### Startup sequence (`lifespan` in [`main.py`](main.py))

On boot the app performs, in order:

1. **`migrate_db()`** — creates the `trades`, `positions`, and `bot_config` tables if missing.
2. **`sync_trade_mode()`** — reads the persisted DEMO/LIVE mode from `bot_config` (defaults to **DEMO**).
3. **`update_auth_header()`** — logs in to Capital.com and caches the `CST` / `X-SECURITY-TOKEN` headers.
4. **`update_markets()`** — loads the full list of tradable epics and their instrument types.
5. **`get_account_preferences()`** — caches leverage settings per instrument.
6. **`jobs.run()`** — starts the background scheduler.
7. **`resume_trades()`** — reconciles DB positions with live Capital.com positions and re-attaches monitors.

On shutdown it closes the SQLite connection and the shared HTTP session.

---

## 4. Authentication & access control

All HTML pages and `/api/*` endpoints are gated by a session cookie. The webhook is intentionally
**not** gated (TradingView cannot send a cookie); it is protected by an IP whitelist instead.

### Session model ([`auth.py`](auth.py), middleware in [`main.py`](main.py))

- Sign in at **`/login`** with `APP_PASSWORD`. A signed, HTTP-only cookie `ch_session` is set for **7 days**.
- The token is `"<expiry>.<HMAC-SHA256(expiry)>"`, signed with `APP_SECRET_KEY`; both the signature and password checks use constant-time comparison.
- **`/logout`** (GET or POST) clears the cookie.
- The `auth_guard` middleware lets through only public paths: `/login`, `/logout`, `/assets/*`, and `/webhook*`.
  - Unauthenticated **htmx** requests receive `401` + `HX-Redirect: /login` (client-side redirect).
  - Unauthenticated plain **GET** navigations are redirected to `/login`.
  - Unauthenticated **API** calls receive `401 {"detail": "Unauthorized"}`.

### Webhook IP whitelist ([`settings.py`](settings.py))

Only these source IPs are accepted on the webhook (the four official TradingView IPs plus localhost):

```
52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7, 127.0.0.1
```

Requests from any other IP get `403 {"message": "IP not whitelisted"}`.

---

## 5. The TradingView webhook

**Endpoint:** `POST /webhook/trading-view` ([`routes/webhook.py`](routes/webhook.py))

### Request body ([`model.py`](model.py) → `TradingViewWebhookModel`)

```json
{
  "epic": "EURUSD",
  "direction": "BUY",
  "amount": 100,
  "hook_name": "20/200EMA",
  "profit": 120,
  "loss": 50,
  "exit_criteria": ["TP", "SL", "STRATEGY", "RECALIBRATE", "EOD_CLOSE", "EOW_CLOSE"]
}
```

| Field | Type / rule | Meaning |
|-------|-------------|---------|
| `epic` | string | Capital.com instrument symbol. Must exist in the loaded markets or the call is rejected `400`. In TradingView use the `{{ticker}}` placeholder. |
| `direction` | `"BUY"` \| `"SELL"` | Trade side. |
| `amount` | integer ≥ 5 | **Capital committed** (margin/notional base), *not* raw contract size — the size is derived from this, leverage, and price (see §7). |
| `hook_name` | string | Strategy identifier; combined with `epic` to key a position (see §6). |
| `profit` | integer ≥ 5 | Take-profit target in account currency. |
| `loss` | integer ≥ 5 | Stop-loss limit in account currency. |
| `exit_criteria` | array of `ExitType` | Which exit rules are active for this trade (see §8). |

### Processing flow

1. **IP check** — reject non-whitelisted source IPs (`403`).
2. **Epic check** — reject epics not present in `memory.epics` (`400`).
3. **Dedup check** — if a trade for the same `epic`+`hook_name` already exists **in the same direction**, the request is logged and ignored (no duplicate position).
4. Otherwise a `HookedTradeExecution` is scheduled as a **FastAPI background task**, and the `epic`+`hook_name`→direction mapping is recorded in memory. The route returns immediately (`200`); execution and monitoring happen asynchronously.

> Note: an opposite-direction signal for the same `epic`+`hook_name` does **not** open a second
> position directly — it is the mechanism that triggers the `STRATEGY` exit on the existing one
> (see §8).

---

## 6. Position identity & the "hook name" concept

A live trade is keyed by the pair **`{epic}_{hook_name}`** ([`memory.py`](memory.py) →
`hooked_trades`). This gives two guarantees:

- **One position per strategy per instrument.** The same `hook_name` on the same `epic` can only
  hold one open position at a time.
- **Independent strategies coexist.** Different `hook_name` values (e.g. `20/200EMA`, `RSI_REVERSAL`)
  run and are tracked separately on the same instrument.

Positions are also segregated by **trade mode** (`DEMO` vs `LIVE`) in `memory.positions`, so demo
and live state never mix.

---

## 7. Trade sizing & risk/reward setup

When a trade opens ([`hook_trade.py`](hook_trade.py) → `__risk_reward_setup` / `__set_trade_size`):

1. **Entry price** = current ask for BUY, current bid for SELL (from streamed prices).
2. **Leverage** for the epic's instrument is read from cached account preferences.
3. **Notional** = `amount × leverage`; raw **size** = `notional / entry_price`.
4. Size is then **rounded per instrument type** ([`enums/trade.py`](enums/trade.py) `TradeInstrument`):
   - `CURRENCIES` → `max(100, round to nearest 100)`
   - `SHARES` → whole number
   - `COMMODITIES` → 1 decimal place
   - `INDICES` / others → precision rules in `__set_trade_size` / `round_trade_size` ([`utils.py`](utils.py))
5. **Stop-loss** and **take-profit price levels** are derived from `loss`/`profit` (in currency)
   divided by size to get the price move, then applied above/below entry according to direction.

The position is opened via `POST /api/v1/positions` on Capital.com; the resulting `dealId` is
resolved by polling open positions ([`service/capital_api.py`](service/capital_api.py) →
`open_trade` / `get_epic_deal_id`) and persisted to the `positions` table.

---

## 8. Exit criteria (the core feature)

Each trade runs a monitor loop (`__monitor_position`) roughly **every 1.12 seconds**. On every tick
it recomputes PnL from the latest streamed price, updates the in-memory position, and checks the
active exit rules **in this priority order**. The first rule that matches closes the trade, records
the `exit_type`, and writes it to history.

| `ExitType` | Trigger condition | Notes |
|------------|-------------------|-------|
| **`TP`** (Take Profit) | Price reaches the take-profit level derived from `profit`. | Direction-aware (≥ target for BUY, ≤ target for SELL). |
| **`SL`** (Stop Loss) | Price reaches the stop-loss level derived from `loss`. | Caps downside. |
| **`EOD_CLOSE`** (End of Day) | ~2 minutes before the instrument's **last daily session** closes. | Uses Capital.com market opening hours; avoids overnight exposure. |
| **`EOW_CLOSE`** (End of Week) | ~2 minutes before the instrument's **last weekly session** closes. | Avoids weekend gap risk. |
| **`STRATEGY`** (Strategy Switch) | A new signal for the same `epic`+`hook_name` arrives in the **opposite** direction. | Closes the current position so the reversal can take over. On a STRATEGY exit the hook mapping is *kept* (the new side is already recorded). |
| **`RECALIBRATE`** | Trailing profit-lock condition is met across the portfolio (see §9). | Internal logic; include the token in `exit_criteria` to enable it. |
| **`USER`** (Manual) | Operator clicks close in the dashboard (`DELETE /api/trade/{deal_id}`). | **Always active**, regardless of `exit_criteria`. |

Only the exit types you list in `exit_criteria` are evaluated (except `USER`, which is always
available). `RECALIBRATE` and `USER` are internal/manual — the Config page's payload generator only
exposes `TP`, `SL`, `STRATEGY`, `EOD_CLOSE`, and `EOW_CLOSE`.

The EOD/EOW timing is computed from each instrument's `openingHours` fetched from Capital.com and
cached in `memory.trading_hours` (see `is_market_eod_close` / `is_market_eow_close` in
[`service/capital_api.py`](service/capital_api.py)).

---

## 9. Recalibration (trailing profit lock)

`RECALIBRATE` ([`recalibrate.py`](recalibrate.py) → `TrailRecalibration`, driven by
`memory.recalibrate_trade()`) manages exits based on **aggregate open PnL** across all current
positions in the active mode. Defaults ([`memory.py`](memory.py)):

- **`recalibrate_profit = 500`** — trailing only activates once total open PnL reaches +500.
- **`recalibrate_trail_gauge = 70`** — once active, it tracks the running max and triggers a close
  if PnL falls back by 70 from that peak.
- **`cooldown_period = 30s`** — after a recalibration fires it holds the signal through a cooldown,
  then resets, preventing rapid re-triggering.

In effect it locks in profit near break-even-or-better when a favorable move reverses, rather than
riding it all the way back down.

---

## 10. Live price streaming

Prices come from Capital.com's WebSocket, managed by a small pool
([`service/socket_manager.py`](service/socket_manager.py),
[`service/capital_socket.py`](service/capital_socket.py)):

- **≤ 40 epics per socket**; new sockets are created on demand as more epics are subscribed.
- On subscribe, the latest ask/bid is seeded from the REST API so monitoring can start immediately.
- Incoming `quote` messages update `memory.market_data[epic]` with ask/bid/timestamp.
- The listener auto-reconnects on timeout/disconnect and **re-subscribes** all previously watched epics.
- Sockets are pinged every 5 minutes to stay alive (see §11).

---

## 11. Scheduled background jobs ([`job.py`](job.py))

An `AsyncIOScheduler` runs these recurring jobs:

| Job | Interval | Purpose |
|-----|----------|---------|
| `socket_manager.ping_all` | 5 min | Keep WebSocket connections alive. |
| `update_auth_header` | 5 min | Refresh Capital.com `CST` / `X-SECURITY-TOKEN`. |
| `update_markets` | 5 hours | Refresh the epic/instrument catalog. |
| `update_epic_hours` | 5 hours | Refresh cached trading hours for subscribed epics. |

---

## 12. Crash / restart recovery ([`job.py`](job.py) → `resume_trades`)

On startup the app reconciles persisted positions with reality:

1. Fetch currently open positions from Capital.com.
2. For each position stored in the `positions` table:
   - **Still open on Capital.com** → spawn a `ResumeTradeExecution` that re-attaches the monitor/exit
     loop (using the stored entry price, SL/TP levels, and exit criteria), and re-record the hook mapping.
   - **No longer open** → delete the stale row from the DB.

This means open trades keep being managed across restarts without manual intervention.

---

## 13. Web UI

All pages are server-rendered (Jinja2 + htmx) and require a session.

| Path | Page | Content |
|------|------|---------|
| `GET /` | **Dashboard** | Portfolio balance (balance/deposit/available/PnL), live open positions, current mode. |
| `GET /positions` | Positions fragment | htmx-polled open-positions table. |
| `GET /portfolio` | Portfolio fragment | htmx-polled balance panel. |
| `GET /history` | **Trade History** | Closed trades with per-trade PnL, %, direction, exit type, hook name, entry/exit prices, timestamps, plus totals (profits, losses, spreads, net PnL, count). Filtered by current mode. |
| `GET /history/data` | History fragment | htmx-polled history table body. |
| `GET /config` | **Config** | Form that builds a ready-to-paste TradingView JSON payload from your inputs. |
| `GET /login` | Login | Password form. |

> The dashboard is served at the site root `/` (not `/dashboard`).

---

## 14. JSON / control API (prefix `/api`, session-protected)

| Method & path | Description |
|---------------|-------------|
| `GET /api/portfolio` | Current account balance object from Capital.com. |
| `GET /api/positions` | In-memory open positions (all modes). |
| `GET /api/preference` | Cached account preferences (leverages, hedging). |
| `GET /api/history/download` | Download closed-trade history for the current mode as **CSV** (`trades.csv`). |
| `DELETE /api/trade/{deal_id}` | **Manually close** a position — sets its `exit_trade` flag so the monitor loop closes it (`USER` exit). |
| `POST /api/mode` | Switch DEMO/LIVE. Body: `{"mode": "DEMO" \| "LIVE"}`. Persisted to `bot_config`. |
| `POST /api/generate-payload` | Build a TradingView webhook payload from Config-page inputs. |

### `POST /api/generate-payload`

Request ([`model.py`](model.py) → `HookPayloadModel`): `hook_name`, `direction`, `trade_amount`,
`stop_loss`, `take_profit`, and any of the toggles `take_profit_exit`, `stop_loss_exit`,
`strategy_exit`, `end_of_day_close_exit`, `end_of_week_close_exit` set to `"on"`.

Response — a payload you paste into the TradingView alert **Message** field:

```json
{
  "epic": "{{ticker}}",
  "direction": "BUY",
  "amount": 100,
  "hook_name": "20/200EMA",
  "profit": 120,
  "loss": 50,
  "exit_criteria": ["TP", "SL", "STRATEGY"]
}
```

---

## 15. Persistence (SQLite, [`database.py`](database.py))

Database file: `database.db`. Three tables:

- **`trades`** — closed-trade history: `id, epic, size, pnl, pnl_percentage, direction, exit_type,
  hook_name, entry_price, exit_price, opened_at, closed_at, mode`.
- **`positions`** — currently-open positions (for restart recovery): `id, epic, size, hook_name,
  direction, entry_price, entry_date, exit_criteria, profit_price, loss_price, mode`.
- **`bot_config`** — persisted app config; currently just the active `trade_mode`.

The history view derives aggregate **profits**, **losses**, an estimated **spread** cost, and **net
PnL** from the `trades` rows for the active mode.

---

## 16. Configuring the TradingView alert (end-to-end)

1. Deploy Capital Hook on a host reachable from TradingView's servers.
2. Sign in to the dashboard, pick **DEMO** or **LIVE** mode, and use **Config** to generate a payload.
3. In TradingView, create an alert:
   - **Webhook URL:** `https://<your-host>/webhook/trading-view`
   - **Message:** the generated JSON (with `"epic": "{{ticker}}"`).
4. When the alert fires, Capital Hook opens and then manages the position per your `exit_criteria`.

> Ensure the instrument's Capital.com **epic** matches what `{{ticker}}` resolves to; unknown epics
> are rejected with `400`.

---

## 17. Logging

All significant events (logins, webhook receipts, opens/closes, API/WebSocket errors) are appended
to **`app.log`** and printed to stdout ([`logger.py`](logger.py)).

---

## 18. Endpoint reference (quick list)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET/POST | `/login` | public | Sign in |
| GET/POST | `/logout` | public | Sign out |
| POST | `/webhook/trading-view` | IP whitelist | Receive TradingView signal |
| GET | `/` | session | Dashboard |
| GET | `/positions` | session | Positions fragment |
| GET | `/portfolio` | session | Portfolio fragment |
| GET | `/history` | session | Trade history |
| GET | `/history/data` | session | History fragment |
| GET | `/config` | session | Payload builder |
| GET | `/api/portfolio` | session | Balance JSON |
| GET | `/api/positions` | session | Open positions JSON |
| GET | `/api/preference` | session | Account preferences |
| GET | `/api/history/download` | session | History CSV |
| DELETE | `/api/trade/{deal_id}` | session | Manual close |
| POST | `/api/mode` | session | Switch DEMO/LIVE |
| POST | `/api/generate-payload` | session | Build TradingView payload |
