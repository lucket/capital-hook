"""In-memory stand-in for the Capital.com REST API, for testing code built on capital_hook.

    fake = FakeCapital()
    client = CapitalClient("me", "pw", "key", http=fake.client())
    await client.login()

`fake.positions`, `fake.deleted`, `fake.requests`, `fake.balance`, `fake.quote` ...
can be inspected and changed to script scenarios; `login_ok = False` and
`fail_open = True` make logins / position opens fail.
"""
import itertools
import json

import httpx

class FakeCapital:
    def __init__(self):
        self.requests = []        # (method, path, host)
        self.positions = {}       # dealId -> {"epic", "size", "direction", "level"}
        self.deleted = []         # dealIds closed
        self.balance = 10000.0
        self.login_ok = True
        self.fail_open = False    # reject POST /positions with a 400
        self.logins = 0
        self.valid_cst = None     # current session token; expire_session() invalidates it
        self.preferences = {"hedgingMode": False, "leverages": {"COMMODITIES": {"current": 20, "available": [1, 5, 20]}, "INDICES": {"current": 20, "available": [1, 20]}}}
        self._ids = itertools.count(1)
        self.quote = {"GOLD": (2000.0, 2001.0)}   # epic -> (bid, ask)
        self.markets = {"GOLD": "COMMODITIES", "US100": "INDICES"}
        self.hours = {"mon": ["00:00 - 23:59"], "tue": ["00:00 - 23:59"], "wed": ["00:00 - 23:59"],
                      "thu": ["00:00 - 23:59"], "fri": ["00:00 - 21:55"], "sat": [], "sun": ["23:00 - 23:59"]}

    def expire_session(self) -> None:
        """Make the current tokens invalid; the next request gets a 401."""
        self.valid_cst = None

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    # ---- routing ----
    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path, request.url.host))

        if path == "/api/v1/session" and method == "POST":
            self.logins += 1
            if not self.login_ok or not request.headers.get("X-CAP-API-KEY"):
                return httpx.Response(401, json={"errorCode": "error.invalid.details"})
            self.valid_cst = f"cst-{self.logins}"
            return httpx.Response(200, json={"accountType": "CFD"}, headers={"CST": self.valid_cst, "X-SECURITY-TOKEN": f"sec-{self.logins}"})

        if self.valid_cst is None or request.headers.get("CST") != self.valid_cst or request.headers.get("X-SECURITY-TOKEN") != f"sec-{self.logins}":
            return httpx.Response(401, json={"errorCode": "error.invalid.session.token"})

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
            self.preferences = json.loads(request.read())
            return httpx.Response(200, json={"status": "SUCCESS"})
        if path == "/api/v1/positions" and method == "GET":
            return httpx.Response(200, json={"positions": [
                {"market": {"epic": p["epic"]},
                 "position": {"size": p["size"], "direction": p["direction"], "upl": 0.0, "level": p["level"], "dealId": d, "createdDateUTC": "2026-07-12T10:00:00"}}
                for d, p in self.positions.items()]})
        if path == "/api/v1/positions" and method == "POST":
            if self.fail_open:
                return httpx.Response(400, json={"errorCode": "error.invalid.size"})
            body = request.read()
            data = json.loads(body)
            deal_id = f"DEAL{next(self._ids)}"
            bid, ask = self.quote[data["epic"]]
            self.positions[deal_id] = {"epic": data["epic"], "size": float(data["size"]), "direction": data["direction"], "level": ask if data["direction"] == "BUY" else bid}
            return httpx.Response(200, json={"dealReference": f"o_{deal_id}"})
        if path.startswith("/api/v1/positions/") and method == "DELETE":
            deal_id = path.rsplit("/", 1)[1]
            if deal_id not in self.positions:
                return httpx.Response(404, json={"errorCode": "error.not-found"})
            del self.positions[deal_id]
            self.deleted.append(deal_id)
            return httpx.Response(200, json={"dealReference": f"c_{deal_id}"})
        return httpx.Response(404, json={"errorCode": "error.unhandled", "path": path})
