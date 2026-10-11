"""In-memory stand-in for the Capital.com API, for testing code built on capital_hook.

    fake = FakeCapital()
    client = CapitalClient("me", "pw", "key", http=fake.client())
    await client.login()

It is a small **state machine**, not a canned responder: opening a position reserves margin, closing it realises
profit and loss into the balance, stop / profit levels and working orders trigger when the price crosses them, and
``check_invariants()`` verifies that everything it holds is consistent.

Inspect and script it through plain attributes: ``positions`` (dealId -> ``{epic, size, direction, level}``),
``orders`` (working orders), ``quote`` (epic -> ``(bid, ask)``; change it or call ``set_quote``), ``balance``,
``markets``, ``hours``, ``preferences``, ``requests`` / ``bodies``, ``closed`` (every close with its reason),
``activity``, ``confirmations``, and the clock ``now``.

Make things fail with ``login_ok = False``, ``fail_open``, ``fail_close``, ``positions_failures``, ``confirm_delay``,
``market_status[epic] = "CLOSED"``, ``min_deal_size[epic]``, or per request with ``fail_next(...)`` (an error answer) and
``lose_response_next(...)`` (the server acts, the client sees a timeout).

Answers follow the official Capital.com API collection (an open returns an ``o_<uuid>`` reference whose outcome is read
with ``GET /confirms/{ref}``; update / close return ``p_<dealId>``; an unknown deal is 404 ``error.not-found.dealId``).
What that collection does not document is listed in ``ASSUMPTIONS``: do not build on those details.
"""
import itertools
import json
import uuid
from datetime import datetime, timezone

import httpx

ASSUMPTIONS = {
    "rejection reasons": "Asynchronous rejections use plausible reasons (MARKET_CLOSED, MINIMUM_ORDER_SIZE_ERROR, INSUFFICIENT_FUNDS); "
                         "the real strings are not documented. Branch on dealStatus, never on reason.",
    "confirmation ids": "An accepted open confirms with a dealId of its own (the order) and the position in affectedDeals[OPENED], as in the "
                        "Postman example. For working orders the fake uses the working-order id for both - not yet observed.",
    "rejected confirmation shape": "A REJECTED confirmation has status DELETED, no dealId and no affectedDeals.",
    "activity statuses": "Opens and closes are logged as POSITION/ACCEPTED, orders as WORKING_ORDER CREATED / MODIFIED / CANCELLED / "
                         "EXECUTED / EXPIRED; the real sequence is not documented.",
    "fill price": "A triggered working order fills at the current ask (BUY) / bid (SELL).",
    "profit and loss": "A position's upl is marked to the closing side (bid for BUY, ask for SELL), so a fresh deal shows the spread.",
    "margin": "Margin = size x level / leverage of the instrument type (leverage 1 when unknown); used only for self-consistency.",
    "netting": "Observed on DEMO (hedging off): a second same-direction deal opens its own position; an opposite deal closes a position and "
               "returns no affectedDeals. Assumed: oldest positions first, source USER; only exact-size offsets are modelled (others: 400 "
               "error.unsupported.in.fake). With hedging on, every open creates its own position.",
    "session tokens": "A new login invalidates the tokens of the previous session.",
    "close/update confirmations": "Observed: GET /confirms/p_<id> after a close is ACCEPTED with status CLOSED. The fake adds profit and an "
                                  "affectedDeals CLOSED entry (assumed); a confirmation for an update is NOT modelled (404).",
    "id format": "The real API answers 400 error.invalid.dealId for a malformed id and 404 error.not-found.dealId for a well-formed unknown one; "
                 "the fake answers 404 for every unknown id.",
    "unfiltered markets": "GET /markets with no searchTerm / epics returns every market here; the real DEMO server answered 500 (kept for the "
                          "downstream tests that rely on it); set unfiltered_markets_fails = True to get the real behaviour. Use the filters.",
    "unsupported order options": "guaranteed / trailing stops and distance / amount variants are refused with 400 error.unsupported.in.fake.",
}

