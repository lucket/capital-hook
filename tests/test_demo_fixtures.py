"""The DEMO suite's real connection is opened once, on the session-wide event loop. Anything async that uses it must run on that loop too, or the
first real request fails with "bound to a different event loop". The harness's own dry run cannot see this (the fake has no real connection),
so it is checked here, in the source."""
import ast
from pathlib import Path

INTEGRATION = Path(__file__).parent / "integration"


def _decorator_kwargs(decorator):
    if isinstance(decorator, ast.Call):
        return {kw.arg: getattr(kw.value, "value", None) for kw in decorator.keywords}
    return {}


def _is_fixture(decorator) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return isinstance(target, ast.Attribute) and target.attr == "fixture" or isinstance(target, ast.Name) and target.id == "fixture"


def test_every_async_fixture_of_the_demo_suite_runs_on_the_session_loop():
    offenders = []
    for path in sorted(INTEGRATION.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.AsyncFunctionDef,)):
                fixtures = [d for d in node.decorator_list if _is_fixture(d)]
                if fixtures and not any(_decorator_kwargs(d).get("loop_scope") == "session" for d in fixtures):
                    offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert not offenders, "async fixtures that do not use loop_scope='session': " + ", ".join(offenders)


def test_the_check_itself_sees_a_plain_async_fixture():
    source = "import pytest\n@pytest.fixture\nasync def f(): ...\n"
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef))
    assert any(_is_fixture(d) for d in node.decorator_list) and not any(_decorator_kwargs(d).get("loop_scope") == "session" for d in node.decorator_list)


