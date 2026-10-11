"""Safety and bookkeeping helpers for the opt-in DEMO suite (``tests/integration``).

The suite trades on a real Capital.com **demo** account, so everything here exists to make that safe:

* it only runs when explicitly enabled (``missing_requirements``);
* every reference / deal id it creates is written to a journal *before* it is used, so a crash can be cleaned up
  at the next start (``Journal`` + ``sweep``);
* tests create deals through a ``Tracker`` whose ``cleanup`` always closes / cancels what the test left behind;
* requests are throttled below Capital.com's rate limits (``Throttle``);
* what the real API did is recorded as plain facts (``Recorder``) - never credentials, never account identifiers.
"""
import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path

from capital_hook import APIError

REQUIRED_ENV = ("CAPITAL_DEMO_IDENTITY", "CAPITAL_DEMO_PASSWORD", "CAPITAL_DEMO_API_KEY")
ALLOW_ENV = "CAPITAL_DEMO_ALLOW"
DRY_RUN_ENV = "CAPITAL_HOOK_DEMO_DRY_RUN"          # internal: run the suite against FakeCapital to test the harness itself

HERE = Path(__file__).parent
JOURNAL_PATH = Path(os.environ.get("CAPITAL_DEMO_JOURNAL_PATH") or HERE / "integration" / ".demo-journal.json")
OBSERVED_PATH = Path(os.environ.get("CAPITAL_DEMO_OBSERVED_PATH") or HERE / "integration" / "observed.json")
SHAPES_PATH = Path(os.environ.get("CAPITAL_DEMO_SHAPES_PATH") or HERE / "fixtures" / "shapes.json")


def dry_run(env=os.environ) -> bool:
    return env.get(DRY_RUN_ENV) == "1"


def missing_requirements(env=os.environ) -> str | None:
    """Why the suite must not run (a skip message), or None when it may."""
    if dry_run(env):
        return None
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        return f"DEMO suite is opt-in: set {', '.join(missing)}"
    if env.get(ALLOW_ENV) != "1":
        return f"DEMO suite places real orders on the DEMO account: set {ALLOW_ENV}=1 to allow it"
    return None


