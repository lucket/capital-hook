"""Async client for the Capital.com REST API."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

import httpx

from .enums import TradeDirection, TradeMode, WorkingOrderType
from .errors import APIError, AuthError, CapitalError
from .models import CloseEvent, Confirmation

log = logging.getLogger("capital_hook.client")

LIVE_HOST = "https://api-capital.backend-capital.com"
DEMO_HOST = "https://demo-api-capital.backend-capital.com"
STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"


def _direction(direction: "TradeDirection | str") -> str:
    """Normalise to "BUY" / "SELL"; anything else (e.g. NEUTRAL) is a programming error."""
    value = (direction.value if isinstance(direction, TradeDirection) else str(direction)).upper()
    if value not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")
    return value


def _number(value: float) -> str:
    """A plain decimal string (never scientific notation such as '1e-05')."""
    return format(Decimal(str(value)), "f")


def _timestamp(value: "datetime | str") -> str:
    """Capital.com's UTC timestamp format, ``YYYY-MM-DDTHH:MM:SS`` (aware datetimes are converted to UTC)."""
    if isinstance(value, str):
        return value
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _order_fields(*, stop_level=None, profit_level=None, stop_distance=None, profit_distance=None,
                  stop_amount=None, profit_amount=None, guaranteed_stop=None, trailing_stop=None) -> dict:
    """Optional stop / profit settings of an order, sent only when given."""
    fields = {
        "stopLevel": stop_level, "profitLevel": profit_level,
        "stopDistance": stop_distance, "profitDistance": profit_distance,
        "stopAmount": stop_amount, "profitAmount": profit_amount,
        "guaranteedStop": guaranteed_stop, "trailingStop": trailing_stop,
    }
    return {k: v for k, v in fields.items() if v is not None}


