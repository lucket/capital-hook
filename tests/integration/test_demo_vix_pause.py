"""DEMO suite, VIX while it is NOT trading (the daily pause 21:00-22:00 UTC Mon-Thu, or the weekend): what does Capital.com do with orders?

These tests only run while VIX is closed (they skip when it trades), because that is the only time the answers can be recorded:

* an order at market while closed: refused at once (HTTP error) or a reference that is rejected afterwards - and with which reason;
* a size that is not a whole number of contracts (VIX increment is 1);
* a working order placed during the closure (it would fill when the market reopens);
* closing an open position during the closure (needs the two-step sequence below).

To learn whether a position can be CLOSED during the pause, one has to exist before it starts. Sequence (all steps are opt-in):

  1. while VIX trades (before 21:00 UTC):   CAPITAL_DEMO_VIX_HOLD=1 pytest -m demo tests/integration/test_demo_vix_pause.py -k hold_one
  2. during the pause (21:00-22:00 UTC):    pytest -m demo tests/integration/test_demo_vix_pause.py -k close_during
  3. after it reopens:                      CAPITAL_DEMO_VIX_RELEASE=1 pytest -m demo tests/integration/test_demo_vix_pause.py -k release

Step 1 leaves one VIX contract open on the DEMO account on purpose (its id is kept in tests/integration/.demo-vix-hold.json, not in the journal);
``CAPITAL_DEMO_EPIC=VIX python tests/integration/demo_cleanup.py --yes`` closes it too.
"""
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

import demo_harness as h
from capital_hook import APIError

pytestmark = [pytest.mark.demo, pytest.mark.asyncio(loop_scope="session")]

VIX = os.environ.get("CAPITAL_DEMO_VIX_EPIC", "VIX")
REAL_HOLD_FILE = Path(__file__).parent / ".demo-vix-hold.json"


def hold_path(env=os.environ, dry_run: bool | None = None) -> Path:
    """Where the held position is recorded. The harness's own dry run (against the fake) must NEVER touch the real record of the real held position,
    so a dry run gets a private temporary file unless a path is given explicitly."""
    explicit = env.get("CAPITAL_DEMO_VIX_HOLD_PATH")
    if explicit:
        return Path(explicit)
    if h.dry_run(env) if dry_run is None else dry_run:
        return Path(tempfile.mkdtemp(prefix="vix-hold-")) / "hold.json"
    return REAL_HOLD_FILE


HOLD_FILE = hold_path()


async def require(demo_client, demo_fake, want: str) -> dict:
    """Skip unless VIX is in the wanted phase ("open" / "closed"). In the harness's own dry run the fake is put in that phase."""
    if demo_fake is not None:
        demo_fake.market_status[VIX] = "TRADEABLE" if want == "open" else "CLOSED"
    market = await demo_client.get_market(VIX)
    status = market["snapshot"].get("marketStatus")
    if (status == "TRADEABLE") != (want == "open"):
        pytest.skip(f"{VIX} is {status}; this test needs it {'trading' if want == 'open' else 'closed (the daily pause or the weekend)'}")
    return market


def now_utc():
    return datetime.now(timezone.utc).strftime("%a %H:%M:%S")


@pytest_asyncio.fixture(loop_scope="session")           # the demo connection lives on the session loop: a fixture that uses it must run there too
async def vix_tracker(demo_client, journal):
    """Creates VIX deals through the harness (journalled, cleaned up). Refuses to run if VIX already holds something that is not ours."""
    held = json.loads(HOLD_FILE.read_text())["deal_id"] if HOLD_FILE.exists() else None
    others = [p["deal_id"] for p in await demo_client.get_positions() if p["epic"] == VIX and p["deal_id"] != held]
    others += [o["deal_id"] for o in await demo_client.get_working_orders() if o["epic"] == VIX]
    if others:
        pytest.skip(f"{VIX} already has positions / orders on the demo account that this suite did not create: {others}")
    tracker = h.Tracker(demo_client, journal)                                # no epic: it must not close the held position
    yield tracker
    await tracker.cleanup()


async def try_open(client, tracker, size, direction="BUY"):
    """Open and describe what happened, whatever it was."""
    started = time.monotonic()
    try:
        reference = await client.open_position(VIX, size, direction)
    except APIError as exc:
        return {"refused_at_once": {"status": exc.status_code, "error_code": exc.error_code}}
    tracker._remember_reference(reference)
    try:
        confirmation = await client.confirm(reference, attempts=8, delay=1.0)
    except APIError as exc:
        return {"reference_prefix": reference[:2], "confirm_error": {"status": exc.status_code, "error_code": exc.error_code}}
    if confirmation.position_deal_id:
        tracker.positions.add(confirmation.position_deal_id)
        tracker.journal.add("position", confirmation.position_deal_id)
    return {"reference_prefix": reference[:2], "deal_status": confirmation.deal_status, "reason": confirmation.reason, "status": confirmation.status,
            "opened_a_position": bool(confirmation.position_deal_id), "seconds_to_resolve": round(time.monotonic() - started, 1)}


async def test_a_market_order_while_vix_is_closed(demo_client, demo_fake, vix_tracker, recorder):
    market = await require(demo_client, demo_fake, "closed")
    outcome = await try_open(demo_client, vix_tracker, 1)
    recorder.note("vix_pause_market_order", {"at_utc": now_utc(), "market_status": market["snapshot"].get("marketStatus"), **outcome})
    assert not outcome.get("opened_a_position"), "an order at market opened a position while the market was closed"


