"""The opt-in DEMO suite's own safety machinery, tested offline (no network, no credentials)."""
import asyncio
import json

import httpx
import pytest

import demo_harness as h
from capital_hook import APIError, CapitalClient


# ---------------- opt-in rules ----------------
def test_missing_requirements_asks_for_every_missing_variable_by_name():
    reason = h.missing_requirements({})
    assert all(name in reason for name in h.REQUIRED_ENV)


def test_missing_requirements_needs_the_explicit_allow_flag_even_with_credentials():
    env = {name: "x" for name in h.REQUIRED_ENV}
    assert "CAPITAL_DEMO_ALLOW=1" in h.missing_requirements(env)
    assert h.missing_requirements({**env, "CAPITAL_DEMO_ALLOW": "1"}) is None
    assert "CAPITAL_DEMO_ALLOW=1" in h.missing_requirements({**env, "CAPITAL_DEMO_ALLOW": "yes"})


def test_the_message_never_contains_a_credential_value():
    env = {"CAPITAL_DEMO_IDENTITY": "secret-identity", "CAPITAL_DEMO_PASSWORD": "secret-password"}
    assert "secret" not in h.missing_requirements(env)


def test_dry_run_is_an_internal_switch_that_bypasses_the_credential_check():
    assert h.missing_requirements({h.DRY_RUN_ENV: "1"}) is None and h.dry_run({h.DRY_RUN_ENV: "1"}) and not h.dry_run({})


# ---------------- shapes & recorder ----------------
def test_shape_of_keeps_structure_and_drops_values():
    assert h.shape_of({"b": [{"x": 1.5, "y": "s"}], "a": True, "n": None, "i": 3}) == {"a": "bool", "b": [{"x": "float", "y": "str"}], "i": "int", "n": "null"}
    assert h.shape_of([]) == [] and h.shape_of({}) == {}


def test_merge_shapes_is_a_union_and_marks_conflicting_types():
    assert h.merge_shapes({"a": "int", "b": "str"}, {"a": "int", "c": "bool"}) == {"a": "int", "b": "str", "c": "bool"}
    assert h.merge_shapes({"a": "int"}, {"a": "null"}) == {"a": "int|null"}
    assert h.merge_shapes([{"x": "int"}], [{"y": "str"}]) == [{"x": "int", "y": "str"}] and h.merge_shapes([], [{"x": "int"}]) == [{"x": "int"}]


@pytest.mark.parametrize("path, normalized", [
    ("/api/v1/positions", "/positions"), ("/api/v1/positions/DEAL123", "/positions/{id}"), ("/api/v1/workingorders/abc-1", "/workingorders/{id}"),
    ("/api/v1/confirms/o_ref", "/confirms/{id}"), ("/api/v1/markets/BTCUSD", "/markets/{id}"), ("/api/v1/markets", "/markets"),
    ("/api/v1/accounts/preferences", "/accounts/preferences"), ("/api/v1/session", "/session")])
def test_normalize_path_hides_ids_and_epics(path, normalized):
    assert h.normalize_path(path) == normalized


def test_recorder_keeps_no_values_only_shapes_and_error_codes(tmp_path):
    rec = h.Recorder()
    rec.observe("POST", "/api/v1/session", 200, {"clientId": "SECRET-CLIENT", "accounts": [{"accountId": "ACCT-99", "accountName": "Luca"}], "accountInfo": {"balance": 123456.78}})
    rec.observe("DELETE", "/api/v1/positions/DEAL-SECRET", 404, {"errorCode": "error.not-found.dealId"})
    rec.note("fact", {"hedging": False})
    rec.dump(tmp_path / "observed.json", tmp_path / "shapes.json")
    text = (tmp_path / "observed.json").read_text() + (tmp_path / "shapes.json").read_text()
    for secret in ("SECRET-CLIENT", "ACCT-99", "Luca", "123456", "DEAL-SECRET"):
        assert secret not in text
    data = json.loads((tmp_path / "observed.json").read_text())
    assert data["error_codes"] == {"DELETE /positions/{id} 404": ["error.not-found.dealId"]}
    assert data["shapes"]["POST /session 200"]["accountInfo"] == {"balance": "float"} and data["notes"] == {"fact": {"hedging": False}}
    assert set(json.loads((tmp_path / "shapes.json").read_text())) == set(data["shapes"])


