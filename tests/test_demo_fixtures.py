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