async def test_a_fractional_size_while_vix_is_closed(demo_client, demo_fake, vix_tracker, recorder):
    await require(demo_client, demo_fake, "closed")
    outcome = await try_open(demo_client, vix_tracker, 1.5)
    recorder.note("vix_pause_fractional_size", {"at_utc": now_utc(), **outcome})
    assert not outcome.get("opened_a_position")


async def test_a_working_order_while_vix_is_closed(demo_client, demo_fake, vix_tracker, recorder):
    market = await require(demo_client, demo_fake, "closed")
    snapshot = market["snapshot"]
    level = h.level(float(snapshot["bid"]), 0.7, int(snapshot.get("decimalPlacesFactor", 2)))        # far below: it should never fill
    note = {"at_utc": now_utc(), "level": level}
    try:
        _, confirmation = await vix_tracker.create_order(VIX, "BUY", 1, level)
        note.update({"deal_status": confirmation.deal_status, "reason": confirmation.reason, "listed": any(
            o["deal_id"] == confirmation.deal_id for o in await demo_client.get_working_orders())})
    except APIError as exc:
        note["refused_at_once"] = {"status": exc.status_code, "error_code": exc.error_code}
    recorder.note("vix_pause_working_order", note)


async def test_hold_one_contract_to_test_closing_during_the_pause(demo_client, demo_fake, recorder):
    if os.environ.get("CAPITAL_DEMO_VIX_HOLD") != "1" and not h.dry_run():
        pytest.skip("opens ONE VIX contract and leaves it open on purpose: set CAPITAL_DEMO_VIX_HOLD=1 (see the module docstring)")
    await require(demo_client, demo_fake, "open")
    if HOLD_FILE.exists():
        pytest.skip(f"a held position is already recorded in {HOLD_FILE.name}")
    if any(p["epic"] == VIX for p in await demo_client.get_positions()):
        pytest.skip(f"{VIX} already has a position on the demo account")
    reference = await demo_client.open_position(VIX, 1, "BUY")
    confirmation = await demo_client.confirm(reference)
    assert confirmation.accepted and confirmation.position_deal_id, f"the open was not accepted: {confirmation.reason}"
    HOLD_FILE.write_text(json.dumps({"deal_id": confirmation.position_deal_id, "opened_at_utc": now_utc()}))
    recorder.note("vix_hold", {"opened_at_utc": now_utc(), "recorded_in": HOLD_FILE.name})


async def test_close_during_the_pause_the_position_held_from_before(demo_client, demo_fake, recorder):
    if not HOLD_FILE.exists():
        pytest.skip("no held position: run the hold step first (see the module docstring)")
    await require(demo_client, demo_fake, "closed")
    deal_id = json.loads(HOLD_FILE.read_text())["deal_id"]
    note = {"at_utc": now_utc()}
    if demo_fake is not None:                                                  # the harness's own dry run: pretend the broker refuses it once
        demo_fake.fail_next("DELETE", "/api/v1/positions", status=400, error_code="error.market.closed")
    try:
        answer = await demo_client.close_position(deal_id)
        note["close_reference_prefix"] = answer["dealReference"][:2]
        try:
            confirmation = await demo_client.confirm(answer["dealReference"], attempts=8, delay=1.0)
            note.update({"deal_status": confirmation.deal_status, "reason": confirmation.reason, "status": confirmation.status})
        except APIError as exc:
            note["confirm_error"] = {"status": exc.status_code, "error_code": exc.error_code}
        still_open = any(p["deal_id"] == deal_id for p in await demo_client.get_positions())
        note["still_open_afterwards"] = still_open
        if not still_open:
            HOLD_FILE.unlink()
    except APIError as exc:
        note["refused_at_once"] = {"status": exc.status_code, "error_code": exc.error_code}
        note["still_open_afterwards"] = True
    try:                                                                       # can its stop / profit be edited while closed?
        market = await demo_client.get_market(VIX)
        bid = float(market["snapshot"]["bid"])
        await demo_client.update_position(deal_id, stop_level=round(bid * 0.7, 2))
        note["edit_stop_during_pause"] = "accepted"
    except APIError as exc:
        note["edit_stop_during_pause"] = {"status": exc.status_code, "error_code": exc.error_code}
    recorder.note("vix_pause_close", note)


async def test_release_the_held_position_once_vix_trades_again(demo_client, demo_fake, recorder):
    if os.environ.get("CAPITAL_DEMO_VIX_RELEASE") != "1" and not h.dry_run():
        pytest.skip("closes the VIX contract that was held on purpose: set CAPITAL_DEMO_VIX_RELEASE=1")
    if not HOLD_FILE.exists():
        pytest.skip("no held position")
    await require(demo_client, demo_fake, "open")
    deal_id = json.loads(HOLD_FILE.read_text())["deal_id"]
    try:
        await demo_client.close_position(deal_id)
    except APIError as exc:
        if exc.status_code != 404:                                              # already gone is fine
            raise
    assert not any(p["deal_id"] == deal_id for p in await demo_client.get_positions())
    HOLD_FILE.unlink()
    recorder.note("vix_hold_released", {"at_utc": now_utc()})