# ---------------------------------------------------------------- the --watch mode of the status diagnostic
def _load_status_script():
    import importlib.util
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    spec = importlib.util.spec_from_file_location("demo_market_status", INTEGRATION / "demo_market_status.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_watch_prints_each_change_of_status_and_stops_when_the_market_opens():
    from datetime import datetime, timedelta

    from capital_hook import CapitalClient
    from capital_hook.testing import FakeCapital

    script = _load_status_script()
    fake = FakeCapital()
    fake.market_status["GOLD"] = "CLOSED"
    client = CapitalClient("me", "pw", "key", http=fake.client())
    clock, lines, polls = [datetime(2026, 10, 12, 21, 59, 50)], [], [0]

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        polls[0] += 1
        if polls[0] == 4:
            fake.market_status["GOLD"] = "TRADEABLE"                   # the status flips 40 s after the watch began

    final = await script.watch(client, "GOLD", interval=10, sleep=sleep, now=lambda: clock[0], out=lines.append)
    assert final == "TRADEABLE" and polls[0] == 4
    assert lines[0].endswith("GOLD: CLOSED") and lines[1].endswith("GOLD: TRADEABLE") and lines[2] == "GOLD is TRADEABLE 40 s after the watch began"
    assert len(lines) == 3                                             # unchanged polls print nothing


async def test_watch_can_give_up_after_a_number_of_polls():
    from capital_hook import CapitalClient
    from capital_hook.testing import FakeCapital

    script = _load_status_script()
    fake = FakeCapital()
    fake.market_status["GOLD"] = "CLOSED"
    client = CapitalClient("me", "pw", "key", http=fake.client())

    async def sleep(seconds):
        pass

    assert await script.watch(client, "GOLD", sleep=sleep, out=lambda line: None, max_polls=3) == "CLOSED"


# ---------------------------------------------------------------- a dry run must never touch the real record of the held VIX position
def _load_pause_module():
    import importlib.util
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    spec = importlib.util.spec_from_file_location("demo_vix_pause_under_test", INTEGRATION / "test_demo_vix_pause.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_hold_file_is_the_real_one_only_for_a_real_run(tmp_path):
    module = _load_pause_module()
    assert module.hold_path({}, dry_run=False) == module.REAL_HOLD_FILE
    dry_a, dry_b = module.hold_path({}, dry_run=True), module.hold_path({}, dry_run=True)
    assert dry_a != module.REAL_HOLD_FILE and dry_a != dry_b and module.REAL_HOLD_FILE.parent not in dry_a.parents      # private, and not under the repo
    explicit = str(tmp_path / "mine.json")
    assert module.hold_path({"CAPITAL_DEMO_VIX_HOLD_PATH": explicit}, dry_run=True) == tmp_path / "mine.json"           # an explicit path always wins


def test_a_dry_run_leaves_a_real_hold_record_alone(tmp_path):
    """Whatever sits at the default location while the harness runs against the fake is untouched - run in a copy of the layout, never the real file."""
    import os
    import shutil
    import subprocess
    import sys
    root = Path(__file__).resolve().parent.parent
    copy = tmp_path / "repo"
    shutil.copytree(root / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__", ".demo-*", "observed.json", ".journal-*"))
    shutil.copytree(root / "src", copy / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    shutil.copy(root / "pyproject.toml", copy / "pyproject.toml")
    record = copy / "tests" / "integration" / ".demo-vix-hold.json"
    record.write_text('{"deal_id": "THE-REAL-ONE", "opened_at_utc": "Sun 22:00:00"}')
    env = {k: v for k, v in os.environ.items() if k not in ("CAPITAL_DEMO_IDENTITY", "CAPITAL_DEMO_PASSWORD", "CAPITAL_DEMO_API_KEY", "CAPITAL_DEMO_VIX_HOLD_PATH")}
    env.update({"CAPITAL_HOOK_DEMO_DRY_RUN": "1", "CAPITAL_DEMO_EPIC": "GOLD", "PYTHONPATH": str(copy / "src"),
                "CAPITAL_DEMO_JOURNAL_PATH": str(tmp_path / "j.json"), "CAPITAL_DEMO_OBSERVED_PATH": str(tmp_path / "o.json"), "CAPITAL_DEMO_SHAPES_PATH": str(tmp_path / "s.json")})
    run = subprocess.run([sys.executable, "-m", "pytest", "-m", "demo", "tests/integration", "-q", "-p", "no:cacheprovider"], cwd=copy, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert record.read_text() == '{"deal_id": "THE-REAL-ONE", "opened_at_utc": "Sun 22:00:00"}'


# ---------------------------------------------------------------- the exits test's helpers (they failed against the real API once: a stale price made the level invalid)
def _load_exits_module():
    import importlib.util
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    spec = importlib.util.spec_from_file_location("demo_exits_under_test", INTEGRATION / "test_demo_exits.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_bracket_puts_the_stop_and_the_target_on_the_sides_each_direction_needs():
    exits = _load_exits_module()
    buy = exits.bracket("BUY", 100.00, 100.10, 0.001, 2)
    sell = exits.bracket("SELL", 100.00, 100.10, 0.001, 2)
    assert buy == {"stop_level": 99.9, "profit_level": 100.2} and sell == {"stop_level": 100.2, "profit_level": 99.9}
    assert buy["stop_level"] < 100.00 < 100.10 < buy["profit_level"] and sell["profit_level"] < 100.00 < 100.10 < sell["stop_level"]


async def test_the_exits_test_reads_the_price_when_it_orders_not_when_the_session_started():
    from capital_hook import CapitalClient
    from capital_hook.testing import FakeCapital
    exits = _load_exits_module()
    fake = FakeCapital()
    client = CapitalClient("me", "pw", "key", http=fake.client())
    assert await exits.fresh_quote(client, "GOLD") == (2000.0, 2001.0)
    fake.set_quote("GOLD", 2010.0, 2011.0)
    assert await exits.fresh_quote(client, "GOLD") == (2010.0, 2011.0)


async def _bracket_world():
    from capital_hook import CapitalClient
    from capital_hook.testing import FakeCapital
    exits = _load_exits_module()
    fake = FakeCapital()
    client = CapitalClient("me", "pw", "key", http=fake.client())

    class Tracker:
        positions = set()

        async def open(self, epic, size, direction, **options):
            reference = await client.open_position(epic, size, direction, **options)
            return reference, await client.confirm(reference, attempts=1, delay=0)
    return exits, fake, client, Tracker()


async def test_a_level_refused_as_too_close_is_retried_wider_at_the_price_of_that_moment():
    exits, fake, client, tracker = await _bracket_world()
    fake.fail_next("POST", "/api/v1/positions", status=400, error_code="error.invalid.takeprofit.minvalue: 2003.5")
    fake.fail_next("POST", "/api/v1/positions", status=400, error_code="error.invalid.stoploss.maxvalue: 1996.1")
    confirmation, levels, attempts, price, gap = await exits.open_with_bracket(client, tracker, "GOLD", 0.1, "BUY", 2, 0.0002)
    assert confirmation.accepted and len(attempts) == 2 and gap == 0.0002 * 1.5 ** 2
    assert [a["gap_pct"] for a in attempts] == [0.02, 0.03] and attempts[0]["refused"].startswith("error.invalid.takeprofit")
    assert levels["stop_level"] < 2000.0 < 2001.0 < levels["profit_level"] and (fake.meta[confirmation.position_deal_id]["stopLevel"], fake.meta[confirmation.position_deal_id]["profitLevel"]) == (levels["stop_level"], levels["profit_level"])


async def test_three_refusals_give_up_and_report_every_attempt():
    exits, fake, client, tracker = await _bracket_world()
    fake.fail_next("POST", "/api/v1/positions", status=400, error_code="error.invalid.takeprofit.minvalue: 2003.5", times=5)
    confirmation, levels, attempts, price, gap = await exits.open_with_bracket(client, tracker, "GOLD", 0.1, "SELL", 2, 0.0002)
    assert confirmation is None and len(attempts) == 3 and fake.positions == {}


async def test_an_error_that_is_not_about_the_levels_is_raised_not_retried():
    import pytest as _pytest

    from capital_hook import APIError
    exits, fake, client, tracker = await _bracket_world()
    fake.fail_next("POST", "/api/v1/positions", status=503, error_code="error.unavailable")
    with _pytest.raises(APIError) as exc:
        await exits.open_with_bracket(client, tracker, "GOLD", 0.1, "BUY", 2, 0.0002)
    assert exc.value.status_code == 503


# ---------------------------------------------------------------- demo_cleanup.py --record-hold
def _load_cleanup_module():
    import importlib.util
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    spec = importlib.util.spec_from_file_location("demo_cleanup_under_test", INTEGRATION / "demo_cleanup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_record_hold_rebuilds_the_record_for_exactly_one_open_position(tmp_path):
    import json
    cleanup = _load_cleanup_module()
    path = tmp_path / ".demo-vix-hold.json"
    message = cleanup.hold_record([{"deal_id": "D1"}], path)
    assert "recorded D1" in message and json.loads(path.read_text())["deal_id"] == "D1"


def test_record_hold_refuses_when_it_cannot_tell_which_position_is_the_held_one(tmp_path):
    cleanup = _load_cleanup_module()
    path = tmp_path / "hold.json"
    assert "found 0" in cleanup.hold_record([], path) and "found 2" in cleanup.hold_record([{"deal_id": "A"}, {"deal_id": "B"}], path) and not path.exists()


def test_record_hold_never_overwrites_a_record_that_names_another_deal(tmp_path):
    cleanup = _load_cleanup_module()
    path = tmp_path / "hold.json"
    path.write_text('{"deal_id": "OTHER"}')
    assert "different deal" in cleanup.hold_record([{"deal_id": "D1"}], path) and path.read_text() == '{"deal_id": "OTHER"}'
    assert "recorded OTHER" in cleanup.hold_record([{"deal_id": "OTHER"}], path)
