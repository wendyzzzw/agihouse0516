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

PROFILE_ACTIONS = {
    "budget": ["BUY", "COMMUNICATE", "PROBE", "SHARE_INFO", "WAIT", "EXIT"],
    "family": ["BUY", "COMMUNICATE", "PROBE", "WAIT"],
    "investor": ["BUY", "BID", "COUNTER_OFFER", "PROBE", "SHARE_INFO", "BUILD_TOOL", "LIE", "WAIT", "EXIT"],
    "flexible": ["BUY", "COMMUNICATE", "PROBE", "WAIT", "EXIT"],
}

MESSAGE_ACTIONS = {
    "COMMUNICATE",
    "PROBE",
    "SHARE_INFO",
    "COORDINATE",
    "LIE",
    "BID",
    "COUNTER_OFFER",
    "REJECT_OFFER",
    "BROADCAST",
    "FORM_CONNECTION",
}

BUY_ACTIONS = {"BUY", "ACCEPT_OFFER"}

ACTION_EVENT_CLASS = {
    "PROBE": "log-probe",
    "SHARE_INFO": "log-trade",
    "COORDINATE": "log-collude",
    "LIE": "log-lie",
    "BID": "log-trade",
    "COUNTER_OFFER": "log-trade",
    "REJECT_OFFER": "log-trade",
    "BROADCAST": "log-trade",
    "FORM_CONNECTION": "log-probe",
}


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
            budget = 280 + self.rng.randint(0, 120)
            self.buyers[bid] = {
                "id": bid,
                "type": "buyer",
                "persona": make_persona(profile),
                "archetype": profile,
                "budget": budget,
                "goal": self._goal_for_profile(profile, budget),
                "constraints": {"max_budget": budget, "target_quantity": 1},
                "actions": list(PROFILE_ACTIONS.get(profile, ["BUY", "COMMUNICATE", "WAIT"])),
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

    def _goal_for_profile(self, profile: str, budget: int) -> dict:
        if profile == "budget":
            return {"type": "buy_if_good_deal", "max_price_per_item": int(budget * 0.85)}
        if profile == "family":
            return {"type": "must_buy_quickly", "max_price_per_item": int(budget * 0.98)}
        if profile == "investor":
            return {"type": "buy_only_clear_arbitrage", "max_price_per_item": int(budget * 0.80)}
        if profile == "flexible":
            return {"type": "wait_for_fire_sale", "max_price_per_item": int(budget * 0.82)}
        return {"type": "buy_if_acceptable", "max_price_per_item": int(budget * 0.90)}

    def contacts_of(self, agent_id: str) -> List[str]:
        return sorted(self.graph.neighbors(agent_id))

    def neighbors_of(self, agent_id: str) -> List[str]:
        return sorted([n for n in self.graph.neighbors(agent_id) if n in self.buyer_ids])

    def world_view_for(self, agent_id: str) -> dict:
        return {
            "simulation": {
                "id": self.topology,
                "summary": "Dynamic airline-seat market with local communication constrained by topology.",
                "max_rounds": self.ticks,
            },
            "market_rules": {
                "pricing": "dynamic_posted_price",
                "transaction_rule": "buyer_accepts_current_seller_price",
                "allow_buyer_communication": True,
                "allow_seller_inventory_updates": True,
            },
            "topology": {
                "name": self.topology,
                "communication_boundary": "adjacency_matrix",
                "graph_knowledge": "local_contacts_only",
            },
            "sellers": {
                sid: {"price": s.posted_price(), "inventory": s.inventory}
                for sid, s in self.sellers.items()
            },
            "neighbors": self.contacts_of(agent_id),
            "buyer_neighbors": self.neighbors_of(agent_id),
            "ticks_remaining": self.ticks - self.current_tick + 1,
        }

    def _emit(self, **kw) -> None:
        kw.setdefault("turn", self.current_tick)
        self.events.append(kw)

    def _target_list(self, target: Any) -> List[str]:
        if target is None:
            return []
        raw_values = target if isinstance(target, list) else [target]
        values: List[str] = []
        seen = set()
        for value in raw_values:
            if value is None:
                continue
            normalized = str(value).strip()
            if not normalized or normalized in seen:
                continue
            values.append(normalized)
            seen.add(normalized)
        return values

    # ---------- per-tick phases ----------

    def deliver_messages(self) -> None:
        """Deliver queued messages to recipients. inbox keeps the FULL history;
        agent_runtime filters it to the visible local transcript."""
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

        if agent["bought"] or agent.get("exited") or atype == "WAIT":
            agent["ticks_waited"] += 1
            return

        if atype == "EXIT":
            agent["exited"] = True
            agent["ticks_waited"] += 1
            self._emit(**{
                "from": agent["id"],
                "msg": f"exited the market ({action.get('reasoning', 'no reason provided')})",
                "cls": "log-trade",
            })
            return

        if atype == "BUILD_TOOL":
            agent["ticks_waited"] += 1
            tool_name = (action.get("content") or "custom_analysis_tool").strip()
            agent.setdefault("tools", []).append({"name": tool_name, "description": "Built during simulation"})
            self._emit(**{
                "from": agent["id"],
                "msg": f"built tool: {tool_name}",
                "cls": "log-tool",
            })
            return

        if atype in BUY_ACTIONS:
            targets = self._target_list(action.get("target"))
            target = targets[0] if targets else None
            seller = self.sellers.get(target)
            if seller is None:
                agent["ticks_waited"] += 1
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
                agent["ticks_waited"] += 1
                self._emit(**{"from": agent["id"], "msg": f"failed buy from {target} (sold out)",
                              "cls": "log-trade"})
            return

        if atype in MESSAGE_ACTIONS:
            recipients = self._target_list(action.get("target")) or self.contacts_of(agent["id"])
            raw_content = action.get("content")
            content = raw_content.strip() if isinstance(raw_content, str) else str(raw_content or "").strip()
            if not recipients or not content:
                agent["ticks_waited"] += 1
                return
            for to in recipients:
                if not self.matrix.get(agent["id"], {}).get(to, False):
                    # topology blocked it
                    self._emit(**{"from": agent["id"], "to": to,
                                  "msg": f"blocked: no edge ({content[:40]})",
                                  "cls": "log-lie"})
                    continue
                msg = {
                    "id": str(uuid.uuid4())[:8],
                    "turn": t,
                    "sender": agent["id"],
                    "recipient": to,
                    "content": content,
                }
                self.pending_messages.append(msg)
                agent["outbox"].append(msg)
                cls = ACTION_EVENT_CLASS.get(atype) or ("log-probe" if "?" in content else "log-trade")
                display = content if len(content) <= 100 else content[:97] + "..."
                self._emit(**{
                    "from": agent["id"], "to": to,
                    "msg": display,
                    "cls": cls,
                })
            agent["ticks_waited"] += 1
            return

        agent["ticks_waited"] += 1
        self._emit(**{
            "from": agent["id"],
            "msg": f"unsupported action '{atype}' treated as WAIT",
            "cls": "log-trade",
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
                if agent["bought"] or agent.get("exited"):
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
                    "persona": b["persona"],
                    "goal": b.get("goal"),
                    "constraints": b.get("constraints"),
                    "actions": b.get("actions"),
                    "budget": b["budget"],
                    "bought": b["bought"],
                    "exited": b.get("exited", False),
                    "purchase_price": b["purchase_price"],
                    "purchase_seller": b["purchase_seller"],
                    "satisfaction": b["satisfaction"],
                    "ticks_waited": b["ticks_waited"],
                    "tools": b.get("tools", []),
                    "messages_sent": len(b["outbox"]),
                    "messages_received": len(b["inbox"]),
                    "inbox": b["inbox"],            # full received-message history
                    "outbox": b["outbox"],          # full sent-message history
                    "beliefs": b["beliefs"],
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
            "conversations": self._conversation_threads(),
        }

    def _conversation_threads(self) -> Dict[str, List[Dict[str, Any]]]:
        """Group every message into per-pair threads keyed 'A<->B' (sorted ids)."""
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
