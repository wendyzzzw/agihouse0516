"""Simulation engine: tick loop, state, event emission.

Phases per tick:
  1. Sellers reprice
  2. Deliver messages queued from the previous tick (avoid same-tick time travel)
  3. Each buyer decides (claude -p or rule) and acts
  4. Snapshot prices
"""
from __future__ import annotations
import random
import uuid
from typing import Dict, List, Any

from market import Seller
from topology import generate_graph, graph_to_matrix
from personas import PROFILE_SEQUENCE, DEFAULT_TOOLS, make_persona
from agent_runtime import decide_via_claude, _fallback


BUYER_IDS = [chr(ord("A") + i) for i in range(15)]   # A..O


class Engine:
    def __init__(
        self,
        topology: str,
        seed: int = 42,
        ticks: int = 55,
        llm_mode: str = "rule",         # "claude" | "rule"
        model: str = "haiku",
    ):
        self.topology = topology
        self.seed = seed
        self.ticks = ticks
        self.llm_mode = llm_mode
        self.model = model

        self.rng = random.Random(seed)

        # Sellers
        self.sellers: Dict[str, Seller] = {
            "Airline_A": Seller("Airline_A", "Airline_A", inventory=6, base_price=280),
            "Airline_B": Seller("Airline_B", "Airline_B", inventory=4, base_price=260),
        }
        self.seller_ids = list(self.sellers.keys())

        # Buyers
        self.buyer_ids = list(BUYER_IDS)
        self.graph = generate_graph(topology, self.buyer_ids, self.seller_ids, seed)
        self.matrix = graph_to_matrix(self.graph)

        self.buyers: Dict[str, dict] = {}
        for i, bid in enumerate(self.buyer_ids):
            profile = PROFILE_SEQUENCE[i]
            self.buyers[bid] = {
                "id": bid,
                "type": "buyer",
                "persona": make_persona(profile),
                "budget": 280 + self.rng.randint(0, 120),
                "bought": False,
                "purchase_price": None,
                "purchase_seller": None,
                "satisfaction": None,
                "ticks_waited": 0,
                "beliefs": {},
                "tools": list(DEFAULT_TOOLS),
                "inbox": [],
                "outbox": [],
            }

        # Messages queued this tick, delivered next tick
        self.pending_messages: List[Dict[str, Any]] = []

        # Output collectors
        self.events: List[Dict[str, Any]] = []
        self.prices_over_time: List[Dict[str, Any]] = []
        self.current_tick: int = 0

    # ---------- helpers ----------

    def neighbors_of(self, agent_id: str) -> List[str]:
        return sorted([n for n in self.graph.neighbors(agent_id) if n in self.buyer_ids])

    def world_view_for(self, agent_id: str) -> dict:
        return {
            "sellers": {
                sid: {"price": s.posted_price(), "inventory": s.inventory}
                for sid, s in self.sellers.items()
            },
            "neighbors": self.neighbors_of(agent_id),
            "ticks_remaining": self.ticks - self.current_tick + 1,
        }

    def _emit(self, **kw) -> None:
        kw.setdefault("turn", self.current_tick)
        self.events.append(kw)

    # ---------- per-tick phases ----------

    def deliver_messages(self) -> None:
        for msg in self.pending_messages:
            recipient = self.buyers.get(msg["recipient"])
            if recipient is None:
                continue
            recipient["inbox"].append({
                "sender": msg["sender"],
                "content": msg["content"],
                "turn": msg["turn"],
            })
            if len(recipient["inbox"]) > 10:
                recipient["inbox"] = recipient["inbox"][-10:]
        self.pending_messages.clear()

    def decide(self, agent: dict) -> dict:
        wv = self.world_view_for(agent["id"])
        if self.llm_mode == "claude":
            return decide_via_claude(agent, wv, model=self.model)
        return _fallback(agent, wv)

    def execute(self, agent: dict, action: dict) -> None:
        atype = (action.get("action") or "WAIT").upper()
        t = self.current_tick

        if agent["bought"] or atype == "WAIT":
            agent["ticks_waited"] += 1
            return

        if atype == "BUY":
            target = action.get("target")
            seller = self.sellers.get(target)
            if seller is None:
                self._emit(**{"from": agent["id"], "msg": f"tried to buy unknown seller '{target}'",
                              "cls": "log-lie"})
                return
            if seller.attempt_buy():
                price = seller.posted_price()
                agent["bought"] = True
                agent["purchase_price"] = price
                agent["purchase_seller"] = target
                # satisfaction = 50 base + bonus for paying under budget (clamped)
                under = (agent["budget"] - price) / max(agent["budget"], 1)
                sat = int(round(50 + 100 * max(0.0, under)))
                agent["satisfaction"] = max(0, min(100, sat))
                # Update buyer's belief — we *know* this price was real
                agent["beliefs"][target] = {"last_price": price, "turn": t, "source": "self"}
                self._emit(**{
                    "from": agent["id"], "to": None,
                    "msg": f"BOUGHT from {target} at ${price} "
                           f"({agent['persona']['profile']}, {agent['satisfaction']}% sat)",
                    "cls": "log-buy",
                    "letter": agent["id"], "price": price, "sat": agent["satisfaction"],
                })
            else:
                self._emit(**{"from": agent["id"], "msg": f"failed buy from {target} (sold out)",
                              "cls": "log-trade"})
            return

        if atype == "COMMUNICATE":
            to = action.get("target")
            content = (action.get("content") or "").strip()
            if not to or not content:
                agent["ticks_waited"] += 1
                return
            if not self.matrix.get(agent["id"], {}).get(to, False):
                # topology blocked it
                self._emit(**{"from": agent["id"], "to": to,
                              "msg": f"blocked: no edge ({content[:40]})",
                              "cls": "log-lie"})
                return
            msg = {
                "id": str(uuid.uuid4())[:8],
                "turn": t,
                "sender": agent["id"],
                "recipient": to,
                "content": content,
            }
            self.pending_messages.append(msg)
            agent["outbox"].append(msg)
            cls = "log-probe" if "?" in content else "log-trade"
            self._emit(**{
                "from": agent["id"], "to": to,
                "msg": content if len(content) <= 100 else content[:97] + "...",
                "cls": cls,
            })

    # ---------- main loop ----------

    def run(self) -> dict:
        for t in range(1, self.ticks + 1):
            self.current_tick = t

            # 1. Sellers reprice
            for s in self.sellers.values():
                s.update_price(t, self.ticks)

            # 2. Deliver messages from last tick
            self.deliver_messages()

            # 3. Buyers decide & act (shuffled order each tick)
            order = list(self.buyer_ids)
            self.rng.shuffle(order)
            for bid in order:
                agent = self.buyers[bid]
                if agent["bought"]:
                    continue
                action = self.decide(agent)
                self.execute(agent, action)

            # 4. Snapshot
            self.prices_over_time.append({
                "turn": t,
                "a": self.sellers["Airline_A"].posted_price(),
                "b": self.sellers["Airline_B"].posted_price(),
                "inv_a": self.sellers["Airline_A"].inventory,
                "inv_b": self.sellers["Airline_B"].inventory,
            })

        return self.snapshot()

    # ---------- output ----------

    def snapshot(self) -> dict:
        bought = [b for b in self.buyers.values() if b["bought"]]
        n_bought = len(bought)
        avg_price = round(sum(b["purchase_price"] for b in bought) / n_bought, 1) if bought else 0
        avg_sat = round(sum(b["satisfaction"] for b in bought) / n_bought, 1) if bought else 0
        by_profile: Dict[str, Any] = {}
        for prof in ["budget", "family", "investor", "flexible"]:
            ps = [b for b in bought if b["persona"]["profile"] == prof]
            by_profile[prof] = (round(sum(b["purchase_price"] for b in ps) / len(ps), 1)
                                if ps else None)
        total_msgs = sum(len(b["outbox"]) for b in self.buyers.values())

        return {
            "topology": self.topology,
            "seed": self.seed,
            "ticks": self.ticks,
            "llm_mode": self.llm_mode,
            "summary": {
                "avg_price": avg_price,
                "by_profile": by_profile,
                "n_bought": n_bought,
                "n_missed": len(self.buyer_ids) - n_bought,
                "avg_satisfaction": avg_sat,
                "total_messages": total_msgs,
            },
            "prices_over_time": self.prices_over_time,
            "events": self.events,
            "agents": {
                bid: {
                    "id": b["id"],
                    "profile": b["persona"]["profile"],
                    "budget": b["budget"],
                    "bought": b["bought"],
                    "purchase_price": b["purchase_price"],
                    "purchase_seller": b["purchase_seller"],
                    "satisfaction": b["satisfaction"],
                    "messages_sent": len(b["outbox"]),
                    "messages_received": len(b["inbox"]),
                }
                for bid, b in self.buyers.items()
            },
            "sellers": {
                sid: {
                    "name": s.name,
                    "base_price": s.base_price,
                    "final_price": s.posted_price(),
                    "initial_inventory": s.initial_inventory,
                    "final_inventory": s.inventory,
                }
                for sid, s in self.sellers.items()
            },
            "comm_matrix": self.matrix,
        }