def test_recorder_notes_must_be_plain_data():
    with pytest.raises(TypeError):
        h.Recorder().note("bad", object())


async def test_recorder_hook_records_responses_without_consuming_them():
    rec = h.Recorder()
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "OK"}))
    async with httpx.AsyncClient(transport=transport, event_hooks={"response": [rec.on_response]}) as http:
        response = await http.get("https://x/api/v1/ping")
    assert response.json() == {"status": "OK"} and rec.shapes == {"GET /ping 200": {"status": "str"}}


async def test_recorder_hook_tolerates_a_non_json_body():
    rec = h.Recorder()
    transport = httpx.MockTransport(lambda r: httpx.Response(502, text="Bad Gateway"))
    async with httpx.AsyncClient(transport=transport, event_hooks={"response": [rec.on_response]}) as http:
        await http.get("https://x/api/v1/ping")
    assert rec.shapes == {"GET /ping 502": "null"}


# ---------------- opening-hours probe ----------------
def test_wrapping_sessions_finds_ranges_that_end_before_they_start():
    assert h.wrapping_sessions({"mon": ["22:00 - 02:00", "03:00 - 05:00"], "tue": ["00:00 - 23:59"], "sat": []}) == ["mon 22:00 - 02:00"]
    assert h.wrapping_sessions(None) == [] and h.wrapping_sessions({"mon": ["garbage"]}) == []


def test_level_rounds_to_the_market_precision():
    assert h.level(1234.5678, 0.7, 2) == 864.2 and h.level(100, 1.3, 0) == 130


