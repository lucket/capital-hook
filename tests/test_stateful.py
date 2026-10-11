"""Randomized sequences of trading operations (Hypothesis). After EVERY step the client's view, the fake's internal state and an
independent expectation (``Model``) must agree, and the fake's invariants must hold.

The model re-states the market rules on purpose (stops trigger on the closing side of the price, orders fill when the price reaches
them) so a mistake in the fake or in the client shows up as a disagreement. Hedging is on so each deal stays its own position;
netting is covered by its own tests.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from book import START, Book, coherent
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, precondition, rule

from capital_hook import APIError, CapitalClient
from capital_hook.testing import FakeCapital

EPICS = {"GOLD": (2000.0, 2001.0), "US100": (15000.0, 15001.0)}


class Model(Book):
    def __init__(self, fake):
        super().__init__(fake)
        self.stops: dict[str, tuple] = {}
        self.clock = datetime(2030, 1, 1, 12, 0, 0)
        fake.now = lambda: self.clock

    def expire(self):
        for order_id, o in list(self.orders.items()):
            if o.get("expires") and o["expires"] <= self.clock:
                del self.orders[order_id]

    def opened_with(self, deal_id, epic, size, direction, stop, profit):
        self.opened(deal_id, epic, size, direction)
        self.stops[deal_id] = (stop, profit)

    def settle(self, before):
        """What the market should have done after the quotes moved: fill reached orders, then trigger stops.

        ``before``: the ids of the positions that existed before the action, so a position created by a fill can be recognised."""
        fake = self.fake
        for order_id, o in list(self.orders.items()):
            bid, ask = fake.quote[o["epic"]]
            reached = {("BUY", "LIMIT"): ask <= o["level"], ("BUY", "STOP"): ask >= o["level"],
                       ("SELL", "LIMIT"): bid >= o["level"], ("SELL", "STOP"): bid <= o["level"]}[(o["direction"], o["kind"])]
            if reached:
                del self.orders[order_id]
                new = sorted(set(fake.positions) - before - set(self.positions))
                match = next(d for d in new if fake.positions[d]["epic"] == o["epic"] and fake.positions[d]["direction"] == o["direction"]
                             and fake.positions[d]["size"] == o["size"])
                self.opened(match, o["epic"], o["size"], o["direction"])
                self.stops[match] = (None, None)
        for deal_id, p in list(self.positions.items()):
            bid, ask = fake.quote[p["epic"]]
            stop, profit = self.stops.get(deal_id, (None, None))
            price = bid if p["direction"] == "BUY" else ask
            hit_stop = stop is not None and (price <= stop if p["direction"] == "BUY" else price >= stop)
            hit_profit = profit is not None and (price >= profit if p["direction"] == "BUY" else price <= profit)
            if hit_stop or hit_profit:
                self.closed(deal_id)


class TradingMachine(RuleBasedStateMachine):
    positions = Bundle("positions")
    orders = Bundle("orders")

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.fake = FakeCapital()
        self.fake.preferences["hedgingMode"] = True
        for epic, (bid, ask) in EPICS.items():
            self.fake.markets.setdefault(epic, "COMMODITIES")
            self.fake.set_quote(epic, bid, ask)
        self.client = CapitalClient("me", "pw", "key", http=self.fake.client())
        self.model = Model(self.fake)
        self.run(self.client.login())

    def run(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    def teardown(self):
        self.run(self.client.aclose())
        self.run(self.fake.client().aclose())
        self.loop.close()

    # ------------------------------------------------------------ operations
    @rule(target=positions, epic=st.sampled_from(sorted(EPICS)), direction=st.sampled_from(["BUY", "SELL"]),
          size=st.sampled_from([0.1, 0.5, 1.0, 2.0, 40.0]), with_stops=st.booleans())
    def open_position(self, epic, direction, size, with_stops):
        bid, ask = self.fake.quote[epic]
        entry = ask if direction == "BUY" else bid
        stop = profit = None
        options = {}
        if with_stops:
            stop, profit = (round(entry * 0.96, 2), round(entry * 1.04, 2)) if direction == "BUY" else (round(entry * 1.04, 2), round(entry * 0.96, 2))
            options = {"stop_level": stop, "profit_level": profit}
        confirmation = self.run(self.confirm_open(epic, size, direction, options))
        if confirmation.accepted:
            self.model.opened_with(confirmation.position_deal_id, epic, size, direction, stop, profit)
            return confirmation.position_deal_id
        assert confirmation.reason == "INSUFFICIENT_FUNDS" and confirmation.position_deal_id is None      # the only legitimate refusal here
        return None

    async def confirm_open(self, epic, size, direction, options):
        reference = await self.client.open_position(epic, size, direction, **options)
        return await self.client.confirm(reference, attempts=1, delay=0)

    @rule(deal_id=consumes(positions))
    def close_position(self, deal_id):
        if deal_id is None:
            return
        if deal_id in self.model.positions:
            self.run(self.client.close_position(deal_id))
            self.model.closed(deal_id)
        else:                                                              # a stop / profit level already closed it
            with pytest.raises(APIError) as exc:
                self.run(self.client.close_position(deal_id))
            assert exc.value.status_code == 404

    @rule(epic=st.sampled_from(sorted(EPICS)), move=st.floats(min_value=-0.08, max_value=0.08, allow_nan=False))
    def move_price(self, epic, move):
        base_bid, base_ask = EPICS[epic]
        bid = round(base_bid * (1 + move), 2)
        before = set(self.fake.positions)
        self.fake.set_quote(epic, bid, round(bid + (base_ask - base_bid), 2))
        self.model.settle(before)

    @rule(target=orders, epic=st.sampled_from(sorted(EPICS)), direction=st.sampled_from(["BUY", "SELL"]), kind=st.sampled_from(["LIMIT", "STOP"]),
          offset=st.floats(min_value=-0.06, max_value=0.06, allow_nan=False), size=st.sampled_from([0.1, 1.0]),
          ttl_minutes=st.sampled_from([None, 30, 120]))
    def create_order(self, epic, direction, kind, offset, size, ttl_minutes):
        bid, ask = self.fake.quote[epic]
        level = round((ask if direction == "BUY" else bid) * (1 + offset), 2)
        before = set(self.fake.positions)
        try:
            expires = self.model.clock + timedelta(minutes=ttl_minutes) if ttl_minutes else None
            reference = self.run(self.client.create_working_order(epic, direction, size, level, kind, good_till_date=expires))
        except APIError as exc:                                            # a level on the wrong side of the market is refused
            assert exc.status_code == 400
            return None
        confirmation = self.run(self.client.confirm(reference, attempts=1, delay=0))
        assert confirmation.accepted
        self.model.orders[confirmation.deal_id] = {"epic": epic, "direction": direction, "kind": kind, "level": level, "size": size, "expires": expires}
        self.model.settle(before)                                          # a marketable order may fill at once
        return confirmation.deal_id

    @rule(order_id=consumes(orders))
    def cancel_order(self, order_id):
        if order_id is None:
            return
        if order_id in self.model.orders:
            self.run(self.client.cancel_working_order(order_id))
            del self.model.orders[order_id]
        else:                                                              # it filled in the meantime
            with pytest.raises(APIError) as exc:
                self.run(self.client.cancel_working_order(order_id))
            assert exc.value.status_code == 404

    @rule(minutes=st.sampled_from([10, 45, 90, 180]))
    def time_passes(self, minutes):
        self.model.clock += timedelta(minutes=minutes)
        self.model.expire()

    @rule()
    def session_expires(self):
        self.fake.expire_session()

    # ------------------------------------------------------------ the property
    @invariant()
    def everything_agrees(self):
        self.run(coherent(self.client, self.fake, self.model))


TradingMachine.TestCase.settings = settings(max_examples=40, stateful_step_count=25, deadline=None, database=None,
                                            suppress_health_check=list(HealthCheck))
TestTradingStateMachine = TradingMachine.TestCase


def test_the_starting_balance_the_model_assumes():
    assert FakeCapital().balance == START