# ---------------------------------------------------------------- shapes
def shape_of(value):
    """The structure of a JSON value without any of its data."""
    if isinstance(value, dict):
        return {k: shape_of(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape_of(value[0])] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "null"


def merge_shapes(a, b):
    """Union of two shapes (several responses of the same endpoint)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: merge_shapes(a[k], b[k]) if k in a and k in b else (a.get(k) if k in a else b[k]) for k in sorted(set(a) | set(b))}
    if isinstance(a, list) and isinstance(b, list):
        if not a or not b:
            return a or b
        return [merge_shapes(a[0], b[0])]
    if a == b:
        return a
    kinds = sorted(set(str(a).split("|")) | set(str(b).split("|")))
    return "|".join(kinds)


_ID_AFTER = {"positions", "workingorders", "confirms", "markets"}


def normalize_path(path: str) -> str:
    """``/api/v1/positions/ABC123`` -> ``/positions/{id}`` (ids and epics never end up in the record)."""
    parts = [p for p in path.split("/") if p]
    if parts[:2] == ["api", "v1"]:
        parts = parts[2:]
    out, previous = [], None
    for part in parts:
        out.append("{id}" if previous in _ID_AFTER else part)
        previous = part
    return "/" + "/".join(out)


# ---------------------------------------------------------------- recorder
class Recorder:
    """Collects what the real API did: response shapes, error codes and named observations."""

    def __init__(self):
        self.notes: dict = {}
        self.shapes: dict = {}
        self.error_codes: dict = {}

    def note(self, key: str, value) -> None:
        json.dumps(value)                      # must be plain data
        self.notes[key] = value

    def observe(self, method: str, path: str, status: int, body) -> None:
        key = f"{method} {normalize_path(path)} {status}"
        if isinstance(body, dict) and "errorCode" in body:
            self.error_codes.setdefault(key, [])
            if body["errorCode"] not in self.error_codes[key]:
                self.error_codes[key].append(body["errorCode"])
        shape = shape_of(body)
        self.shapes[key] = merge_shapes(self.shapes[key], shape) if key in self.shapes else shape

    async def on_response(self, response) -> None:           # httpx response event hook
        await response.aread()
        try:
            body = response.json()
        except Exception:                                    # noqa: BLE001 - not JSON
            body = None
        self.observe(response.request.method, response.request.url.path, response.status_code, body)

    def dump(self, observed_path: Path = OBSERVED_PATH, shapes_path: Path | None = None) -> None:
        observed_path.parent.mkdir(parents=True, exist_ok=True)
        observed_path.write_text(json.dumps({"notes": self.notes, "error_codes": self.error_codes, "shapes": self.shapes}, indent=2, sort_keys=True) + "\n")
        if shapes_path is not None:
            shapes_path.parent.mkdir(parents=True, exist_ok=True)
            shapes_path.write_text(json.dumps(self.shapes, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- throttle
class Throttle:
    """Request hook: keeps below 10 requests/s overall and POST /session below 1/s."""

    def __init__(self, min_interval: float = 0.15, session_interval: float = 1.2):
        self.min_interval, self.session_interval = min_interval, session_interval
        self._last = 0.0
        self._last_session = 0.0

    async def on_request(self, request) -> None:
        interval = self.min_interval
        if request.method == "POST" and request.url.path.endswith("/session"):
            interval = max(interval, self.session_interval)
            wait = self._last_session + interval - time.monotonic()
        else:
            wait = self._last + interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        now = time.monotonic()
        self._last = now
        if request.method == "POST" and request.url.path.endswith("/session"):
            self._last_session = now


# ---------------------------------------------------------------- journal
class Journal:
    """Everything the suite created, written to disk *before* it is relied upon."""

    def __init__(self, path: Path = JOURNAL_PATH):
        self.path = Path(path)

    def entries(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return []

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".journal-")
        with os.fdopen(fd, "w") as handle:
            json.dump(entries, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def add(self, kind: str, ident: str) -> None:
        entries = self.entries()
        if not any(e["kind"] == kind and e["id"] == ident for e in entries):
            entries.append({"kind": kind, "id": ident})
            self._write(entries)

    def remove(self, kind: str, ident: str) -> None:
        self._write([e for e in self.entries() if not (e["kind"] == kind and e["id"] == ident)])

    def clear(self) -> None:
        self._write([])


async def sweep(client, journal: Journal) -> list[str]:
    """Close / cancel whatever an earlier (crashed) run left behind. Returns what it did."""
    done = []
    entries = journal.entries()
    if not entries:
        return done
    positions = {p["deal_id"] for p in await client.get_positions()}
    orders = {o["deal_id"] for o in await client.get_working_orders()}
    for entry in entries:
        ident = entry["id"]
        if entry["kind"] == "reference":                      # an order whose outcome was never recorded
            try:
                confirmation = await client.confirm(ident, attempts=1)
            except APIError:
                continue
            ident = confirmation.deal_id
            if not ident:
                continue
        if ident in positions:
            await _quiet(client.close_position, ident)
            done.append(f"closed {ident}")
        elif ident in orders:
            await _quiet(client.cancel_working_order, ident)
            done.append(f"cancelled {ident}")
    journal.clear()
    return done


async def _quiet(call, *args):
    try:
        return await call(*args)
    except APIError as exc:
        if exc.status_code != 404:                            # already gone is fine
            raise


# ---------------------------------------------------------------- tracker
class Tracker:
    """Creates deals on behalf of a test and guarantees they are closed / cancelled afterwards."""

    def __init__(self, client, journal: Journal):
        self.client, self.journal = client, journal
        self.positions: set[str] = set()
        self.orders: set[str] = set()
        self.references: set[str] = set()

    async def open(self, epic, size, direction, **options):
        reference = await self.client.open_position(epic, size, direction, **options)
        self._remember_reference(reference)                    # recorded before anything else is done with it
        confirmation = await self.client.confirm(reference)
        if confirmation.deal_id:
            self.positions.add(confirmation.deal_id)
            self.journal.add("position", confirmation.deal_id)
        return reference, confirmation

    async def create_order(self, epic, direction, size, level, kind="LIMIT", **options):
        reference = await self.client.create_working_order(epic, direction, size, level, kind, **options)
        self._remember_reference(reference)
        confirmation = await self.client.confirm(reference)
        if confirmation.deal_id:
            self.orders.add(confirmation.deal_id)
            self.journal.add("order", confirmation.deal_id)
        return reference, confirmation

    def _remember_reference(self, reference: str) -> None:
        self.references.add(reference)
        self.journal.add("reference", reference)

    def adopt_positions(self, deal_ids) -> None:
        """Track positions that appeared as a side effect (e.g. an order that filled)."""
        for deal_id in deal_ids:
            self.positions.add(deal_id)
            self.journal.add("position", deal_id)

    async def cleanup(self) -> None:
        open_now = {p["deal_id"] for p in await self.client.get_positions()}
        for deal_id in sorted(self.positions & open_now):
            await _quiet(self.client.close_position, deal_id)
        pending = {o["deal_id"] for o in await self.client.get_working_orders()}
        for deal_id in sorted(self.orders & pending):
            await _quiet(self.client.cancel_working_order, deal_id)
        for deal_id in self.positions:
            self.journal.remove("position", deal_id)
        for deal_id in self.orders:
            self.journal.remove("order", deal_id)
        for reference in self.references:
            self.journal.remove("reference", reference)
        self.positions.clear()
        self.orders.clear()
        self.references.clear()


def level(price: float, factor: float, decimals: int = 2) -> float:
    """A price level a given factor away from ``price`` (e.g. 0.7 = 30% below), rounded to the market's precision."""
    return round(price * factor, decimals)


_WRAP = re.compile(r"^(\d{1,2}:\d{2}(?::\d{2})?) - (\d{1,2}:\d{2}(?::\d{2})?)$")


def wrapping_sessions(hours: dict) -> list[str]:
    """Opening-hour ranges whose end is before their start (a session running past midnight): ``["mon 22:00 - 02:00"]``."""
    found = []
    for day, ranges in (hours or {}).items():
        for rng in ranges:
            match = _WRAP.match(rng)
            if match and match.group(2) < match.group(1):
                found.append(f"{day} {rng}")
    return found