# ---------------- throttle ----------------
async def test_throttle_spaces_requests_and_session_logins(monkeypatch):
    sleeps = []
    clock = [100.0]

    async def fake_sleep(seconds):
        sleeps.append(round(seconds, 3))
        clock[0] += seconds

    monkeypatch.setattr(h.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    throttle = h.Throttle(min_interval=0.2, session_interval=1.0)

    def req(method, path):
        return httpx.Request(method, f"https://x{path}")

    await throttle.on_request(req("GET", "/api/v1/accounts"))
    await throttle.on_request(req("GET", "/api/v1/accounts"))            # too soon: waits ~0.2
    await throttle.on_request(req("POST", "/api/v1/session"))
    await throttle.on_request(req("POST", "/api/v1/session"))            # second login: waits ~1.0
    assert sleeps[0] == pytest.approx(0.2, abs=0.001) and sleeps[-1] == pytest.approx(1.0, abs=0.001)


# ---------------- journal, sweep, tracker ----------------
def test_journal_survives_and_deduplicates(tmp_path):
    journal = h.Journal(tmp_path / "j.json")
    assert journal.entries() == []
    journal.add("position", "D1")
    journal.add("position", "D1")
    journal.add("order", "O1")
    assert journal.entries() == [{"kind": "position", "id": "D1"}, {"kind": "order", "id": "O1"}]
    assert h.Journal(tmp_path / "j.json").entries() == journal.entries()          # a new process sees it
    journal.remove("position", "D1")
    assert journal.entries() == [{"kind": "order", "id": "O1"}]
    journal.clear()
    assert journal.entries() == []


def test_journal_ignores_a_corrupt_file(tmp_path):
    (tmp_path / "j.json").write_text("{not json")
    assert h.Journal(tmp_path / "j.json").entries() == []


@pytest.fixture
def journal(tmp_path):
    return h.Journal(tmp_path / "journal.json")


async def test_sweep_closes_and_cancels_what_a_crashed_run_left(client, fake, journal):
    await client.open_position("GOLD", 1, "BUY")
    (position_id,) = fake.positions
    await client.create_working_order("GOLD", "BUY", 1, 1000)
    (order_id,) = fake.orders
    reference = await client.open_position("GOLD", 2, "SELL")                     # outcome never recorded, only its reference
    (reference_deal,) = [d for d in fake.positions if d != position_id]
    for kind, ident in (("position", position_id), ("order", order_id), ("reference", reference), ("position", "already-gone")):
        journal.add(kind, ident)
    done = await h.sweep(client, journal)
    assert sorted(done) == sorted([f"closed {position_id}", f"cancelled {order_id}", f"closed {reference_deal}"])
    assert fake.positions == {} and fake.orders == {} and journal.entries() == []


async def test_sweep_with_an_empty_journal_makes_no_request(client, fake, journal):
    assert await h.sweep(client, journal) == [] and fake.requests == []


async def test_tracker_open_journals_the_reference_before_confirming(client, fake, journal):
    tracker = h.Tracker(client, journal)
    seen = []
    original = client.confirm

    async def spy(reference, **kwargs):
        seen.append([e["kind"] for e in journal.entries()])
        return await original(reference, **kwargs)

    client.confirm = spy
    _, confirmation = await tracker.open("GOLD", 1, "BUY")
    assert seen == [["reference"]]                                                # on disk before anything else happens
    assert tracker.positions == {confirmation.position_deal_id} and {"kind": "position", "id": confirmation.position_deal_id} in journal.entries()


async def test_tracker_cleanup_closes_positions_cancels_orders_and_clears_the_journal(client, fake, journal):
    tracker = h.Tracker(client, journal)
    await tracker.open("GOLD", 1, "BUY")
    await tracker.create_order("GOLD", "BUY", 1, 1000)
    await client.open_position("GOLD", 5, "BUY")                                 # NOT tracked: belongs to somebody else
    foreign = {d for d, p in fake.positions.items() if p["size"] == 5.0}
    await tracker.cleanup()
    assert set(fake.positions) == foreign and fake.orders == {} and journal.entries() == []


async def test_tracker_cleanup_tolerates_deals_that_are_already_gone(client, fake, journal):
    tracker = h.Tracker(client, journal)
    _, confirmation = await tracker.open("GOLD", 1, "BUY")
    await client.close_position(confirmation.position_deal_id)
    await tracker.cleanup()
    assert tracker.positions == set()


async def test_tracker_cleanup_still_raises_real_errors(client, fake, journal):
    tracker = h.Tracker(client, journal)
    await tracker.open("GOLD", 1, "BUY")
    fake.fail_close = True
    with pytest.raises(APIError):
        await tracker.cleanup()


async def test_tracker_adopt_positions_tracks_side_effect_deals(client, fake, journal):
    tracker = h.Tracker(client, journal)
    await client.open_position("GOLD", 1, "BUY")
    tracker.adopt_positions(list(fake.positions))
    await tracker.cleanup()
    assert fake.positions == {}


async def test_tracker_with_an_epic_closes_everything_left_on_it_even_if_ids_were_not_tracked(client, fake, journal):
    """Safety net: the suite verified the epic was empty before it started, so anything on it at cleanup is ours."""
    tracker = h.Tracker(client, journal, epic="GOLD")
    fake.set_quote("US100", 15000.0, 15001.0)
    await client.open_position("GOLD", 1, "BUY")                                 # opened behind the tracker's back (e.g. an id mix-up)
    await client.create_working_order("GOLD", "BUY", 1, 1000)
    await client.open_position("US100", 1, "BUY")                                # another market: left alone
    await tracker.cleanup()
    assert [p["epic"] for p in fake.positions.values()] == ["US100"] and fake.orders == {}
