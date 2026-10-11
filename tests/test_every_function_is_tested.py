"""Guard: every public function / property of the library has a test named after it (``test_<name>`` or ``test_<name>_...``).

A new public name without a test fails here, so "every function is executed" stays true as the library grows.
Behaviour is checked by the other files; this one only keeps the inventory honest.
"""
import ast
import inspect
import re
from pathlib import Path

import pytest

import capital_hook
from capital_hook import CapitalClient, Confirmation, PriceStream, hours
from capital_hook.testing import FakeCapital

TESTS = Path(__file__).parent


def _collected_test_names():
    names = set()
    for path in TESTS.glob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                names.add(node.name)
    return names


def public(cls):
    return sorted(name for name, _ in inspect.getmembers(cls) if not name.startswith("_") and (callable(getattr(cls, name)) or isinstance(inspect.getattr_static(cls, name), property)))


def inventory():
    items = [("CapitalClient", n) for n in public(CapitalClient)]
    items += [("PriceStream", n) for n in public(PriceStream)]
    items += [("Confirmation", n) for n in ("accepted", "rejected", "position_deal_id", "from_response")]
    items += [("hours", n) for n, f in inspect.getmembers(hours, inspect.isfunction) if not n.startswith("_") and f.__module__ == hours.__name__]
    items += [("FakeCapital", n) for n in public(FakeCapital)]
    return items


@pytest.mark.parametrize("owner, name", inventory(), ids=lambda v: v)
def test_every_public_name_has_a_test_named_after_it(owner, name):
    pattern = re.compile(rf"^test_{re.escape(name)}(_|$)")
    assert any(pattern.match(t) for t in _collected_test_names()), f"{owner}.{name} has no test named test_{name}* (add one, or the API changed)"


def test_the_inventory_is_not_empty_and_covers_the_exported_names():
    owners = {o for o, _ in inventory()}
    assert owners == {"CapitalClient", "PriceStream", "Confirmation", "hours", "FakeCapital"}
    assert len(inventory()) > 50
    for exported in ("CapitalClient", "PriceStream", "Confirmation", "is_market_closed"):
        assert exported in capital_hook.__all__
