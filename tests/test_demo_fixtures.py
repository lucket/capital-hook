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
