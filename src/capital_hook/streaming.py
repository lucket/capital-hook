"""Live price streaming over the Capital.com WebSocket."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from websockets.asyncio.client import ClientConnection, connect

from .client import STREAM_URL, CapitalClient
from .errors import CapitalError, StreamError

log = logging.getLogger("capital_hook.stream")


@dataclass(frozen=True)
class Quote:
    epic: str
    bid: float
    ask: float
    timestamp: float


QuoteCallback = Callable[[Quote], "Awaitable[None] | None"]


class _Connection:
    """One WebSocket carrying up to `max_per_connection` epics; reconnects by itself."""

    def __init__(self, stream: "PriceStream"):
        self._stream = stream
        self.epics: set[str] = set()
        self._ws: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._closing = False
        self._runner = asyncio.create_task(self._run())
        self._pinger = asyncio.create_task(self._ping_loop()) if stream.ping_every else None

    # ---- public ----
    async def wait_connected(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            raise StreamError(f"could not connect to the price stream within {timeout:g}s") from None

    async def subscribe(self, epic: str) -> None:
        self.epics.add(epic)
        if self._ws is not None:
            await self._send("marketData.subscribe", epic)

    async def unsubscribe(self, epic: str) -> None:
        self.epics.discard(epic)
        if self._ws is not None:
            await self._send("marketData.unsubscribe", epic)

    async def ping(self) -> None:
        if self._ws is not None:
            await self._send("ping")

    async def close(self) -> None:
        self._closing = True
        for task in (self._pinger, self._runner):
            if task:
                task.cancel()
        for task in (self._pinger, self._runner):
            if task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        self._connected.clear()

    # ---- internals ----
    async def _send(self, destination: str, epic: str | None = None) -> None:
        cst, token = self._stream.client.session_tokens
        message = {"destination": destination, "correlationId": f"{destination}_{epic or 'ping'}", "cst": cst, "securityToken": token}
        if epic is not None:
            message["payload"] = {"epics": [epic]}
        await self._ws.send(json.dumps(message))  # type: ignore[union-attr]

    async def _run(self) -> None:
        delay = self._stream.reconnect_delay
        while not self._closing:
            try:
                self._stream.client.session_tokens          # need a session before connecting
                async with connect(self._stream.url, ping_interval=60, ping_timeout=30) as ws:
                    self._ws = ws
                    for epic in list(self.epics):
                        await self._send("marketData.subscribe", epic)
                    self._connected.set()
                    delay = self._stream.reconnect_delay
                    log.info("price stream connected (%d epics)", len(self.epics))
                    async for raw in ws:
                        await self._handle(raw)
                log.warning("price stream closed by server; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure -> reconnect
                log.warning("price stream error: %s; reconnecting in %.1fs", exc, delay)
                delay = min(delay * 2, 30.0)
            finally:
                self._ws = None
                self._connected.clear()
            if not self._closing:
                await asyncio.sleep(delay)

    async def _handle(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
            destination = data.get("destination")
            if destination == "quote":
                p = data["payload"]
                quote = Quote(epic=p["epic"], bid=float(p["bid"]), ask=float(p["ofr"]), timestamp=p.get("timestamp", 0))
                await self._stream._emit(quote)
            elif data.get("status") not in (None, "OK"):
                log.warning("stream message rejected: %s", data)
            else:
                log.debug("stream message: %s", destination)
        except Exception:  # noqa: BLE001 - a bad message must not kill the stream
            log.exception("could not process stream message: %r", raw)

    async def _ping_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(self._stream.ping_every)
            try:
                await self.ping()
            except Exception as exc:  # noqa: BLE001
                log.warning("ping failed: %s", exc)


class PriceStream:
    """Subscribe to live quotes; `on_quote` is called for every price update.

    Epics are spread over several WebSocket connections (Capital.com allows 40
    per connection). Connections are created on demand, reconnect automatically
    and resubscribe their epics. The REST client supplies the session tokens, so
    log in (``await client.login()``) before subscribing.

    ``on_quote`` may be a plain function or a coroutine function.
    """

    def __init__(
        self,
        client: CapitalClient,
        on_quote: QuoteCallback,
        *,
        url: str = STREAM_URL,
        max_per_connection: int = 40,
        connect_timeout: float = 30.0,
        reconnect_delay: float = 1.0,
        ping_every: float | None = 300.0,
        seed_from_rest: bool = True,
    ):
        self.client = client
        self.on_quote = on_quote
        self.url = url
        self.max_per_connection = max_per_connection
        self.connect_timeout = connect_timeout
        self.reconnect_delay = reconnect_delay
        self.ping_every = ping_every
        self.seed_from_rest = seed_from_rest
        self._connections: list[_Connection] = []

    @property
    def subscribed_epics(self) -> set[str]:
        return set().union(*(c.epics for c in self._connections)) if self._connections else set()

    async def subscribe(self, epic: str) -> None:
        """Start streaming `epic`. Returns once the stream is connected and the epic requested.

        With ``seed_from_rest`` (default) the latest REST price is delivered to
        `on_quote` immediately, so a price exists before the first tick arrives.
        Raises :class:`StreamError` if the stream cannot be reached in `connect_timeout`.
        """
        if epic in self.subscribed_epics:
            return
        conn = next((c for c in self._connections if len(c.epics) < self.max_per_connection), None)
        if conn is None:
            conn = _Connection(self)
            self._connections.append(conn)
        await conn.wait_connected(self.connect_timeout)
        await conn.subscribe(epic)
        if self.seed_from_rest:
            try:
                ask, bid = await self.client.get_prices(epic)
                await self._emit(Quote(epic=epic, bid=bid, ask=ask, timestamp=0))
            except CapitalError as exc:
                log.warning("could not seed %s from REST: %s", epic, exc)

    async def unsubscribe(self, epic: str) -> None:
        for conn in self._connections:
            if epic in conn.epics:
                await conn.unsubscribe(epic)
                return

    async def ping_all(self) -> None:
        """Send a keep-alive on every connection (also done automatically every `ping_every` seconds)."""
        for conn in self._connections:
            try:
                await conn.ping()
            except Exception as exc:  # noqa: BLE001
                log.warning("ping failed: %s", exc)

    async def aclose(self) -> None:
        for conn in self._connections:
            await conn.close()
        self._connections.clear()

    async def _emit(self, quote: Quote) -> None:
        result = self.on_quote(quote)
        if inspect.isawaitable(result):
            await result
