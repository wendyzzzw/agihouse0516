"""Simulation engine. Consumes the team-canonical YAML schema (test_config.yaml).

Phases per tick (round):
  1. Sellers reprice (yield-management three-signal model)
  2. Deliver messages queued from the previous tick
  3. Each buyer decides (claude -p or rule) and acts
  4. Snapshot prices

Inputs:
  - config_path:    YAML file with `simulations: [...]`
  - simulation_id:  which simulation in the file (None → first)
  - override_topology: optional, replaces simulation.topology.generator.buyer_buyer_edges.type
  - llm_mode:       "claude" or "rule"

Output: see snapshot() — matches what runs/*.json has always exported, plus a
`config` field describing which YAML file and simulation produced this run.
"""
from __future__ import annotations
import random
import uuid
from typing import Dict, List, Any, Optional

from market import Seller
from topology import build_matrix
from personas import make_persona, DEFAULT_TOOLS
from agent_runtime import decide_via_claude, _fallback
from config import load, simulation as _sim, DEFAULT_CONFIG


class Engine:
    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG,
        simulation_id: Optional[str] = None,
        override_topology: Optional[str] = None,
        seed: int = 42,
        llm_mode: str = "rule",
        model: Optional[str] = None,
    ):
        self.config_path = config_path
        self.cfg = load(config_path)
        self.sim = _sim(self.cfg, simulation_id)
        self.simulation_id = self.sim["id"]
        self.override_topology = override_topology
        self.llm_mode = llm_mode
        self.seed = seed
        self.rng = random.Random(seed)

        settings = self.sim.get("settings") or {}
        self.ticks = int(settings.get("max_rounds", 55))

        llm_cfg = self.sim.get("llm") or {}
        self.model = model or llm_cfg.get("model", "haiku")

        # Build sellers from the simulation's `sellers` list.
        pricing_cfg = self.sim.get("pricing") or _default_pricing()
        self.sellers: Dict[str, Seller] = {}
        for s in self.sim["sellers"]:
            base = int(s.get("min_price") or s["starting_price"])
            start = int(s["starting_price"])
            start_mult = start / base if base else 1.30
            ceil_pct = max(start_mult, 1.40)
            # `starting_price` is the actual posted price at t=0; `min_price` is the floor.
            self.sellers[s["id"]] = Seller(
                id=s["id"],
                name=s.get("name", s["id"]),
                inventory=int(s["inventory"]),
                base_price=base,
                start_mult=start_mult,
                floor_pct=1.0,          # base IS the floor — see min_price above
                ceil_pct=ceil_pct,
                pricing=pricing_cfg,
            )
            # Persist archetype + goal as engine-level metadata for snapshot / prompt.
            self.sellers[s["id"]].archetype = s.get("archetype", "")
            self.sellers[s["id"]].goal = dict(s.get("goal") or {})
        self.seller_ids = list(self.sellers.keys())

        # Build buyers from the `buyers` list.
        self.buyer_ids = [b["id"] for b in self.sim["buyers"]]
        self.buyers: Dict[str, dict] = {}
        # demo.html uses A..O letters to address agents; preserve that mapping
        # by assigning a letter per buyer in declaration order.
        self.id_to_letter: Dict[str, str] = {
            b["id"]: (chr(ord("A") + i) if i < 26 else None)
            for i, b in enumerate(self.sim["buyers"])
        }
        for i, b in enumerate(self.sim["buyers"]):
            bid = b["id"]
            letter = self.id_to_letter[bid]
            self.buyers[bid] = {
                "id": bid,
                "letter": letter,
                "type": "buyer",
                "archetype": b.get("archetype", ""),
                "persona": make_persona("buyer", b.get("archetype", ""), config_path=config_path),
                "budget": int(b["budget"]),
                "goal": dict(b.get("goal") or {}),
                "bought": False,
                "items_owned": 0,
                "target_items": int((b.get("goal") or {}).get("quantity")
                                    or (b.get("goal") or {}).get("max_quantity")
                                    or 1),
                "purchase_price": None,
                "purchase_seller": None,
                "satisfaction": None,
                "ticks_waited": 0,
                "beliefs": {},
                "tools": list(DEFAULT_TOOLS),
                "inbox": [],
                "outbox": [],
            }

        # Topology
        self.graph, self.matrix = build_matrix(
            self.sim, override_buyer_buyer=override_topology, seed=seed,
        )

        # Output collectors
        self.pending_messages: List[Dict[str, Any]] = []
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
                "id": msg.get("id"),
                "sender": msg["sender"],
                "content": msg["content"],
                "turn": msg["turn"],
            })
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
                self._emit(**{"from": agent["id"], "letter": agent["letter"],
                              "msg": f"tried to buy unknown seller '{target}'",
                              "cls": "log-lie"})
                return
            if seller.attempt_buy():
                price = seller.posted_price()
                agent["bought"] = True
                agent["items_owned"] += 1
                agent["purchase_price"] = price
                agent["purchase_seller"] = target
                agent["satisfaction"] = _satisfaction(agent, price)
                agent["beliefs"][target] = {"last_price": price, "turn": t, "source": "self"}
                self._emit(**{
                    "from": agent["id"], "letter": agent["letter"],
                    "to": None,
                    "msg": f"BOUGHT from {target} at ${price} "
                           f"({agent['archetype']}, {agent['satisfaction']}% sat)",
                    "cls": "log-buy",
                    "price": price, "sat": agent["satisfaction"],
                })
            else:
                self._emit(**{"from": agent["id"], "letter": agent["letter"],
                              "msg": f"failed buy from {target} (sold out)",
                              "cls": "log-trade"})
            return

        if atype == "COMMUNICATE":
            to = action.get("target")
            content = (action.get("content") or "").strip()
            if not to or not content:
                agent["ticks_waited"] += 1
                return
            if not self.matrix.get(agent["id"], {}).get(to, False):
                self._emit(**{"from": agent["id"], "letter": agent["letter"], "to": to,
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
                "from": agent["id"], "letter": agent["letter"],
                "to": to, "to_letter": self.id_to_letter.get(to),
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

            # 2. Deliver
            self.deliver_messages()

            # 3. Buyers decide & act (shuffled)
            order = list(self.buyer_ids)
            self.rng.shuffle(order)
            for bid in order:
                agent = self.buyers[bid]
                if agent["bought"]:
                    continue
                action = self.decide(agent)
                self.execute(agent, action)

            # 4. Snapshot
            sellers_list = list(self.sellers.values())
            snap = {"turn": t}
            for i, s in enumerate(sellers_list):
                key = "a" if i == 0 else "b" if i == 1 else f"s{i}"
                snap[key] = s.posted_price()
                snap[f"inv_{key}"] = s.inventory
            self.prices_over_time.append(snap)

        return self.snapshot()

    # ---------- output ----------

    def snapshot(self) -> dict:
        bought = [b for b in self.buyers.values() if b["bought"]]
        n_bought = len(bought)
        avg_price = round(sum(b["purchase_price"] for b in bought) / n_bought, 1) if bought else 0
        avg_sat = round(sum(b["satisfaction"] for b in bought) / n_bought, 1) if bought else 0
        by_archetype: Dict[str, Any] = {}
        for b in bought:
            a = b["archetype"]
            by_archetype.setdefault(a, []).append(b["purchase_price"])
        by_archetype = {a: round(sum(v) / len(v), 1) for a, v in by_archetype.items()}
        total_msgs = sum(len(b["outbox"]) for b in self.buyers.values())

        return {
            "config": {
                "path": os.path.relpath(self.config_path, os.path.dirname(os.path.dirname(__file__))),
                "simulation_id": self.simulation_id,
                "override_topology": self.override_topology,
            },
            "topology": self.override_topology or _topology_label(self.sim),
            "seed": self.seed,
            "ticks": self.ticks,
            "llm_mode": self.llm_mode,
            "summary": {
                "avg_price": avg_price,
                "by_archetype": by_archetype,
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
                    "letter": b["letter"],
                    "archetype": b["archetype"],
                    "persona": b["persona"],
                    "budget": b["budget"],
                    "goal": b["goal"],
                    "items_owned": b["items_owned"],
                    "target_items": b["target_items"],
                    "bought": b["bought"],
                    "purchase_price": b["purchase_price"],
                    "purchase_seller": b["purchase_seller"],
                    "satisfaction": b["satisfaction"],
                    "ticks_waited": b["ticks_waited"],
                    "messages_sent": len(b["outbox"]),
                    "messages_received": len(b["inbox"]),
                    "inbox": b["inbox"],
                    "outbox": b["outbox"],
                    "beliefs": b["beliefs"],
                }
                for bid, b in self.buyers.items()
            },
            "sellers": {
                sid: {
                    "id": s.id,
                    "name": s.name,
                    "archetype": getattr(s, "archetype", ""),
                    "goal": getattr(s, "goal", {}),
                    "starting_price": int(round(s.base_price * (s.current_price / max(s.base_price, 1))))
                                      if False else int(round(s.base_price * (s.ceil_pct))),
                    "final_price": s.posted_price(),
                    "min_price": s.base_price,
                    "initial_inventory": s.initial_inventory,
                    "final_inventory": s.inventory,
                }
                for sid, s in self.sellers.items()
            },
            "comm_matrix": self.matrix,
            "conversations": self._conversation_threads(),
        }

    def _conversation_threads(self) -> Dict[str, List[Dict[str, Any]]]:
        threads: Dict[str, List[Dict[str, Any]]] = {}
        for b in self.buyers.values():
            for m in b["outbox"]:
                a, c = m["sender"], m["recipient"]
                key = f"{min(a,c)}<->{max(a,c)}"
                threads.setdefault(key, []).append({
                    "turn": m["turn"], "from": a, "to": c, "content": m["content"],
                })
        for k in threads:
            threads[k].sort(key=lambda x: x["turn"])
        return threads


def _satisfaction(agent: dict, price: int) -> int:
    """Map outcome → 0-100 satisfaction depending on the goal type."""
    goal = agent.get("goal") or {}
    gtype = goal.get("type")
    budget = agent["budget"]
    if gtype == "buy_if_below_price":
        cap = goal.get("max_price_per_item") or budget
        # Below cap = satisfied. The further below, the better.
        score = 60 + 40 * max(0.0, (cap - price) / max(cap, 1))
    elif gtype == "must_buy_quantity":
        cap = goal.get("max_price_per_item") or budget
        score = 75 + 25 * max(0.0, (cap - price) / max(cap, 1))   # already happy you got it
    else:
        score = 50 + 50 * max(0.0, (budget - price) / max(budget, 1))
    return max(0, min(100, int(round(score))))


def _topology_label(sim: Dict[str, Any]) -> str:
    """Best-effort name for the buyer-buyer topology in a simulation."""
    topo = sim.get("topology") or {}
    if topo.get("input_type") == "edge_list":
        return "edge_list"
    bb = (topo.get("generator") or {}).get("buyer_buyer_edges")
    if isinstance(bb, str):
        return bb
    if isinstance(bb, dict):
        return bb.get("type") or "unknown"
    return "unknown"


def _default_pricing() -> Dict[str, float]:
    """Fallback pricing coefficients if a simulation doesn't define its own."""
    return {
        "behind_pace_threshold": 0.15,
        "ahead_pace_threshold": 0.15,
        "behind_discount": 0.96,
        "ahead_raise": 1.04,
        "endgame_time_left": 0.15,
        "endgame_discount": 0.92,
        "demand_pulse_ratio": 0.20,
        "demand_pulse_raise": 1.03,
    }


import os   # noqa: E402   (needed by snapshot for relpath)
