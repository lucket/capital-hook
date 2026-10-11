"""In-memory stand-in for the Capital.com REST API, for testing code built on capital_hook.

    fake = FakeCapital()
    client = CapitalClient("me", "pw", "key", http=fake.client())
    await client.login()

`fake.positions`, `fake.deleted`, `fake.requests`, `fake.balance`, `fake.quote` ...
can be inspected and changed to script scenarios; `login_ok = False`,
`fail_open = True` and `fail_close = True` make logins / position opens / position closes fail,
`confirm_delay = N` makes the next N confirmation lookups answer 404.

The answers follow the official Capital.com API collection: an open returns an ``o_<uuid>`` deal
reference (the outcome is read with ``GET /confirms/{ref}``), update / close return ``p_<dealId>``,
and an unknown deal is a 404 ``error.not-found.dealId``.
"""
import itertools
import json
import uuid

import httpx


class FakeCapital:
    def __init__(self):
        self.requests = []        # (method, path, host)
        self.bodies = []          # JSON body of each request (None when it has none), aligned with `requests`
        self.positions = {}       # dealId -> {"epic", "size", "direction", "level"}
        self.deleted = []         # dealIds closed through DELETE /positions/{id}
        self.balance = 10000.0
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
        self.hours = {"mon": ["00:00 - 23:59"], "tue": ["00:00 - 23:59"], "wed": ["00:00 - 23:59"],
                      "thu": ["00:00 - 23:59"], "fri": ["00:00 - 21:55"], "sat": [], "sun": ["23:00 - 23:59"]}
        # side tables: richer per-deal data that must not change the shape of `positions`
        self.confirmations = {}   # dealReference -> confirmation body
        self.meta = {}            # dealId -> {"dealReference", "stopLevel", "profitLevel"}

    def expire_session(self) -> None:
        """Make the current tokens invalid; the next request gets a 401."""
        self.valid_cst = None

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    # ---- helpers ----
    def _new_reference(self, prefix: str = "o") -> str:
        return f"{prefix}_{uuid.UUID(int=next(self._refs))}"

    @staticmethod
    def _error(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"errorCode": code})

    def _position_body(self, deal_id: str, p: dict) -> dict:
        meta = self.meta.get(deal_id, {})
        position = {"size": p["size"], "direction": p["direction"], "upl": 0.0, "level": p["level"], "dealId": deal_id,
                    "dealReference": meta.get("dealReference", f"p_{deal_id}"), "createdDateUTC": "2026-07-12T10:00:00"}
        for key in ("stopLevel", "profitLevel"):
            if meta.get(key) is not None:
                position[key] = meta[key]
        return {"market": {"epic": p["epic"]}, "position": position}

    # ---- routing ----
    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path, request.url.host))
        raw = request.read()
        body = json.loads(raw) if raw else None
        self.bodies.append(body)

        if path == "/api/v1/session" and method == "POST":
            self.logins += 1
            if not self.login_ok or not request.headers.get("X-CAP-API-KEY"):
                return self._error(401, "error.invalid.details")
            self.valid_cst = f"cst-{self.logins}"
            return httpx.Response(200, json={"accountType": "CFD"}, headers={"CST": self.valid_cst, "X-SECURITY-TOKEN": f"sec-{self.logins}"})

        if self.valid_cst is None or request.headers.get("CST") != self.valid_cst or request.headers.get("X-SECURITY-TOKEN") != f"sec-{self.logins}":
            return self._error(401, "error.invalid.session.token")

        if path == "/api/v1/session" and method == "DELETE":
            self.valid_cst = None
            return httpx.Response(200, json={"status": "SUCCESS"})
        if path == "/api/v1/ping" and method == "GET":
            return httpx.Response(200, json={"status": "OK"})

        if path == "/api/v1/markets" and method == "GET":
            return httpx.Response(200, json={"markets": [{"epic": e, "instrumentType": t} for e, t in self.markets.items()]})
        if path.startswith("/api/v1/markets/") and method == "GET":
            epic = path.rsplit("/", 1)[1]
            bid, ask = self.quote[epic]
            return httpx.Response(200, json={"snapshot": {"bid": bid, "offer": ask}, "instrument": {"openingHours": self.hours}})
        if path == "/api/v1/accounts" and method == "GET":
            return httpx.Response(200, json={"accounts": [{"symbol": "$", "balance": {"balance": self.balance, "deposit": self.balance, "profitLoss": 0.0, "available": self.balance}}]})
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
            if self.fail_open:
                return self._error(400, "error.invalid.size")
            deal_id = f"DEAL{next(self._ids)}"
            bid, ask = self.quote[body["epic"]]
            level = ask if body["direction"] == "BUY" else bid
            self.positions[deal_id] = {"epic": body["epic"], "size": float(body["size"]), "direction": body["direction"], "level": level}
            reference = self._new_reference("o")
            self.meta[deal_id] = {"dealReference": f"p_{deal_id}", "stopLevel": body.get("stopLevel"), "profitLevel": body.get("profitLevel")}
            self.confirmations[reference] = {
                "date": "2026-07-12T10:00:00.000", "status": "OPEN", "reason": "SUCCESS", "dealStatus": "ACCEPTED",
                "epic": body["epic"], "dealReference": reference, "dealId": deal_id,
                "affectedDeals": [{"dealId": deal_id, "status": "OPENED"}], "level": level, "size": float(body["size"]),
                "direction": body["direction"], "guaranteedStop": bool(body.get("guaranteedStop")), "trailingStop": bool(body.get("trailingStop")),
            }
            return httpx.Response(200, json={"dealReference": reference})
        if path.startswith("/api/v1/positions/") and method in ("GET", "PUT", "DELETE"):
            deal_id = path.rsplit("/", 1)[1]
            if method == "DELETE" and self.fail_close:
                return self._error(500, "error.server")
            if deal_id not in self.positions:
                return self._error(404, "error.not-found.dealId")
            if method == "GET":
                return httpx.Response(200, json=self._position_body(deal_id, self.positions[deal_id]))
            if method == "PUT":
                for key in ("stopLevel", "profitLevel"):
                    if key in body:
                        self.meta.setdefault(deal_id, {})[key] = body[key]
                return httpx.Response(200, json={"dealReference": f"p_{deal_id}"})
            del self.positions[deal_id]
            self.meta.pop(deal_id, None)
            self.deleted.append(deal_id)
            return httpx.Response(200, json={"dealReference": f"p_{deal_id}"})

        if path.startswith("/api/v1/confirms/") and method == "GET":
            if self.confirm_delay > 0:
                self.confirm_delay -= 1
                return self._error(404, "error.not-found.dealReference")
            confirmation = self.confirmations.get(path.rsplit("/", 1)[1])
            if confirmation is None:
                return self._error(404, "error.not-found.dealReference")
            return httpx.Response(200, json=confirmation)
        return httpx.Response(404, json={"errorCode": "error.unhandled", "path": path})
