"""Async client for the Capital.com REST API."""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx

from .enums import TradeDirection, TradeMode
from .errors import APIError, AuthError, CapitalError

log = logging.getLogger("capital_hook.client")

LIVE_HOST = "https://api-capital.backend-capital.com"
DEMO_HOST = "https://demo-api-capital.backend-capital.com"
STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"


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
    async def get_markets(self) -> list[dict]:
        return (await self._request("GET", "/api/v1/markets")).json().get("markets", [])

    async def get_instrument_types(self) -> dict[str, str]:
        """Map of epic -> instrumentType for every tradable market."""
        return {m["epic"]: m["instrumentType"] for m in await self.get_markets()}

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
    async def get_positions(self) -> list[dict]:
        """Open positions as flat dicts: epic, size, direction, pnl, open_price, deal_id, created_date."""
        raw = (await self._request("GET", "/api/v1/positions")).json().get("positions", [])
        return [
            {
                "epic": p["market"]["epic"],
                "size": float(p["position"]["size"]),
                "direction": p["position"]["direction"],
                "pnl": float(p["position"]["upl"]),
                "open_price": float(p["position"]["level"]),
                "deal_id": p["position"]["dealId"],
                "created_date": p["position"]["createdDateUTC"],
            }
            for p in raw
        ]

    async def open_position(self, epic: str, size: float, direction: TradeDirection | str) -> str:
        """Open a market position; returns the deal *reference* (see :meth:`find_deal_id`)."""
        direction = direction.value if isinstance(direction, TradeDirection) else direction
        response = await self._request("POST", "/api/v1/positions", json={"epic": epic, "direction": direction, "size": str(size)})
        return response.json()["dealReference"]

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