_UNSUPPORTED = ("guaranteedStop", "trailingStop", "stopDistance", "profitDistance", "stopAmount", "profitAmount")
_TIME = "%Y-%m-%dT%H:%M:%S"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FakeCapital:
    def __init__(self):
        self.requests = []        # (method, path, host)
        self.bodies = []          # JSON body of each request (None when it has none), aligned with `requests`
        self.positions = {}       # dealId -> {"epic", "size", "direction", "level"}
        self.deleted = []         # dealIds closed through DELETE /positions/{id}
        self.closed = []          # every close: {"dealId","epic","direction","size","open_level","close_level","pnl","source","time"}
        self.login_ok = True
        self.positions_failures = 0   # next N `GET /positions` answer 500
        self.fail_close = False   # reject DELETE /positions/{id} with a 500
        self.fail_open = False    # reject POST /positions with a 400
        self.confirm_delay = 0    # the next N `GET /confirms/{ref}` answer 404 (confirmation not available yet)
        self.logins = 0
        self.valid_cst = None     # current session token; expire_session() invalidates it
        self.preferences = {"hedgingMode": False, "leverages": {"COMMODITIES": {"current": 20, "available": [1, 5, 20]}, "INDICES": {"current": 20, "available": [1, 20]}}}
        self._ids = itertools.count(1)
        self._refs = itertools.count(1)
        self.quote = {"GOLD": (2000.0, 2001.0)}   # epic -> (bid, ask)
        self.markets = {"GOLD": "COMMODITIES", "US100": "INDICES"}
        self.min_deal_size = {}        # epic -> minimum deal size (default 0.01)
        self.market_status = {}        # epic -> "TRADEABLE" (default) | "CLOSED" ...
        self.hours = {"mon": ["00:00 - 23:59"], "tue": ["00:00 - 23:59"], "wed": ["00:00 - 23:59"],
                      "thu": ["00:00 - 23:59"], "fri": ["00:00 - 21:55"], "sat": [], "sun": ["23:00 - 23:59"]}
        # side tables: richer per-deal data that must not change the shape of `positions`
        self.now = _utcnow        # clock (naive UTC); replace with a callable to control time
        self.orders = {}          # working order dealId -> {"epic","direction","size","level","type","goodTillDate","created", ...}
        self.activity = []        # history/activity records, oldest first
        self.confirmations = {}   # dealReference -> confirmation body
        self.meta = {}            # dealId -> {"dealReference", "stopLevel", "profitLevel", "created", "order"}
        self._issued = set()      # deal ids this fake handed out (as opposed to ones a test wrote into `positions`)
        self.unfiltered_markets_fails = False   # True: GET /markets without searchTerm / epics answers 500, as the DEMO server does
        self.queries = []         # (method, path, query params) of every request that carried a query string
        self._offset_orders = set()   # ids of orders that closed positions instead of opening one
        self._fills = []          # (orderId, dealId): working orders that became positions
        self._faults = []         # pending injected faults
        self._balance = 10000.0
        self._baseline = 10000.0  # balance before any realised profit/loss
        self._realised = 0.0      # profit/loss realised since the baseline

    # ------------------------------------------------------------------ account
    @property
    def balance(self) -> float:
        return self._balance

    @balance.setter
    def balance(self, value: float) -> None:
        """Assigning the balance restarts the books: the invariant becomes balance == this value + later realised P&L."""
        self._balance = self._baseline = float(value)
        self._realised = 0.0

    def _leverage(self, epic: str) -> float:
        kind = self.markets.get(epic, "")
        return float(self.preferences.get("leverages", {}).get(kind, {}).get("current", 1) or 1)

    def upl(self, deal_id: str) -> float:
        """Unrealised profit/loss of an open position, marked to the closing side of the quote."""
        p = self.positions[deal_id]
        bid, ask = self.quote.get(p["epic"], (p["level"], p["level"]))
        return (bid - p["level"]) * p["size"] if p["direction"] == "BUY" else (p["level"] - ask) * p["size"]

    def margin(self) -> float:
        return sum(p["size"] * p["level"] / self._leverage(p["epic"]) for p in self.positions.values())

    def _account_body(self) -> dict:
        upl = sum(self.upl(d) for d in self.positions)
        return {"symbol": "$", "balance": {"balance": self._balance, "deposit": self._baseline, "profitLoss": round(upl, 6),
                                           "available": self._balance + upl - self.margin()}}

    # ------------------------------------------------------------------ scripting helpers
    def expire_session(self) -> None:
        """Make the current tokens invalid; the next request gets a 401."""
        self.valid_cst = None

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def set_quote(self, epic: str, bid: float, ask: float) -> None:
        """Move the market and let stops, profit levels and working orders react."""
        self.quote[epic] = (bid, ask)
        self.settle()

    def fail_next(self, method: str, path_prefix: str, status: int = 500, error_code: str = "error.server", times: int = 1) -> None:
        """The next ``times`` requests matching method and path prefix are answered with an error and change nothing."""
        self._faults.append({"kind": "error", "method": method, "prefix": path_prefix, "status": status, "code": error_code, "times": times})

    def lose_response_next(self, method: str, path_prefix: str, times: int = 1) -> None:
        """The next matching requests are executed by the server, but the client gets a timeout instead of the answer."""
        self._faults.append({"kind": "lose", "method": method, "prefix": path_prefix, "times": times})

    def _take_fault(self, method: str, path: str):
        for fault in self._faults:
            if fault["method"] == method and path.startswith(fault["prefix"]):
                fault["times"] -= 1
                if fault["times"] <= 0:
                    self._faults.remove(fault)
                return fault
        return None

    # ------------------------------------------------------------------ internals
    def _stamp(self) -> str:
        return self.now().strftime(_TIME)

    def _new_reference(self, prefix: str = "o") -> str:
        return f"{prefix}_{uuid.UUID(int=next(self._refs))}"

    def _new_id(self, prefix: str = "DEAL") -> str:
        deal_id = f"{prefix}{next(self._ids)}"
        self._issued.add(deal_id)
        return deal_id

    def _log(self, kind: str, status: str, deal_id: str, epic: str, source: str = "USER", **details) -> None:
        stamp = self._stamp()
        self.activity.append({"date": stamp, "dateUTC": stamp, "epic": epic, "dealId": deal_id, "source": source, "type": kind,
                              "status": status, "details": details})

    @staticmethod
    def _matches(record: dict, expression: str) -> bool:
        """A small FIQL subset: clauses joined by ';' (AND), each ``field==a,b`` or ``field!=a,b``."""
        for clause in filter(None, expression.split(";")):
            op = "!=" if "!=" in clause else "=="
            field, _, values = clause.partition(op)
            hit = record.get(field) in values.split(",")
            if hit == (op == "!="):
                return False
        return True

    @staticmethod
    def _error(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"errorCode": code})

    def _position_body(self, deal_id: str, p: dict) -> dict:
        meta = self.meta.get(deal_id, {})
        position = {"size": p["size"], "direction": p["direction"], "upl": round(self.upl(deal_id), 6), "level": p["level"], "dealId": deal_id,
                    "dealReference": meta.get("dealReference", f"p_{deal_id}"), "createdDateUTC": meta.get("created") or self._stamp()}
        for key in ("stopLevel", "profitLevel"):
            if meta.get(key) is not None:
                position[key] = meta[key]
        return {"market": {"epic": p["epic"]}, "position": position}

    def _order_body(self, deal_id: str, o: dict) -> dict:
        data = {"dealId": deal_id, "direction": o["direction"], "epic": o["epic"], "orderSize": o["size"], "orderLevel": o["level"],
                "orderType": o["type"], "timeInForce": "GOOD_TILL_DATE" if o["goodTillDate"] else "GOOD_TILL_CANCELLED",
                "createdDateUTC": o["created"]}
        if o["goodTillDate"]:
            data["goodTillDateUTC"] = o["goodTillDate"]
        return {"workingOrderData": data, "marketData": {"epic": o["epic"]}}

    # ------------------------------------------------------------------ the market reacts
    def _close(self, deal_id: str, source: str, ref: str | None = None) -> dict:
        """Remove a position and realise its profit or loss."""
        p = self.positions.pop(deal_id)
        bid, ask = self.quote.get(p["epic"], (p["level"], p["level"]))
        close_level = bid if p["direction"] == "BUY" else ask
        pnl = (close_level - p["level"]) * p["size"] if p["direction"] == "BUY" else (p["level"] - close_level) * p["size"]
        self._balance += pnl
        self._realised += pnl
        self.meta.pop(deal_id, None)
        record = {"dealId": deal_id, "epic": p["epic"], "direction": p["direction"], "size": p["size"], "open_level": p["level"],
                  "close_level": close_level, "pnl": pnl, "source": source, "time": self._stamp()}
        self.closed.append(record)
        reference = ref or f"p_{deal_id}"
        self._log("POSITION", "ACCEPTED", deal_id, p["epic"], source=source, dealReference=reference)
        self.confirmations[reference] = {
            "date": self._stamp() + ".000", "status": "CLOSED", "dealStatus": "ACCEPTED", "epic": p["epic"],
            "dealReference": reference, "dealId": deal_id, "affectedDeals": [{"dealId": deal_id, "status": "CLOSED"}], "level": close_level,
            "size": p["size"], "direction": "SELL" if p["direction"] == "BUY" else "BUY", "profit": round(pnl, 6), "profitCurrency": "USD",
            "guaranteedStop": False, "trailingStop": False}
        return record

    def settle(self) -> None:
        """Apply what the clock and the quotes imply: expire working orders, fill the ones the price reached, and
        trigger stop / profit levels. Runs automatically at the start of every request."""
        now = self.now()
        for deal_id, order in list(self.orders.items()):
            if order["goodTillDate"] and datetime.strptime(order["goodTillDate"], _TIME) <= now:
                del self.orders[deal_id]
                self._log("WORKING_ORDER", "EXPIRED", deal_id, order["epic"], source="SYSTEM")
                continue
            bid, ask = self.quote.get(order["epic"], (None, None))
            if bid is None:
                continue
            buy = order["direction"] == "BUY"
            price = ask if buy else bid
            reached = (price <= order["level"]) if (order["type"] == "LIMIT") == buy else (price >= order["level"])
            if not reached:
                continue
            del self.orders[deal_id]
            new_id = self._new_id()
            self.positions[new_id] = {"epic": order["epic"], "size": order["size"], "direction": order["direction"], "level": price}
            self.meta[new_id] = {"dealReference": f"p_{new_id}", "stopLevel": order.get("stopLevel"), "profitLevel": order.get("profitLevel"),
                                 "created": self._stamp(), "order": deal_id}
            self._fills.append((deal_id, new_id))
            self._log("WORKING_ORDER", "EXECUTED", deal_id, order["epic"], source="SYSTEM", dealReference=f"p_{new_id}")
            self._log("POSITION", "ACCEPTED", new_id, order["epic"], source="SYSTEM", dealReference=f"p_{new_id}")
        for deal_id in list(self.positions):
            p, meta = self.positions[deal_id], self.meta.get(deal_id, {})
            bid, ask = self.quote.get(p["epic"], (None, None))
            if bid is None:
                continue
            price = bid if p["direction"] == "BUY" else ask
            stop, profit = meta.get("stopLevel"), meta.get("profitLevel")
            long_ = p["direction"] == "BUY"
            if stop is not None and ((price <= stop) if long_ else (price >= stop)):
                self._close(deal_id, "SL")
            elif profit is not None and ((price >= profit) if long_ else (price <= profit)):
                self._close(deal_id, "TP")

    def _check_levels(self, direction: str, level: float, body: dict):
        """Stop / profit levels must sit on the correct side of the entry level (400 with the limit, as the real API)."""
        stop, profit = body.get("stopLevel"), body.get("profitLevel")
        long_ = direction == "BUY"
        if stop is not None and (stop >= level if long_ else stop <= level):
            return self._error(400, f"error.invalid.stoploss.{'maxvalue' if long_ else 'minvalue'}: {level}")
        if profit is not None and (profit <= level if long_ else profit >= level):
            return self._error(400, f"error.invalid.takeprofit.{'minvalue' if long_ else 'maxvalue'}: {level}")
        return None

    def _reject_reason(self, epic: str, size: float, level: float) -> str | None:
        if self.market_status.get(epic, "TRADEABLE") != "TRADEABLE":
            return "MARKET_CLOSED"
        if size < self.min_deal_size.get(epic, 0.01):
            return "MINIMUM_ORDER_SIZE_ERROR"
        available = self._balance + sum(self.upl(d) for d in self.positions) - self.margin()
        if size * level / self._leverage(epic) > available:
            return "INSUFFICIENT_FUNDS"
        return None

    def _offset(self, epic: str, direction: str, size: float):
        """Hedging off: a deal against existing positions closes them. Oldest first (assumed); only exact fits are modelled.

        Returns True when positions were closed, False when there is nothing to offset, "refused" for a partial offset."""
        opposite = [(d, p) for d, p in self.positions.items() if p["epic"] == epic and p["direction"] != direction]
        if not opposite:
            return False
        chosen, total = [], 0.0
        for deal_id, p in opposite:
            if total + 1e-12 >= size:
                break
            chosen.append(deal_id)
            total += p["size"]
        if abs(total - size) > 1e-9:
            return "refused"
        for deal_id in chosen:
            self._close(deal_id, "USER")
        return True

    def _confirmation(self, reference: str, body: dict, level: float, deal_id: str | None, reason: str | None, *, order_id: str | None = None,
                      affected: list | None = None) -> dict:
        data = {"date": self._stamp() + ".000", "status": "OPEN" if not reason else "DELETED",
                "dealStatus": "REJECTED" if reason else "ACCEPTED", "epic": body["epic"], "dealReference": reference,
                "affectedDeals": [] if reason or affected == [] else [{"dealId": deal_id, "status": "OPENED"}], "level": level, "size": float(body["size"]),
                "direction": body["direction"], "guaranteedStop": False, "trailingStop": False}
        if reason:
            data["reason"] = reason                         # DEMO's accepted confirmations carry no "reason" key (the Postman example does)
        else:
            data["dealId"] = order_id or deal_id            # an order's id; for an opened position it differs from the position's id
        self.confirmations[reference] = data
        return data

    # ------------------------------------------------------------------ routing
    def _market_detail(self, epic: str) -> dict:
        bid, ask = self.quote.get(epic, (0, 0))
        return {
            "instrument": {"epic": epic, "name": epic, "type": self.markets.get(epic, ""), "lotSize": 1, "currency": "USD", "openingHours": self.hours},
            "dealingRules": {"minDealSize": {"unit": "POINTS", "value": self.min_deal_size.get(epic, 0.01)},
                             "minStepDistance": {"unit": "POINTS", "value": 0.01},
                             "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.01},
                             "maxStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 100},
                             "minSizeIncrement": {"unit": "POINTS", "value": 0.01},
                             "maxDealSize": {"unit": "POINTS", "value": 1000}},
            "snapshot": {"marketStatus": self.market_status.get(epic, "TRADEABLE"), "bid": bid, "offer": ask},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path, request.url.host))
        if request.url.params:
            self.queries.append((method, path, dict(request.url.params)))
        raw = request.read()
        body = json.loads(raw) if raw else None
        self.bodies.append(body)

        fault = self._take_fault(method, path)
        if fault and fault["kind"] == "error":
            return self._error(fault["status"], fault["code"])
        response = self._dispatch(method, path, request, body)
        if fault and fault["kind"] == "lose":
            raise httpx.ReadTimeout("response lost after the server executed the request", request=request)
        return response

    def _dispatch(self, method: str, path: str, request: httpx.Request, body) -> httpx.Response:
        if path == "/api/v1/session" and method == "POST":
            self.logins += 1
            if not self.login_ok or not request.headers.get("X-CAP-API-KEY"):
                return self._error(401, "error.invalid.details")
            self.valid_cst = f"cst-{self.logins}"
            return httpx.Response(200, json={"accountType": "CFD"}, headers={"CST": self.valid_cst, "X-SECURITY-TOKEN": f"sec-{self.logins}"})

        if self.valid_cst is None or request.headers.get("CST") != self.valid_cst or request.headers.get("X-SECURITY-TOKEN") != f"sec-{self.logins}":
            return self._error(401, "error.invalid.session.token")

        self.settle()

        if path == "/api/v1/session" and method == "DELETE":
            self.valid_cst = None
            return httpx.Response(200, json={"status": "SUCCESS"})
        if path == "/api/v1/ping" and method == "GET":
            return httpx.Response(200, json={"status": "OK"})

        if path == "/api/v1/markets" and method == "GET":
            term, wanted = request.url.params.get("searchTerm"), request.url.params.get("epics")
            if term:                                           # summaries under "markets"
                return httpx.Response(200, json={"markets": [{"epic": e, "instrumentType": t, "instrumentName": e}
                                                             for e, t in self.markets.items() if term.lower() in e.lower()]})
            if wanted:                                         # full details under "marketDetails"
                known = [e for e in wanted.split(",") if e in self.markets or e in self.quote]
                return httpx.Response(200, json={"marketDetails": [self._market_detail(e) for e in known]})
            if self.unfiltered_markets_fails:
                return self._error(500, 'Cannot invoke "Object.hashCode()" because "key" is null')
            return httpx.Response(200, json={"markets": [{"epic": e, "instrumentType": t} for e, t in self.markets.items()]})
        if path.startswith("/api/v1/markets/") and method == "GET":
            epic = path.rsplit("/", 1)[1]
            if epic not in self.quote and epic not in self.markets:
                return self._error(404, "error.not-found.epic")
            return httpx.Response(200, json=self._market_detail(epic))
        if path == "/api/v1/accounts" and method == "GET":
            return httpx.Response(200, json={"accounts": [self._account_body()]})
        if path == "/api/v1/accounts/preferences" and method == "GET":
            return httpx.Response(200, json=self.preferences)
        if path == "/api/v1/accounts/preferences" and method == "PUT":
            self.preferences = body
            return httpx.Response(200, json={"status": "SUCCESS"})

        if path == "/api/v1/positions" and method == "GET":
            if self.positions_failures > 0:
                self.positions_failures -= 1
                return self._error(500, "error.server")
            return httpx.Response(200, json={"positions": [self._position_body(d, p) for d, p in self.positions.items()]})
        if path == "/api/v1/positions" and method == "POST":
            return self._open_position(body)
        if path.startswith("/api/v1/positions/") and method in ("GET", "PUT", "DELETE"):
            return self._one_position(method, path.rsplit("/", 1)[1], body)

        if path == "/api/v1/workingorders" and method == "GET":
            return httpx.Response(200, json={"workingOrders": [self._order_body(d, o) for d, o in self.orders.items()]})
        if path == "/api/v1/workingorders" and method == "POST":
            return self._create_order(body)
        if path.startswith("/api/v1/workingorders/") and method in ("PUT", "DELETE"):
            return self._one_order(method, path.rsplit("/", 1)[1], body)

        if path == "/api/v1/history/activity" and method == "GET":
            params = dict(request.url.params)
            records = [dict(a) for a in self.activity]
            if "dealId" in params:
                records = [a for a in records if a["dealId"] == params["dealId"]]
            if "filter" in params:
                records = [a for a in records if self._matches(a, params["filter"])]
            for a in records:
                details = a.pop("details")
                if params.get("detailed") == "true":
                    a["details"] = details
            return httpx.Response(200, json={"activities": records})

        if path.startswith("/api/v1/confirms/") and method == "GET":
            if self.confirm_delay > 0:
                self.confirm_delay -= 1
                return self._error(404, "error.not-found.dealReference")
            confirmation = self.confirmations.get(path.rsplit("/", 1)[1])
            if confirmation is None:
                return self._error(404, "error.not-found.dealReference")
            return httpx.Response(200, json=confirmation)
        return httpx.Response(404, json={"errorCode": "error.unhandled", "path": path})

    # ------------------------------------------------------------------ positions
    def _open_position(self, body: dict) -> httpx.Response:
        if self.fail_open:
            return self._error(400, "error.invalid.size")
        if any(k in body for k in _UNSUPPORTED):
            return self._error(400, "error.unsupported.in.fake")
        epic = body["epic"]
        if epic not in self.quote:
            return self._error(404, "error.not-found.epic")
        bid, ask = self.quote[epic]
        level = ask if body["direction"] == "BUY" else bid
        bad = self._check_levels(body["direction"], level, body)
        if bad is not None:
            return bad
        reference = self._new_reference("o")
        reason = self._reject_reason(epic, float(body["size"]), level)
        if reason:
            self._confirmation(reference, body, level, None, reason)
            self._log("POSITION", "REJECTED", "", epic, dealReference=reference, reason=reason)
            return httpx.Response(200, json={"dealReference": reference})
        if not self.preferences.get("hedgingMode"):
            offset = self._offset(epic, body["direction"], float(body["size"]))
            if offset == "refused":
                return self._error(400, "error.unsupported.in.fake")
            if offset:                                              # an opposite deal closed existing position(s) instead of opening one
                order_id = self._new_id("OD")
                self._offset_orders.add(order_id)
                self._confirmation(reference, body, level, None, None, order_id=order_id, affected=[])
                self._log("POSITION", "ACCEPTED", "", epic, dealReference=reference, size=float(body["size"]), direction=body["direction"], level=level)
                return httpx.Response(200, json={"dealReference": reference})
        deal_id = self._new_id()
        self.positions[deal_id] = {"epic": epic, "size": float(body["size"]), "direction": body["direction"], "level": level}
        self.meta[deal_id] = {"dealReference": f"p_{deal_id}", "stopLevel": body.get("stopLevel"), "profitLevel": body.get("profitLevel"),
                              "created": self._stamp(), "order": None}
        self._confirmation(reference, body, level, deal_id, None, order_id=self._new_id("OD"))
        self._log("POSITION", "ACCEPTED", deal_id, epic, dealReference=reference, size=float(body["size"]), direction=body["direction"], level=level)
        return httpx.Response(200, json={"dealReference": reference})

    def _one_position(self, method: str, deal_id: str, body) -> httpx.Response:
        if method == "DELETE" and self.fail_close:
            return self._error(500, "error.server")
        if deal_id not in self.positions:
            return self._error(404, "error.not-found.dealId")
        if method == "GET":
            return httpx.Response(200, json=self._position_body(deal_id, self.positions[deal_id]))
        if method == "PUT":
            if any(k in body for k in _UNSUPPORTED):
                return self._error(400, "error.unsupported.in.fake")
            p = self.positions[deal_id]
            bid, ask = self.quote.get(p["epic"], (p["level"], p["level"]))
            bad = self._check_levels(p["direction"], bid if p["direction"] == "BUY" else ask, body)
            if bad is not None:
                return bad
            for key in ("stopLevel", "profitLevel"):
                if key in body:
                    self.meta.setdefault(deal_id, {})[key] = body[key]
            self._log("POSITION", "MODIFIED", deal_id, p["epic"])
            return httpx.Response(200, json={"dealReference": f"p_{deal_id}"})
        self.deleted.append(deal_id)
        self._close(deal_id, "USER")
        return httpx.Response(200, json={"dealReference": f"p_{deal_id}"})

    # ------------------------------------------------------------------ working orders
    def _create_order(self, body: dict) -> httpx.Response:
        if any(k in body for k in _UNSUPPORTED):
            return self._error(400, "error.unsupported.in.fake")
        if body.get("goodTillDate") and datetime.strptime(body["goodTillDate"], _TIME) <= self.now():
            return self._error(400, "error.not-in-future.goodTillDate")
        if body["epic"] not in self.quote:
            return self._error(404, "error.not-found.epic")
        deal_id = self._new_id("WO")
        reference = self._new_reference("o")
        self.orders[deal_id] = {"epic": body["epic"], "direction": body["direction"], "size": float(body["size"]), "level": float(body["level"]),
                                "type": body["type"], "goodTillDate": body.get("goodTillDate"), "created": self._stamp(),
                                "stopLevel": body.get("stopLevel"), "profitLevel": body.get("profitLevel")}
        self._confirmation(reference, body, float(body["level"]), deal_id, None)
        self._log("WORKING_ORDER", "CREATED", deal_id, body["epic"], dealReference=reference)
        self.settle()                                                          # a marketable order fills at once
        return httpx.Response(200, json={"dealReference": reference})

    def _one_order(self, method: str, deal_id: str, body) -> httpx.Response:
        if deal_id not in self.orders:
            return self._error(404, "error.not-found.dealId")
        order = self.orders[deal_id]
        if method == "PUT":
            if body.get("goodTillDate") and datetime.strptime(body["goodTillDate"], _TIME) <= self.now():
                return self._error(400, "error.not-in-future.goodTillDate")
            if "level" in body:
                order["level"] = float(body["level"])
            if "goodTillDate" in body:
                order["goodTillDate"] = body["goodTillDate"]
            for key in ("stopLevel", "profitLevel"):
                if key in body:
                    order[key] = body[key]
            self._log("WORKING_ORDER", "MODIFIED", deal_id, order["epic"])
            self.settle()
        else:
            del self.orders[deal_id]
            self._log("WORKING_ORDER", "CANCELLED", deal_id, order["epic"])
        return httpx.Response(200, json={"dealReference": self._new_reference("o")})

    # ------------------------------------------------------------------ consistency
    def check_invariants(self) -> None:
        """Raise AssertionError listing every way in which the fake's own state is inconsistent.

        Positions that a test wrote straight into ``positions`` are tolerated (they have no history); everything the
        fake created itself must be fully accounted for.
        """
        self.settle()
        problems = []
        ids = list(self.positions) + list(self.orders)
        if len(ids) != len(set(ids)):
            problems.append(f"an id is both a position and a working order: {sorted(i for i in set(ids) if ids.count(i) > 1)}")
        closed_ids = [c["dealId"] for c in self.closed]
        if len(closed_ids) != len(set(closed_ids)):
            problems.append("a deal was closed more than once")
        for deal_id in closed_ids:
            if deal_id in self.positions:
                problems.append(f"{deal_id} is closed but still open")
        if abs(self._balance - (self._baseline + self._realised)) > 1e-6:
            problems.append(f"balance {self._balance} != baseline {self._baseline} + realised {self._realised}")
        for deal_id, p in self.positions.items():
            if p["size"] <= 0:
                problems.append(f"{deal_id} has a non-positive size")
            if deal_id in self._issued:
                meta = self.meta.get(deal_id)
                if not meta or not meta.get("dealReference"):
                    problems.append(f"{deal_id} has no deal reference")
                if not any(a["dealId"] == deal_id and a["type"] == "POSITION" for a in self.activity):
                    problems.append(f"{deal_id} was opened without an activity record")
        for deal_id in closed_ids:
            if deal_id in self._issued and sum(1 for a in self.activity if a["dealId"] == deal_id and a["type"] == "POSITION") < 2:
                problems.append(f"{deal_id} was closed without a close record")
        for reference, c in self.confirmations.items():
            opened = next((a["dealId"] for a in c.get("affectedDeals", []) if a.get("status") == "OPENED"), None)
            deal_id = opened if opened and opened != c.get("dealId") else c.get("dealId")      # an opened position, else the working order
            if c["dealStatus"] == "ACCEPTED":
                known = (deal_id in self.positions or deal_id in self.orders or deal_id in closed_ids or deal_id in self._offset_orders
                         or any(o == deal_id for o, _ in self._fills)
                         or any(a["dealId"] == deal_id and a["status"] in ("CANCELLED", "EXPIRED") for a in self.activity))
                if not known:
                    problems.append(f"confirmation {reference} accepted {deal_id}, which exists nowhere")
            elif deal_id is not None or c["affectedDeals"]:
                problems.append(f"confirmation {reference} was rejected yet names a deal")
        for deal_id, o in self.orders.items():
            if o["level"] <= 0 or o["size"] <= 0:
                problems.append(f"working order {deal_id} has a non-positive level or size")
            if any(a["dealId"] == deal_id and a["status"] in ("CANCELLED", "EXPIRED") for a in self.activity):
                problems.append(f"working order {deal_id} is cancelled/expired yet still pending")
        for order_id, deal_id in self._fills:
            if order_id in self.orders:
                problems.append(f"working order {order_id} filled into {deal_id} but is still pending")
            if deal_id not in self.positions and deal_id not in closed_ids:
                problems.append(f"working order {order_id} filled into {deal_id}, which does not exist")
        account = self._account_body()["balance"]
        if account["available"] > account["balance"] + account["profitLoss"] + 1e-6:
            problems.append("available funds exceed balance plus unrealised P&L")
        if problems:
            raise AssertionError("FakeCapital invariants violated:\n  - " + "\n  - ".join(problems))