class CapitalClient:
    """Talks to one Capital.com environment (DEMO or LIVE) at a time.

    The client owns the session tokens (`CST` / `X-SECURITY-TOKEN`). Call
    :meth:`login` once; if a request is later rejected with 401 (the session
    expired) the client logs in again once and repeats the request.

    Methods raise :class:`APIError` for non-success answers and
    :class:`AuthError` when a session cannot be established; they never swallow
    errors, so callers decide how to retry.
    """

    def __init__(
        self,
        identity: str,
        password: str,
        api_key: str,
        *,
        mode: TradeMode = TradeMode.DEMO,
        http: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        auto_relogin: bool = True,
    ):
        self._identity = identity
        self._password = password
        self._api_key = api_key
        self._mode = mode
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._auto_relogin = auto_relogin
        self._cst: str | None = None
        self._security_token: str | None = None
        self._login_lock = asyncio.Lock()

    # ---- environment / session ----
    @staticmethod
    def host_for(mode: TradeMode) -> str:
        return LIVE_HOST if mode == TradeMode.LIVE else DEMO_HOST

    @property
    def mode(self) -> TradeMode:
        return self._mode

    @property
    def host(self) -> str:
        return self.host_for(self._mode)

    @property
    def authenticated(self) -> bool:
        return bool(self._cst and self._security_token)

    @property
    def session_tokens(self) -> tuple[str, str]:
        """(CST, X-SECURITY-TOKEN) of the current session; raises AuthError when logged out."""
        if not self.authenticated:
            raise AuthError("not logged in")
        return self._cst, self._security_token  # type: ignore[return-value]

    async def login(self) -> None:
        """Open a session on the current environment."""
        response = await self._http.post(
            f"{self.host}/api/v1/session",
            headers={"X-CAP-API-KEY": self._api_key},
            json={"identifier": self._identity, "password": self._password, "encryptedPassword": False},
        )
        if response.status_code != 200:
            raise AuthError(f"login failed on {self._mode.value}: {self._describe(response)}")
        cst, token = response.headers.get("CST"), response.headers.get("X-SECURITY-TOKEN")
        if not cst or not token:
            raise AuthError(f"login on {self._mode.value} returned no session tokens")
        self._cst, self._security_token = cst, token
        log.info("logged in to Capital.com (%s)", self._mode.value)

    async def set_mode(self, mode: TradeMode, *, login: bool = True) -> None:
        """Switch environment. The old session is dropped; by default a new one is opened."""
        if mode == self._mode and self.authenticated:
            return
        self._mode = mode
        self._cst = self._security_token = None
        if login:
            await self.login()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def ping(self) -> str:
        """Keep the session alive (it expires after 10 minutes of inactivity). Returns the status, "OK"."""
        return (await self._request("GET", "/api/v1/ping")).json().get("status", "")

    async def logout(self) -> None:
        """End the session on Capital.com and forget the tokens. Safe to call when already logged out."""
        if not self.authenticated:
            return
        cst, token = self.session_tokens
        try:
            response = await self._http.request("DELETE", f"{self.host}/api/v1/session", headers={"CST": cst, "X-SECURITY-TOKEN": token})
            if response.status_code != 401:          # 401 = it had already expired: nothing left to end
                self._raise_for_status(response)
        finally:
            self._cst = self._security_token = None

    # ---- plumbing ----
    @staticmethod
    def _describe(response: httpx.Response) -> str:
        try:
            code = response.json().get("errorCode")
        except Exception:  # noqa: BLE001 - body is not JSON
            code = None
        return f"{response.status_code}" + (f" {code}" if code else "")

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            code = response.json().get("errorCode")
        except Exception:  # noqa: BLE001
            code = None
        raise APIError(response.status_code, response.text, code, str(response.url))

    async def _request(self, method: str, path: str, *, json=None, params=None, _retry: bool = True) -> httpx.Response:
        if not self.authenticated:
            async with self._login_lock:
                if not self.authenticated:
                    await self.login()
        cst, token = self.session_tokens
        response = await self._http.request(
            method, f"{self.host}{path}", json=json, params=params,
            headers={"CST": cst, "X-SECURITY-TOKEN": token},
        )
        if response.status_code == 401 and self._auto_relogin and _retry:
            async with self._login_lock:
                if self._cst == cst:            # nobody refreshed the session meanwhile
                    log.info("session rejected (401); logging in again")
                    await self.login()
            return await self._request(method, path, json=json, params=params, _retry=False)
        self._raise_for_status(response)
        return response

    # ---- account ----
    async def get_accounts(self) -> list[dict]:
        return (await self._request("GET", "/api/v1/accounts")).json().get("accounts", [])

    async def get_account(self) -> dict:
        """The first (default) account."""
        accounts = await self.get_accounts()
        if not accounts:
            raise CapitalError("no accounts returned")
        return accounts[0]

    async def get_preferences(self) -> dict:
        return (await self._request("GET", "/api/v1/accounts/preferences")).json()

    async def set_preferences(self, *, leverages: dict | None = None, hedging_mode: bool | None = None) -> None:
        """Update leverages and/or hedging mode, keeping whatever you do not pass."""
        current = await self.get_preferences()
        payload = {
            "leverages": leverages if leverages is not None else current.get("leverages", {}),
            "hedgingMode": hedging_mode if hedging_mode is not None else current.get("hedgingMode", False),
        }
        await self._request("PUT", "/api/v1/accounts/preferences", json=payload)

    # ---- markets ----
    async def get_markets(self, search_term: str | None = None, epics: list[str] | tuple[str, ...] | None = None) -> list[dict]:
        """Market summaries, each with at least ``epic`` and ``instrumentType``.

        ``search_term`` / ``epics`` (max 50) narrow the answer; ``search_term`` wins when both are given. The two filters come back in
        different shapes from Capital.com (``markets`` summaries vs ``marketDetails`` with instrument / dealingRules / snapshot);
        details are flattened here and keep their ``instrument``, ``dealingRules`` and ``snapshot`` entries.
        Called with no argument the request has no filter, which the DEMO server answered with a 500 in testing.
        """
        params = {}
        if search_term:
            params["searchTerm"] = search_term
        if epics:
            params["epics"] = ",".join(epics)
        body = (await self._request("GET", "/api/v1/markets", params=params or None)).json()
        markets = list(body.get("markets", []))
        for detail in body.get("marketDetails", []):
            instrument, snapshot = detail.get("instrument", {}), detail.get("snapshot", {})
            markets.append({**detail, "epic": instrument.get("epic"), "instrumentType": instrument.get("type"),
                            "instrumentName": instrument.get("name"), "marketStatus": snapshot.get("marketStatus"),
                            "bid": snapshot.get("bid"), "offer": snapshot.get("offer")})
        return markets

    async def get_instrument_types(self, epics: list[str] | tuple[str, ...] | None = None) -> dict[str, str]:
        """Map of epic -> instrumentType, for ``epics`` when given, otherwise for whatever an unfiltered ``get_markets`` returns."""
        return {m["epic"]: m["instrumentType"] for m in await self.get_markets(epics=epics)}

    async def get_market(self, epic: str) -> dict:
        return (await self._request("GET", f"/api/v1/markets/{epic}")).json()

    async def get_prices(self, epic: str) -> tuple[float, float]:
        """Latest (ask, bid) from the REST snapshot."""
        snapshot = (await self.get_market(epic)).get("snapshot", {})
        ask, bid = snapshot.get("offer"), snapshot.get("bid")
        if not ask or not bid:
            raise CapitalError(f"no ask/bid available for {epic}")
        return float(ask), float(bid)

    async def get_opening_hours(self, epic: str) -> dict | None:
        """Weekly opening hours (`{"mon": ["00:00 - 21:55"], ...}`), or None if not published."""
        hours = (await self.get_market(epic)).get("instrument", {}).get("openingHours", {})
        return hours or None

    # ---- positions ----
    @staticmethod
    def _flat_position(p: dict) -> dict:
        return {
            "epic": p["market"]["epic"],
            "size": float(p["position"]["size"]),
            "direction": p["position"]["direction"],
            "pnl": float(p["position"]["upl"]),
            "open_price": float(p["position"]["level"]),
            "deal_id": p["position"]["dealId"],
            "created_date": p["position"]["createdDateUTC"],
        }

    async def get_positions(self) -> list[dict]:
        """Open positions as flat dicts: epic, size, direction, pnl, open_price, deal_id, created_date."""
        raw = (await self._request("GET", "/api/v1/positions")).json().get("positions", [])
        return [self._flat_position(p) for p in raw]

    async def get_position(self, deal_id: str) -> dict:
        """One open position: the keys of :meth:`get_positions` plus ``deal_reference``, ``stop_level``, ``profit_level``
        when Capital.com reports them. Raises :class:`APIError` (404 ``error.not-found.dealId``) if it is not open."""
        data = (await self._request("GET", f"/api/v1/positions/{deal_id}")).json()
        position = data["position"]
        flat = self._flat_position(data)
        for key, source in (("deal_reference", "dealReference"), ("stop_level", "stopLevel"), ("profit_level", "profitLevel")):
            if position.get(source) is not None:
                flat[key] = position[source]
        return flat

    async def open_position(self, epic: str, size: float, direction: TradeDirection | str, **order_options) -> str:
        """Open a market position; returns the deal *reference*. A 200 does not mean the position opened:
        check :meth:`confirm` (or :meth:`get_positions`).

        Optional ``order_options``: ``stop_level``, ``profit_level``, ``stop_distance``, ``profit_distance``,
        ``stop_amount``, ``profit_amount``, ``guaranteed_stop``, ``trailing_stop`` (sent only when given).
        """
        body = {"epic": epic, "direction": _direction(direction), "size": _number(size), **_order_fields(**order_options)}
        response = await self._request("POST", "/api/v1/positions", json=body)
        return response.json()["dealReference"]

    async def update_position(self, deal_id: str, **order_options) -> str:
        """Change the stop / profit of an open position; returns the deal reference (``p_<dealId>``).

        Takes the same optional settings as :meth:`open_position`; at least one is required.
        """
        fields = _order_fields(**order_options)
        if not fields:
            raise ValueError("update_position needs at least one of stop_level, profit_level, stop_distance, profit_distance, "
                             "stop_amount, profit_amount, guaranteed_stop, trailing_stop")
        return (await self._request("PUT", f"/api/v1/positions/{deal_id}", json=fields)).json()["dealReference"]

    async def confirm(self, deal_reference: str, *, attempts: int = 5, delay: float = 0.5) -> Confirmation:
        """Outcome of an order (``ACCEPTED`` / ``REJECTED``) and the deal it produced.

        The confirmation can lag the order by a moment: a 404 ``error.not-found.dealReference`` is retried up to
        ``attempts`` times, ``delay`` seconds apart, before it is raised.
        """
        for attempt in range(1, attempts + 1):
            try:
                data = (await self._request("GET", f"/api/v1/confirms/{deal_reference}")).json()
                return Confirmation.from_response(data)
            except APIError as exc:
                if exc.status_code != 404 or attempt == attempts:
                    raise
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def find_deal_id(
        self,
        epic: str,
        size: float,
        direction: TradeDirection | str,
        *,
        exclude: Iterable[str] = (),
        attempts: int = 6,
        delay: float = 1.0,
    ) -> str | None:
        """Look up the deal id of a just-opened position (the open call only returns a reference).

        Polls the open positions up to `attempts` times, `delay` seconds apart, for one matching epic,
        size and direction whose id is not in `exclude`. Errors while polling are retried; if the last
        attempt also failed, that error is raised (None means "listed fine, no match").
        """
        direction = direction.value if isinstance(direction, TradeDirection) else direction
        excluded = set(exclude)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                positions = await self.get_positions()
            except (CapitalError, httpx.HTTPError) as exc:      # transient trouble: try again
                last_error = exc
                log.warning("could not list positions (attempt %d/%d): %s", attempt + 1, attempts, exc)
            else:
                last_error = None
                for position in positions:
                    if (position["epic"] == epic and float(position["size"]) == float(size)
                            and position["direction"] == direction and position["deal_id"] not in excluded):
                        return position["deal_id"]
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        return None

    async def close_position(self, deal_id: str) -> dict:
        """Close a position; returns Capital.com's answer (contains `dealReference`)."""
        return (await self._request("DELETE", f"/api/v1/positions/{deal_id}")).json()

    # ---- working orders ----
    @staticmethod
    def _flat_working_order(item: dict) -> dict:
        data = item["workingOrderData"]
        flat = {
            "deal_id": data["dealId"],
            "epic": data["epic"],
            "direction": data["direction"],
            "size": float(data["orderSize"]),
            "level": float(data["orderLevel"]),
            "type": data["orderType"],
            "time_in_force": data.get("timeInForce"),
            "good_till_date": data.get("goodTillDateUTC") or data.get("goodTillDate"),
            "created_date": data.get("createdDateUTC") or data.get("createdDate"),
        }
        for key, source in (("stop_distance", "stopDistance"), ("profit_distance", "profitDistance")):
            if data.get(source) is not None:
                flat[key] = data[source]
        return flat

    async def get_working_orders(self) -> list[dict]:
        """Pending (limit / stop) orders as flat dicts: deal_id, epic, direction, size, level, type, time_in_force,
        good_till_date, created_date (+ stop_distance / profit_distance when set)."""
        raw = (await self._request("GET", "/api/v1/workingorders")).json().get("workingOrders", [])
        return [self._flat_working_order(item) for item in raw]

    async def create_working_order(
        self,
        epic: str,
        direction: TradeDirection | str,
        size: float,
        level: float,
        type: WorkingOrderType | str = WorkingOrderType.LIMIT,
        *,
        good_till_date: "datetime | str | None" = None,
        **order_options,
    ) -> str:
        """Place a limit / stop order at ``level``; returns the deal reference (see :meth:`confirm`).

        ``good_till_date`` (UTC) makes it expire; without it the order stays until cancelled. A date in the past is
        refused by Capital.com (400 ``error.not-in-future.goodTillDate``). ``order_options`` as in :meth:`open_position`.
        """
        kind = (type.value if isinstance(type, WorkingOrderType) else str(type)).upper()
        if kind not in ("LIMIT", "STOP"):
            raise ValueError(f"type must be LIMIT or STOP, got {type!r}")
        body = {"epic": epic, "direction": _direction(direction), "size": _number(size), "level": level, "type": kind, **_order_fields(**order_options)}
        if good_till_date is not None:
            body["goodTillDate"] = _timestamp(good_till_date)
        return (await self._request("POST", "/api/v1/workingorders", json=body)).json()["dealReference"]

    async def update_working_order(self, deal_id: str, *, level: float | None = None, good_till_date: "datetime | str | None" = None, **order_options) -> str:
        """Change the level, expiry or stop / profit of a pending order; at least one setting is required."""
        fields = _order_fields(**order_options)
        if level is not None:
            fields["level"] = level
        if good_till_date is not None:
            fields["goodTillDate"] = _timestamp(good_till_date)
        if not fields:
            raise ValueError("update_working_order needs at least one of level, good_till_date or a stop / profit setting")
        return (await self._request("PUT", f"/api/v1/workingorders/{deal_id}", json=fields)).json()["dealReference"]

    async def cancel_working_order(self, deal_id: str) -> str:
        """Cancel a pending order; returns the deal reference. Unknown / already gone: 404 ``error.not-found.dealId``."""
        return (await self._request("DELETE", f"/api/v1/workingorders/{deal_id}")).json()["dealReference"]

    # ---- history ----
    async def get_close_event(self, deal_id: str, *, last_period: int = 86400) -> "CloseEvent | None":
        """How a position ended: stop loss, take profit, a close request, or the broker. ``None`` when there is no sign it closed
        (still open, unknown, or older than ``last_period`` seconds).

        The activity of a deal starts with the entry that opened it; a later accepted position entry is its close, and an entry
        whose source is ``SL`` / ``TP`` / ``CLOSE_OUT`` is a close by itself."""
        entries = [a for a in await self.get_activity(deal_id=deal_id, detailed=True, last_period=last_period)
                   if a.get("type") == "POSITION" and a.get("status") == "ACCEPTED"]
        entries.sort(key=lambda a: a.get("dateUTC") or "")
        closers = [a for a in entries if a.get("source") in ("SL", "TP", "CLOSE_OUT")]
        if closers:
            return CloseEvent.from_activity(closers[-1])
        if len(entries) >= 2:
            return CloseEvent.from_activity(entries[-1])
        return None

    async def get_activity(
        self,
        *,
        from_: "datetime | str | None" = None,
        to: "datetime | str | None" = None,
        last_period: int | None = None,
        detailed: bool = False,
        deal_id: str | None = None,
        filter: str | None = None,
    ) -> list[dict]:
        """Account activity (positions, working orders, edits) with each deal's ``status``
        (ACCEPTED, EXECUTED, REJECTED, CANCELLED, EXPIRED, ...). ``from_`` / ``to`` are UTC, at most 1 day apart;
        ``last_period`` is in seconds (default 600, max 86400); ``filter`` is a FIQL expression."""
        params = {}
        if from_ is not None:
            params["from"] = _timestamp(from_)
        if to is not None:
            params["to"] = _timestamp(to)
        if last_period is not None:
            params["lastPeriod"] = last_period
        if detailed:
            params["detailed"] = "true"
        if deal_id is not None:
            params["dealId"] = deal_id
        if filter is not None:
            params["filter"] = filter
        return (await self._request("GET", "/api/v1/history/activity", params=params)).json().get("activities", [])
