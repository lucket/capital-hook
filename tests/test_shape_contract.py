"""The fake must not invent fields: every key it returns must exist in the shape recorded from the real DEMO API.

``tests/fixtures/shapes.json`` holds only structure (keys and value types) captured by the opt-in DEMO suite. Refresh it by
running ``pytest -m demo`` and copying the ``shapes`` section of ``tests/integration/observed.json``.
"""
import json
from datetime import datetime
from pathlib import Path

import pytest

import demo_harness as h

REAL = json.loads((Path(__file__).parent / "fixtures" / "shapes.json").read_text())
# The recorded list entry lacks profitLevel only because no listed position had one; the single-position shape shows it exists.
_listed = REAL["GET /positions 200"]["positions"][0]["position"]
_listed.update(REAL["GET /positions/{id} 200"]["position"])
NUMBERS = {"int", "float"}


def problems(fake_shape, real_shape, where=""):
    """What the fake has that the real API does not (extra keys, incompatible types)."""
    if isinstance(fake_shape, dict):
        if not isinstance(real_shape, dict):
            return [f"{where}: fake has an object, real has {real_shape}"]
        out = []
        for key, value in fake_shape.items():
            if key not in real_shape:
                out.append(f"{where}.{key}: not in the real API")
            else:
                out += problems(value, real_shape[key], f"{where}.{key}")
        return out
    if isinstance(fake_shape, list):
        if not isinstance(real_shape, list):
            return [f"{where}: fake has a list, real has {real_shape}"]
        return problems(fake_shape[0], real_shape[0], f"{where}[]") if fake_shape and real_shape else []
    real_kinds = set(str(real_shape).split("|")) if isinstance(real_shape, str) else {"?"}
    if fake_shape in real_kinds or (fake_shape in NUMBERS and real_kinds & NUMBERS) or "null" in real_kinds:
        return []
    return [f"{where}: fake {fake_shape}, real {sorted(real_kinds)}"]


@pytest.fixture
def seen(fake, client):
    """Every (method path status) -> shape the fake produced while a test drove it."""
    shapes = {}
    original = fake.handler

    def spy(request):
        response = original(request)
        try:
            body = json.loads(response.content) if response.content else None
        except ValueError:
            body = None
        key = f"{request.method} {h.normalize_path(request.url.path)} {response.status_code}"
        shape = h.shape_of(body)
        shapes[key] = h.merge_shapes(shapes[key], shape) if key in shapes else shape
        return response

    client._http._transport.handler = spy
    return shapes


async def drive(fake, client):
    fake.set_quote("GOLD", 2000.0, 2001.0)
    fake.preferences["hedgingMode"] = True
    await client.login()
    await client.ping()
    await client.get_account()
    await client.get_preferences()
    await client.get_markets(search_term="GOL")
    await client.get_markets(epics=["GOLD"])
    await client.get_market("GOLD")
    reference = await client.open_position("GOLD", 1, "BUY", stop_level=1900, profit_level=2100)
    confirmation = await client.confirm(reference)
    deal_id = confirmation.position_deal_id
    await client.get_positions()
    await client.get_position(deal_id)
    await client.update_position(deal_id, stop_level=1950)
    order = await client.create_working_order("GOLD", "BUY", 1, 1500)
    order_id = (await client.confirm(order)).deal_id
    await client.get_working_orders()
    await client.update_working_order(order_id, level=1400)
    await client.get_activity(last_period=600, detailed=True)
    await client.cancel_working_order(order_id)
    closing = (await client.close_position(deal_id))["dealReference"]
    await client.confirm(closing)
    for failing in (client.close_position, client.cancel_working_order):
        with pytest.raises(Exception):
            await failing("missing")
    with pytest.raises(Exception):
        await client.get_market("NOPE")
    await client.logout()


async def test_the_fake_returns_only_fields_the_real_api_has(fake, client, seen):
    await drive(fake, client)
    checked, found = 0, []
    for key, shape in sorted(seen.items()):
        if key in REAL:
            checked += 1
            found += [f"{key} {p}" for p in problems(shape, REAL[key])]
    assert checked >= 15, f"only {checked} of the fake's responses could be compared: {sorted(seen)}"
    assert not found, "\n".join(found)


def test_the_helper_catches_an_invented_field_and_a_wrong_type():
    assert problems({"a": "str", "b": "int"}, {"a": "str"}) == [".b: not in the real API"]
    assert problems({"a": "str"}, {"a": "bool"}) == [".a: fake str, real ['bool']"]
    assert problems({"a": "int"}, {"a": "float"}) == [] and problems({"a": ["int"]}, {"a": ["float|int"]}) == []
