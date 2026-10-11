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
