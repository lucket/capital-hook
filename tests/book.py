"""Shared by the lifecycle scenarios and the randomized stateful test: the expected-state model and the three-way comparison."""
import pytest

START = 10_000.0


class Book:
    """The expected state, updated only by what the test itself did and the quotes it set."""

    def __init__(self, fake):
        self.fake = fake
        self.positions: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.realised = 0.0
        self.closed_ids: list[str] = []

    def opened(self, deal_id, epic, size, direction):
        bid, ask = self.fake.quote[epic]
        self.positions[deal_id] = {"epic": epic, "size": size, "direction": direction, "level": ask if direction == "BUY" else bid}

    def pnl(self, p):
        bid, ask = self.fake.quote[p["epic"]]
        return (bid - p["level"]) * p["size"] if p["direction"] == "BUY" else (p["level"] - ask) * p["size"]

    def closed(self, deal_id):
        self.realised += self.pnl(self.positions.pop(deal_id))
        self.closed_ids.append(deal_id)

    @property
    def unrealised(self):
        return sum(self.pnl(p) for p in self.positions.values())


async def coherent(client, fake, book):
    listed = {p["deal_id"]: p for p in await client.get_positions()}
    assert set(listed) == set(book.positions) == set(fake.positions), "open positions differ between client, fake and expectation"
    for deal_id, want in book.positions.items():
        got = listed[deal_id]
        assert (got["epic"], got["direction"]) == (want["epic"], want["direction"])
        assert got["size"] == pytest.approx(want["size"]) and fake.positions[deal_id]["level"] == pytest.approx(want["level"])
        assert got["pnl"] == pytest.approx(book.pnl(want))
    assert {o["deal_id"] for o in await client.get_working_orders()} == set(book.orders) == set(fake.orders), "working orders differ"
    account = (await client.get_account())["balance"]
    assert account["balance"] == pytest.approx(START + book.realised) == pytest.approx(fake.balance)
    assert account["profitLoss"] == pytest.approx(book.unrealised)
    assert not set(book.closed_ids) & set(fake.positions)
    fake.check_invariants()
