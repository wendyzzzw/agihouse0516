"""Seller-side dynamic pricing — simplified yield management.

Three signals each tick:
  1. fill_progress vs. expected_progress  → discount when behind, raise when ahead
  2. time_left small & inventory left      → fire-sale dump
  3. recent_demand pulse                   → demand-push raise
"""


class Seller:
    def __init__(self, id: str, name: str, inventory: int, base_price: int, start_mult: float = 1.30):
        self.id = id
        self.name = name
        self.inventory = inventory
        self.initial_inventory = inventory
        self.base_price = base_price
        # Real-world airlines: list price starts above "fair" base and drifts based on demand/time.
        self.current_price = float(base_price) * start_mult
        self.recent_demand = 0   # buys this tick, reset after pricing

    def update_price(self, t: int, T: int) -> None:
        if self.inventory <= 0:
            return

        fill = 1.0 - self.inventory / self.initial_inventory
        time_progress = t / T
        time_left = (T - t) / T
        expected_fill = time_progress

        # Signal 1: behind/ahead of expected pace
        if fill < expected_fill - 0.15:
            self.current_price *= 0.96
        elif fill > expected_fill + 0.15:
            self.current_price *= 1.04

        # Signal 2: end-of-window fire sale
        if time_left < 0.15 and self.inventory > 0:
            self.current_price *= 0.92

        # Signal 3: a demand pulse this past tick
        if self.recent_demand >= max(1, int(self.initial_inventory * 0.2)):
            self.current_price *= 1.03

        # Clamp to [0.65×, 1.40×] of base
        lo = self.base_price * 0.65
        hi = self.base_price * 1.40
        if self.current_price < lo:
            self.current_price = lo
        elif self.current_price > hi:
            self.current_price = hi

        # Reset demand window
        self.recent_demand = 0

    def posted_price(self) -> int:
        return int(round(self.current_price))

    def attempt_buy(self) -> bool:
        if self.inventory > 0:
            self.inventory -= 1
            self.recent_demand += 1
            return True
        return False
