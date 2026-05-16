"""Seller-side dynamic pricing — yield management (3 signals).

Pricing coefficients are passed in from the engine (typically from each
simulation's `pricing:` block in its YAML). A `_DEFAULT_PRICING` constant
covers the case where the simulation doesn't declare one.
"""
from __future__ import annotations
from typing import Optional, Dict, Any


_DEFAULT_PRICING: Dict[str, float] = {
    "behind_pace_threshold": 0.15,
    "ahead_pace_threshold": 0.15,
    "behind_discount": 0.96,
    "ahead_raise": 1.04,
    "endgame_time_left": 0.15,
    "endgame_discount": 0.92,
    "demand_pulse_ratio": 0.20,
    "demand_pulse_raise": 1.03,
}


class Seller:
    def __init__(
        self,
        id: str,
        name: str,
        inventory: int,
        base_price: int,
        start_mult: float = 1.30,
        floor_pct: float = 0.65,
        ceil_pct: float = 1.40,
        pricing: Optional[Dict[str, float]] = None,
    ):
        self.id = id
        self.name = name
        self.inventory = inventory
        self.initial_inventory = inventory
        self.base_price = base_price
        self.current_price = float(base_price) * start_mult
        self.floor_pct = floor_pct
        self.ceil_pct = ceil_pct
        self.recent_demand = 0
        # Pricing coefficients — defaults if the simulation didn't define them.
        self.p = pricing if pricing is not None else dict(_DEFAULT_PRICING)

    def update_price(self, t: int, T: int) -> None:
        if self.inventory <= 0:
            return

        fill = 1.0 - self.inventory / self.initial_inventory
        time_progress = t / T
        time_left = (T - t) / T
        expected_fill = time_progress

        # Signal 1: behind/ahead of expected pace
        if fill < expected_fill - self.p["behind_pace_threshold"]:
            self.current_price *= self.p["behind_discount"]
        elif fill > expected_fill + self.p["ahead_pace_threshold"]:
            self.current_price *= self.p["ahead_raise"]

        # Signal 2: end-of-window fire sale
        if time_left < self.p["endgame_time_left"] and self.inventory > 0:
            self.current_price *= self.p["endgame_discount"]

        # Signal 3: demand pulse this past tick
        pulse_threshold = max(1, int(self.initial_inventory * self.p["demand_pulse_ratio"]))
        if self.recent_demand >= pulse_threshold:
            self.current_price *= self.p["demand_pulse_raise"]

        # Clamp to [floor_pct*base, ceil_pct*base]
        lo = self.base_price * self.floor_pct
        hi = self.base_price * self.ceil_pct
        if self.current_price < lo:
            self.current_price = lo
        elif self.current_price > hi:
            self.current_price = hi

        self.recent_demand = 0

    def posted_price(self) -> int:
        return int(round(self.current_price))

    def attempt_buy(self) -> bool:
        if self.inventory > 0:
            self.inventory -= 1
            self.recent_demand += 1
            return True
        return False
