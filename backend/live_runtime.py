"""File-backed live simulation runtime.

This module powers the demo-oriented "real iteration" path:
  - compile scenarios from test_config.yaml
  - create a run directory under runs/live/{run_id}
  - advance buyer and seller agents turn by turn in a background task
  - persist messages, events, snapshots, and per-agent traces to local files

The implementation intentionally keeps the market mechanics small and legible.
It is built for manual demos and iteration, not production concurrency.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from agent_runtime import build_action_json_schema, build_system_prompt, build_user_prompt


MESSAGE_ACTIONS = {
    "COMMUNICATE",
    "PROBE",
    "SHARE_INFO",
    "COORDINATE",
    "LIE",
    "BROADCAST",
}
OFFER_ACTIONS = {"BID", "COUNTER_OFFER", "VOLUME_BID"}
BUYER_ACTIONS = [
    "BID",
    "COUNTER_OFFER",
    "ACCEPT_OFFER",
    "REJECT_OFFER",
    "PROBE",
    "SHARE_INFO",
    "COORDINATE",
    "WAIT",
    "EXIT",
    "LIE",
]
SELLER_ACTIONS = [
    "SET_PRICE",
    "ACCEPT_OFFER",
    "COUNTER_OFFER",
    "REJECT_OFFER",
    "PROBE",
    "BROADCAST",
    "WAIT",
]


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parse_price(text: Any) -> Optional[int]:
    if text is None:
        return None
    match = re.search(r"\$?\b(\d{2,7})\b", str(text).replace(",", ""))
    return int(match.group(1)) if match else None


def profile_from_archetype(archetype: str) -> str:
    if any(token in archetype for token in ("budget", "bargain", "ceiling", "last_minute", "sniper")):
        return "budget"
    if any(token in archetype for token in ("must_have", "deadline", "early", "anxious", "impulsive", "family")):
        return "family"
    if any(token in archetype for token in ("investor", "arbitrage", "researcher", "broker", "experimental")):
        return "investor"
    return "flexible"


def goal_quantity(agent: Dict[str, Any]) -> int:
    goal = agent.get("goal") or {}
    constraints = agent.get("constraints") or {}
    return int(goal.get("quantity") or goal.get("max_quantity") or constraints.get("target_quantity") or 1)


def max_price(agent: Dict[str, Any]) -> int:
    goal = agent.get("goal") or {}
    constraints = agent.get("constraints") or {}
    budget = int(agent.get("budget") or 0)
    return int(
        goal.get("max_price_per_item")
        or constraints.get("max_spend_per_unit")
        or constraints.get("max_spend")
        or budget
    )


@dataclass
class CompiledScenario:
    scenario_id: str
    summary: str
    max_rounds: int
    market_rules: Dict[str, Any]
    topology: Dict[str, Any]
    agents: Dict[str, Dict[str, Any]]
    comm_matrix: Dict[str, Dict[str, bool]]
    config_snapshot: Dict[str, Any]


class ScenarioCompiler:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    def list_scenarios(self) -> List[Dict[str, Any]]:
        return [
            {"id": sim["id"], "summary": sim.get("summary", "").strip()}
            for sim in self.config.get("simulations", [])
        ]

    def compile(self, scenario_id: str, seed: int, max_rounds: Optional[int] = None) -> CompiledScenario:
        scenario = next(
            (sim for sim in self.config.get("simulations", []) if sim.get("id") == scenario_id),
            None,
        )
        if scenario is None:
            raise ValueError(f"unknown scenario_id: {scenario_id}")

        archetypes = self.config.get("archetypes", {})
        agents: Dict[str, Dict[str, Any]] = {}

        for seller in scenario.get("sellers", []):
            sid = seller["id"]
            archetype = seller.get("archetype", "seller")
            description = archetypes.get("sellers", {}).get(archetype, "")
            agents[sid] = {
                "id": sid,
                "role": "seller",
                "type": "seller",
                "archetype": archetype,
                "archetype_description": description,
                "persona": {
                    "profile": archetype,
                    "description": description,
                    "traits": {"patience": 0.5, "risk_aversion": 0.5, "social": 0.5, "honesty": 0.8},
                },
                "goal": deepcopy(seller.get("goal") or {}),
                "constraints": {"min_price": seller.get("min_price", 0)},
                "actions": list(seller.get("actions") or SELLER_ACTIONS),
                "inventory": int(seller.get("inventory", 0)),
                "initial_inventory": int(seller.get("inventory", 0)),
                "current_price": int(seller.get("starting_price", 0)),
                "starting_price": int(seller.get("starting_price", 0)),
                "min_price": int(seller.get("min_price", 0)),
                "revenue": 0,
                "inbox": [],
                "outbox": [],
                "beliefs": {},
                "tools": [],
                "ticks_waited": 0,
            }

        for buyer in scenario.get("buyers", []):
            bid = buyer["id"]
            archetype = buyer.get("archetype", "buyer")
            description = archetypes.get("buyers", {}).get(archetype, "")
            profile = profile_from_archetype(archetype)
            agents[bid] = {
                "id": bid,
                "role": "buyer",
                "type": "buyer",
                "archetype": archetype,
                "archetype_description": description,
                "persona": {
                    "profile": profile,
                    "description": description,
                    "traits": {"patience": 0.65, "risk_aversion": 0.55, "social": 0.55, "honesty": 0.8},
                },
                "goal": deepcopy(buyer.get("goal") or {}),
                "constraints": {"max_budget": buyer.get("budget", 0), "target_quantity": goal_quantity(buyer)},
                "actions": list(buyer.get("actions") or BUYER_ACTIONS),
                "budget": int(buyer.get("budget", 0)),
                "initial_budget": int(buyer.get("budget", 0)),
                "items_owned": 0,
                "bought": False,
                "exited": False,
                "purchase_price": None,
                "purchase_seller": None,
                "satisfaction": None,
                "inbox": [],
                "outbox": [],
                "beliefs": {},
                "tools": [],
                "ticks_waited": 0,
            }

        seller_ids = [s["id"] for s in scenario.get("sellers", [])]
        buyer_ids = [b["id"] for b in scenario.get("buyers", [])]
        matrix = self._compile_matrix(scenario.get("topology") or {}, buyer_ids, seller_ids, seed)

        return CompiledScenario(
            scenario_id=scenario_id,
            summary=(scenario.get("summary") or "").strip(),
            max_rounds=int(max_rounds or scenario.get("settings", {}).get("max_rounds") or 10),
            market_rules=deepcopy(scenario.get("market_rules") or {}),
            topology=deepcopy(scenario.get("topology") or {}),
            agents=agents,
            comm_matrix=matrix,
            config_snapshot=deepcopy(scenario),
        )

    def _compile_matrix(
        self,
        topology: Dict[str, Any],
        buyer_ids: List[str],
        seller_ids: List[str],
        seed: int,
    ) -> Dict[str, Dict[str, bool]]:
        nodes = sorted(buyer_ids + seller_ids)
        matrix = {a: {b: False for b in nodes} for a in nodes}

        def add(a: str, b: str) -> None:
            if a not in matrix or b not in matrix or a == b:
                return
            matrix[a][b] = True
            matrix[b][a] = True

        if topology.get("input_type") == "edge_list":
            edges = topology.get("edges") or {}
            for group in ("seller_buyer", "buyer_buyer", "seller_seller"):
                for a, b in edges.get(group) or []:
                    add(a, b)
            return matrix

        generator = topology.get("generator") or {}

        if generator.get("seller_buyer_edges") == "complete_bipartite":
            for buyer in buyer_ids:
                for seller in seller_ids:
                    add(buyer, seller)

        buyer_edges = generator.get("buyer_buyer_edges")
        if buyer_edges == "complete":
            for i, a in enumerate(buyer_ids):
                for b in buyer_ids[i + 1:]:
                    add(a, b)
        elif isinstance(buyer_edges, dict) and buyer_edges.get("type") == "clustered":
            for cluster in buyer_edges.get("clusters") or []:
                for i, a in enumerate(cluster):
                    for b in cluster[i + 1:]:
                        add(a, b)
            for a, b in buyer_edges.get("bridge_edges") or []:
                add(a, b)

        if generator.get("seller_seller_edges") == "complete":
            for i, a in enumerate(seller_ids):
                for b in seller_ids[i + 1:]:
                    add(a, b)

        return matrix


class RunStore:
    def __init__(self, root: Path):
        self.root = root

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def create(self, payload: Dict[str, Any], initial_state: Dict[str, Any]) -> None:
        run_dir = self.run_dir(payload["run_id"])
        (run_dir / "state").mkdir(parents=True, exist_ok=True)
        (run_dir / "agents").mkdir(parents=True, exist_ok=True)
        (run_dir / "events").mkdir(parents=True, exist_ok=True)
        (run_dir / "messages").mkdir(parents=True, exist_ok=True)
        atomic_write_json(run_dir / "run.json", payload)
        atomic_write_json(run_dir / "state" / "turn_0.json", initial_state)
        atomic_write_json(run_dir / "result.json", snapshot_from_state(initial_state, []))

    def run_meta(self, run_id: str) -> Dict[str, Any]:
        path = self.run_dir(run_id) / "run.json"
        if not path.exists():
            raise FileNotFoundError(run_id)
        return read_json(path)

    def list_runs(self) -> List[Dict[str, Any]]:
        if not self.root.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for run_dir in sorted(self.root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
            if not run_dir.is_dir():
                continue
            meta_path = run_dir / "run.json"
            if not meta_path.exists():
                continue
            try:
                meta = read_json(meta_path)
            except Exception:
                continue
            row = dict(meta)
            try:
                state = read_json(self.state_path(str(meta["run_id"]), int(meta.get("current_turn", 0))))
                result = snapshot_from_state(state, read_jsonl(run_dir / "events" / "events.jsonl"))
                row["result_summary"] = result.get("summary")
                row["result_turn"] = result.get("current_turn")
            except Exception:
                row["result_summary"] = None
            try:
                row["message_count"] = len(read_jsonl(run_dir / "messages" / "messages.jsonl"))
            except Exception:
                row["message_count"] = 0
            rows.append(row)
        return rows

    def update_meta(self, run_id: str, **updates: Any) -> Dict[str, Any]:
        meta = self.run_meta(run_id)
        meta.update(updates)
        atomic_write_json(self.run_dir(run_id) / "run.json", meta)
        return meta

    def state_path(self, run_id: str, turn: int) -> Path:
        return self.run_dir(run_id) / "state" / f"turn_{turn}.json"

    def latest_state(self, run_id: str) -> Dict[str, Any]:
        meta = self.run_meta(run_id)
        return read_json(self.state_path(run_id, int(meta.get("current_turn", 0))))

    def state_at(self, run_id: str, turn: Optional[int]) -> Dict[str, Any]:
        meta = self.run_meta(run_id)
        target_turn = int(meta.get("current_turn", 0) if turn is None else turn)
        target_turn = max(0, min(target_turn, int(meta.get("current_turn", 0))))
        return read_json(self.state_path(run_id, target_turn))

    def write_state(self, run_id: str, state: Dict[str, Any]) -> None:
        atomic_write_json(self.state_path(run_id, int(state["turn"])), state)

    def append_event(self, run_id: str, event: Dict[str, Any]) -> None:
        append_jsonl(self.run_dir(run_id) / "events" / "events.jsonl", event)

    def append_message(self, run_id: str, message: Dict[str, Any]) -> None:
        append_jsonl(self.run_dir(run_id) / "messages" / "messages.jsonl", message)

    def write_trace(self, run_id: str, agent_id: str, turn: int, trace: Dict[str, Any]) -> None:
        atomic_write_json(self.run_dir(run_id) / "agents" / agent_id / f"turn_{turn}.json", trace)

    def events(self, run_id: str) -> List[Dict[str, Any]]:
        return read_jsonl(self.run_dir(run_id) / "events" / "events.jsonl")

    def messages(self, run_id: str) -> List[Dict[str, Any]]:
        return read_jsonl(self.run_dir(run_id) / "messages" / "messages.jsonl")

    def write_result(self, run_id: str, state: Dict[str, Any]) -> None:
        atomic_write_json(self.run_dir(run_id) / "result.json", snapshot_from_state(state, self.events(run_id)))

    def result(self, run_id: str) -> Dict[str, Any]:
        path = self.run_dir(run_id) / "result.json"
        if not path.exists():
            return snapshot_from_state(self.latest_state(run_id), self.events(run_id))
        return read_json(path)

    def config_snapshot(self, run_id: str) -> Dict[str, Any]:
        path = self.run_dir(run_id) / "config_snapshot.json"
        if not path.exists():
            raise FileNotFoundError(path)
        return read_json(path)

    def trace(self, run_id: str, agent_id: str, turn: int) -> Dict[str, Any]:
        path = self.run_dir(run_id) / "agents" / agent_id / f"turn_{turn}.json"
        if not path.exists():
            raise FileNotFoundError(path)
        return read_json(path)


class DecisionAdapter:
    def decide(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class RuleAdapter(DecisionAdapter):
    def decide(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        if agent["role"] == "seller":
            return self._seller(agent, local_view)
        return self._buyer(agent, local_view)

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _pick(items: list, index: int) -> str:
        return items[index % len(items)]

    @staticmethod
    def _has(*tokens: str, archetype: str) -> bool:
        return any(t in archetype for t in tokens)

    # ── buyer ─────────────────────────────────────────────────────────────────

    def _buyer(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        if agent.get("bought") or agent.get("exited"):
            return {"action": "WAIT", "reasoning": "already done"}

        arch = agent.get("archetype", "")
        ticks = int(agent.get("ticks_waited", 0))
        ceiling = max_price(agent)
        budget = int(agent.get("budget", 0))

        # Accept any open offer within ceiling
        offers = [o for o in local_view.get("offers", [])
                  if o.get("to") == agent["id"] and o.get("status") == "open"]
        acceptable = next((o for o in offers if int(o.get("price", 10**9)) <= ceiling), None)
        if acceptable:
            return {
                "action": "ACCEPT_OFFER",
                "target": acceptable["from"],
                "content": self._buyer_accept(arch, int(acceptable["price"])),
                "reasoning": "seller offer within ceiling — taking it",
            }

        sellers = [
            (sid, info) for sid, info in local_view.get("sellers", {}).items()
            if info.get("inventory", 0) > 0 and sid in local_view.get("neighbors", [])
        ]
        buyer_nbrs = [n for n in local_view.get("neighbors", []) if n.startswith("buyer_")]

        if not sellers:
            # No seller reachable — probe a neighbor for leads
            if buyer_nbrs:
                target = self._pick(buyer_nbrs, ticks)
                return {
                    "action": "PROBE", "target": target,
                    "content": self._pick([
                        "Anyone have a seller with inventory? I can't reach one from here.",
                        "I'm cut off from sellers right now — who do you have access to?",
                        "Any sellers still active near you? I'm stuck.",
                    ], ticks),
                    "reasoning": "no reachable sellers — probing neighbors for leads",
                }
            return {"action": "WAIT", "reasoning": "no connected seller with inventory"}

        seller_id, seller_info = min(sellers, key=lambda x: x[1]["price"])
        price = int(seller_info["price"])

        # Urgent buyers lock in immediately if affordable
        if self._has("must_have", "early_lock", "anxious", "impulsive", "deadline", archetype=arch):
            if price <= ceiling and budget >= price:
                return {
                    "action": "ACCEPT_OFFER", "target": seller_id,
                    "content": self._buyer_accept_urgent(arch, price),
                    "reasoning": "time-pressured — locking in at ceiling price",
                }

        # Standard buyers buy once they've waited a bit and price is within ceiling
        if price <= ceiling and budget >= price and ticks >= 2:
            return {
                "action": "ACCEPT_OFFER", "target": seller_id,
                "content": self._buyer_accept(arch, price),
                "reasoning": "price within ceiling after waiting",
            }

        # Action cycle: bid → probe → social move → wait
        phase = ticks % 4

        if phase == 0:
            return self._buyer_bid(arch, ticks, seller_id, price, ceiling)

        if phase == 1 and buyer_nbrs:
            return self._buyer_probe(arch, ticks, buyer_nbrs, seller_id, price)

        if phase == 2 and buyer_nbrs:
            # Manipulative / coalition players do something adversarial or social
            if self._has("manipulative", "spiteful", archetype=arch):
                return self._buyer_lie(arch, ticks, buyer_nbrs, sellers)
            if self._has("coalition", "cooperative", "information_broker", "free_rider", archetype=arch):
                return self._buyer_coordinate(arch, ticks, buyer_nbrs, seller_id, price, ceiling)
            return self._buyer_share(arch, ticks, buyer_nbrs, seller_id, price)

        if phase == 3 and buyer_nbrs and self._has(
            "researcher", "silent_sniper", "contrarian", "social_proof", archetype=arch
        ):
            return self._buyer_share(arch, ticks, buyer_nbrs, seller_id, price)

        return {"action": "WAIT", "reasoning": "holding for better price or response"}

    def _buyer_bid(self, arch: str, ticks: int, seller_id: str, price: int, ceiling: int) -> dict:
        if self._has("aggressive", "bargain", archetype=arch):
            bid = min(ceiling, max(1, int(price * 0.80)))
            msgs = [
                f"${bid}. That's my opening and it's firm — take it or I walk.",
                f"You're asking ${price}. I'll do ${bid}, not a cent more.",
                f"Market says ${bid}. I have alternatives. Your move.",
            ]
        elif self._has("whale", "bulk", archetype=arch):
            bid = min(ceiling, max(1, int(price * 0.87)))
            msgs = [
                f"I'm a volume buyer. ${bid} per unit for a multi-unit deal — interested?",
                f"Willing to take multiple units at ${bid} each. That's real volume for you.",
                f"${bid} and I'll clear more than one item. Think about it.",
            ]
        elif self._has("cooperative", "fair", archetype=arch):
            bid = min(ceiling, max(1, int(price * 0.90)))
            msgs = [
                f"I'd like to offer ${bid} — I think that's fair for both of us.",
                f"${bid} is my honest offer. I'm not trying to lowball you.",
                f"How does ${bid} sound? I want this to work for both sides.",
            ]
        elif self._has("skeptical", archetype=arch):
            bid = min(ceiling, max(1, int(price * 0.85)))
            msgs = [
                f"${price} is hard to justify. What makes this worth more than ${bid}?",
                f"I'd need a reason to pay ${price}. I'm at ${bid} until you convince me.",
                f"${bid}. And I want to know why the list price is ${price}.",
            ]
        else:
            bid = min(ceiling, max(1, int(price * 0.87)))
            msgs = [
                f"I can do ${bid} for one unit — can we close this?",
                f"${bid} is where my budget is. Any flexibility on your end?",
                f"Offering ${bid}. That's genuine, not an anchor.",
            ]
        return {
            "action": "BID", "target": seller_id,
            "content": self._pick(msgs, ticks),
            "reasoning": f"bidding ${bid} against listed ${price}",
        }

    def _buyer_probe(self, arch: str, ticks: int, nbrs: list, seller_id: str, price: int) -> dict:
        target = self._pick(nbrs, ticks)
        msgs = [
            f"What price are you seeing? I'm getting {seller_id} at ${price} — feels high.",
            f"Have you managed to negotiate anything down? {seller_id} won't move for me.",
            f"Any intel on {seller_id}'s real floor? They're quoting ${price} but I doubt that's bottom.",
            f"Are you holding out or buying? I'm at {seller_id} at ${price} and debating.",
            f"Who are you connected to? I want to know if there's a better deal somewhere else.",
        ]
        return {
            "action": "PROBE", "target": target,
            "content": self._pick(msgs, ticks),
            "reasoning": "gathering local price intelligence",
        }

    def _buyer_coordinate(self, arch: str, ticks: int, nbrs: list, seller_id: str, price: int, ceiling: int) -> dict:
        target = self._pick(nbrs, ticks)
        hold = int(ceiling * 0.82)
        msgs = [
            f"If we hold out, {seller_id} will drop. Don't buy above ${hold} — pass it on.",
            f"I'm not moving above ${hold}. If you do the same we have leverage. Spread the word.",
            f"Let's coordinate — refuse their offers this round. They need us more than we need them.",
            f"Hold the line at ${hold}. Make them come to us.",
            f"I've been talking to a few people — there's quiet agreement not to pay above ${hold}. Join us.",
        ]
        return {
            "action": "COORDINATE", "target": target,
            "content": self._pick(msgs, ticks),
            "reasoning": "coordinating a price ceiling with buyers",
        }

    def _buyer_share(self, arch: str, ticks: int, nbrs: list, seller_id: str, price: int) -> dict:
        target = self._pick(nbrs, ticks)
        floor_est = int(price * 0.87)
        msgs = [
            f"FYI: {seller_id} is at ${price} but I think they'll go to ${floor_est}. Worth pushing.",
            f"Passing this on — I got {seller_id} to move a bit. Keep bidding low, don't accept list price.",
            f"Intel: {seller_id} has inventory pressure. Their floor is probably around ${floor_est}.",
            f"Heads up: {seller_id} quoted me ${price}. Don't pay that — counter hard.",
        ]
        return {
            "action": "SHARE_INFO", "target": target,
            "content": self._pick(msgs, ticks),
            "reasoning": "sharing market intel with neighbor",
        }

    def _buyer_lie(self, arch: str, ticks: int, nbrs: list, sellers: list) -> dict:
        target = self._pick(nbrs, ticks)
        sid = sellers[0][0] if sellers else "the main seller"
        msgs = [
            f"Between us: {sid} just told me they're almost out — I'd move fast if I were you. I already locked in.",
            f"{sid} said prices are going up next round. Might want to wait for a new entrant instead.",
            f"I heard someone from another group already bought {sid}'s last good units. What I'm seeing looks like scraps.",
            f"Don't bother with {sid} — I probed them, floor is basically list price. Try your other contacts.",
        ]
        return {
            "action": "LIE", "target": target,
            "content": self._pick(msgs, ticks),
            "reasoning": "misdirecting competitor to reduce bidding pressure",
        }

    def _buyer_accept(self, arch: str, price: int) -> str:
        if self._has("skeptical", archetype=arch):
            return f"Fine. ${price}. I'm not thrilled but I'll take it."
        if self._has("cooperative", "fair", archetype=arch):
            return f"Deal at ${price}. Pleasure doing business."
        if self._has("aggressive", "bargain", archetype=arch):
            return f"Accepted at ${price}. Took long enough."
        if self._has("whale", "bulk", archetype=arch):
            return f"Taking it at ${price}. If you have more units, let's talk volume."
        return f"I'll take it at ${price}."

    def _buyer_accept_urgent(self, arch: str, price: int) -> str:
        if self._has("anxious", archetype=arch):
            return f"Yes — ${price}, locking in now before this disappears."
        if self._has("early_lock", archetype=arch):
            return f"Locking in at ${price}. Certainty matters more than squeezing you."
        if self._has("deadline", archetype=arch):
            return f"I'm on a deadline — ${price} works, let's close this."
        if self._has("impulsive", archetype=arch):
            return f"Done. ${price}. Let's go."
        return f"Accepted at ${price}. Need to close this now."

    # ── seller ────────────────────────────────────────────────────────────────

    def _seller(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        if int(agent.get("inventory", 0)) <= 0:
            return {"action": "WAIT", "reasoning": "sold out"}

        arch = agent.get("archetype", "")
        floor = int(agent.get("min_price", 0))
        current = int(agent.get("current_price", floor))
        ticks = int(agent.get("ticks_waited", 0))

        # Respond to incoming buyer offers first
        open_offers = [
            o for o in local_view.get("offers", [])
            if o.get("to") == agent["id"] and o.get("status") == "open"
        ]
        if open_offers:
            best = max(open_offers, key=lambda o: int(o.get("price", 0)))
            offered = int(best.get("price", 0))
            goal_type = agent.get("goal", {}).get("type", "")

            if self._has("desperate", "clearance", "market_maker", "impatient", archetype=arch):
                accept_threshold = floor
            elif "sell_all" in goal_type:
                accept_threshold = floor
            else:
                accept_threshold = max(floor, int(current * 0.88))

            if offered >= accept_threshold:
                return {
                    "action": "ACCEPT_OFFER", "target": best["from"],
                    "content": self._seller_accept(arch, offered),
                    "reasoning": "offer clears floor — accepting",
                }
            counter = max(floor, min(current, int(offered * 1.12)))
            return {
                "action": "COUNTER_OFFER", "target": best["from"],
                "content": self._seller_counter(arch, offered, counter),
                "reasoning": f"countering at ${counter}",
            }

        # Proactive strategy rotation
        phase = ticks % 3
        neighbors = local_view.get("neighbors", [])
        buyer = next((n for n in neighbors if n.startswith("buyer_")), None)

        if phase == 0 and buyer:
            return {
                "action": "BROADCAST", "target": buyer,
                "content": self._seller_broadcast(arch, agent),
                "reasoning": "advertising to connected buyers",
            }

        if phase == 1 and ticks >= 2:
            # Archetype-driven price adjustment
            if self._has("desperate", "clearance", archetype=arch):
                new_price = max(floor, int(current * 0.93))
            elif self._has("premium", "prestige", "hardliner", archetype=arch):
                new_price = max(floor, int(current * 0.995))
            elif self._has("experimental", "opportunistic", archetype=arch):
                new_price = max(floor, int(current * 0.94))
            else:
                new_price = max(floor, int(current * 0.96))

            if new_price < current:
                return {
                    "action": "SET_PRICE", "target": agent["id"],
                    "content": self._seller_price_drop(arch, current, new_price),
                    "reasoning": f"adjusting price to ${new_price} to stimulate demand",
                }

        return {"action": "WAIT", "reasoning": "holding position"}

    def _seller_broadcast(self, arch: str, agent: Dict) -> str:
        inv = int(agent.get("inventory", 0))
        price = int(agent.get("current_price", 0))
        units = f"{inv} unit{'s' if inv != 1 else ''}"

        if self._has("scarcity", archetype=arch):
            if inv <= 2:
                return f"Last {units} at ${price}. I can't hold these — first serious buyer gets them."
            return f"Only {units} left at ${price}. Multiple buyers are already in talks with me."
        if self._has("auctioneer", archetype=arch):
            return f"Open for bids: {units} at ${price} ask. I'm entertaining multiple offers — make yours count."
        if self._has("desperate", "clearance", archetype=arch):
            return f"I need to move these fast. {units} at ${price} — I'm flexible, let's talk."
        if self._has("premium", "prestige", archetype=arch):
            return f"Premium listing: {units} at ${price}. Not discounting, but serious buyers are welcome."
        if self._has("volume", "bundler", archetype=arch):
            return f"{units} at ${price} each. Buying 2 or more? We can discuss a package rate."
        if self._has("relationship", "loyalist", archetype=arch):
            return f"Fair deal available: {units} at ${price}. Message me — I reward serious buyers."
        if self._has("manipulative", archetype=arch):
            return f"High interest today. {units} at ${price} — I've had three inquiries this round already."
        if self._has("anchoring", archetype=arch):
            return f"Listed at ${price}, well under market. {units} available for the right buyer."
        if self._has("market_maker", archetype=arch):
            return f"Active and ready: {units} at ${price}. Counters welcome — let's find a price that works."
        if self._has("hardliner", archetype=arch):
            return f"{units} at ${price}. That's the price. Not negotiating, but you're welcome to transact."
        if self._has("experimental", archetype=arch):
            return f"Testing a new ask: {units} at ${price}. React and let me know what you think is fair."
        return f"{units} available at ${price}. Serious buyers, reach out."

    def _seller_counter(self, arch: str, offered: int, counter: int) -> str:
        if self._has("hardliner", archetype=arch):
            return f"${offered} doesn't work. ${counter} is my floor — that's final."
        if self._has("anchoring", archetype=arch):
            return f"Appreciate the offer but ${offered} doesn't cover my costs. ${counter} — that's already a real concession."
        if self._has("relationship", archetype=arch):
            return f"I want to make this work. ${offered} is too low for me, but I'll do ${counter}. Fair?"
        if self._has("manipulative", archetype=arch):
            return f"I have another offer above yours right now. ${counter} to beat it."
        if self._has("volume", "bundler", archetype=arch):
            return f"${offered} per unit doesn't work. ${counter} — or buy more units and I'll sharpen the price."
        if self._has("desperate", "clearance", archetype=arch):
            return f"Wish I could do ${offered}, I really do. ${counter} is the lowest I can go."
        if self._has("auctioneer", archetype=arch):
            return f"I've got interest from others at ${counter}. Match it and it's yours."
        return f"Can't do ${offered}. Best I can offer is ${counter}."

    def _seller_accept(self, arch: str, price: int) -> str:
        if self._has("desperate", "clearance", "impatient", archetype=arch):
            return f"Done at ${price}. Glad to close this."
        if self._has("relationship", "loyalist", archetype=arch):
            return f"Pleasure doing business at ${price}. Come back if you need more."
        if self._has("premium", "prestige", archetype=arch):
            return f"Accepted at ${price}. You're getting quality here."
        if self._has("auctioneer", archetype=arch):
            return f"Sold at ${price}. You beat the competition."
        return f"Accepted at ${price}. Transaction confirmed."

    def _seller_price_drop(self, arch: str, old: int, new: int) -> str:
        if self._has("clearance", "desperate", archetype=arch):
            return f"Dropping to ${new} — I need to clear this inventory."
        if self._has("experimental", archetype=arch):
            return f"New price point: ${new}. Testing what the market will bear."
        if self._has("market_maker", archetype=arch):
            return f"Adjusting to ${new} to keep things moving. Come talk to me."
        if self._has("opportunistic", archetype=arch):
            return f"Demand is lighter than I expected. Moving to ${new} for now."
        if self._has("relationship", archetype=arch):
            return f"Adjusting down to ${new}. I want serious buyers to find this worthwhile."
        return f"Lowering to ${new}. Reach out if you're interested."


class ClaudeCliAdapter(DecisionAdapter):
    def decide(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        from agent_runtime import decide_via_claude

        return decide_via_claude(agent, prompt_world_view(local_view), model=local_view.get("model", "haiku"))


class OpenAIAdapter(DecisionAdapter):
    def decide(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from openai import OpenAI
        except Exception as exc:
            raise RuntimeError("openai package is not installed") from exc

        schema = build_action_json_schema(agent)
        system_prompt = build_system_prompt(agent, prompt_world_view(local_view))
        user_prompt = build_user_prompt(agent, prompt_world_view(local_view))
        client = OpenAI()
        response = client.chat.completions.create(
            model=local_view.get("model") or "gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        if parsed.get("action") not in schema["properties"]["action"]["enum"]:
            raise RuntimeError(f"invalid action from OpenAI: {parsed.get('action')}")
        return parsed


def adapter_for(provider: str) -> DecisionAdapter:
    provider = (provider or "rule").lower()
    if provider == "openai":
        return OpenAIAdapter()
    if provider in ("claude", "claude_cli"):
        return ClaudeCliAdapter()
    return RuleAdapter()


def prompt_world_view(local_view: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "simulation": local_view.get("simulation") or {},
        "market_rules": local_view.get("market_rules") or {},
        "topology": local_view.get("topology") or {},
        "sellers": local_view.get("sellers") or {},
        "neighbors": local_view.get("neighbors") or [],
        "ticks_remaining": local_view.get("ticks_remaining", 0),
    }


class LiveRunEngine:
    def __init__(self, store: RunStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self.rng = random.Random(self.store.run_meta(run_id).get("seed", 42))

    def run_to_completion(self) -> None:
        meta = self.store.run_meta(self.run_id)
        provider = meta.get("llm_provider", "rule")
        speed_ms = int(meta.get("speed_ms", 500))
        adapter = adapter_for(provider)
        self.store.update_meta(self.run_id, status="running", error=None)

        try:
            while True:
                meta = self.store.run_meta(self.run_id)
                state = self.store.latest_state(self.run_id)
                if state["turn"] >= int(meta["max_rounds"]) or self._is_done(state):
                    self._finish(state)
                    return
                next_state = self.step(state, adapter)
                self.store.write_state(self.run_id, next_state)
                self.store.write_result(self.run_id, next_state)
                self.store.update_meta(self.run_id, current_turn=next_state["turn"])
                if speed_ms > 0:
                    time.sleep(speed_ms / 1000)
        except Exception as exc:
            self.store.update_meta(self.run_id, status="failed", error=str(exc))
            raise

    def step(self, state: Dict[str, Any], adapter: DecisionAdapter) -> Dict[str, Any]:
        state = deepcopy(state)
        state["turn"] += 1
        turn = int(state["turn"])
        self._deliver_pending(state)

        agent_ids = list(state["agents"].keys())
        self.rng.shuffle(agent_ids)
        for agent_id in agent_ids:
            agent = state["agents"][agent_id]
            if self._inactive(agent):
                continue
            local_view = self._local_view(state, agent_id)
            trace = self._trace_base(agent, local_view)
            try:
                decision = adapter.decide(agent, local_view)
                trace["raw_decision"] = decision
            except Exception as exc:
                decision = RuleAdapter().decide(agent, local_view)
                trace["adapter_error"] = str(exc)
                trace["raw_decision"] = decision
            decision = self._sanitize_decision(agent, decision)
            trace["parsed_action"] = decision
            self._execute(state, agent_id, decision)
            trace["post_state"] = self._agent_public(agent)
            self.store.write_trace(self.run_id, agent_id, turn, trace)

        self._snapshot_prices(state)
        return state

    def _trace_base(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        prompt_view = prompt_world_view(local_view)
        system_prompt = build_system_prompt(agent, prompt_view)
        user_prompt = build_user_prompt(agent, prompt_view)
        action_schema = build_action_json_schema(agent)
        return {
            "turn": local_view["turn"],
            "agent_id": agent["id"],
            "role": agent["role"],
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "action_schema": action_schema,
            "llm_input": {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "action_schema": action_schema,
            },
            "local_view": local_view,
        }

    def _sanitize_decision(self, agent: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(decision, dict):
            return {"action": "WAIT", "reasoning": "malformed decision"}
        action = str(decision.get("action") or "WAIT").upper()
        if action not in set(agent.get("actions") or []) | {"WAIT"}:
            action = "WAIT"
        return {
            "action": action,
            "target": decision.get("target"),
            "content": decision.get("content"),
            "reasoning": decision.get("reasoning", ""),
        }

    def _execute(self, state: Dict[str, Any], agent_id: str, decision: Dict[str, Any]) -> None:
        agent = state["agents"][agent_id]
        action = decision["action"]
        target = decision.get("target")
        content = (decision.get("content") or "").strip()

        if action == "WAIT":
            agent["ticks_waited"] = int(agent.get("ticks_waited", 0)) + 1
            return
        if action == "EXIT":
            agent["exited"] = True
            self._event(state, agent_id, None, f"{agent_id} exited the market.", "log-trade")
            return
        if action == "BUILD_TOOL":
            tool_name = content or "custom_analysis_tool"
            agent.setdefault("tools", []).append({"name": tool_name, "description": "Built during simulation"})
            self._event(state, agent_id, None, f"built tool: {tool_name}", "log-tool")
            return
        if action == "SET_PRICE":
            price = parse_price(content)
            if agent["role"] == "seller" and price is not None:
                agent["current_price"] = max(int(agent.get("min_price", 0)), price)
                self._event(state, agent_id, None, f"set listed price to ${agent['current_price']}", "log-trade")
            return
        if action in MESSAGE_ACTIONS:
            recipients = [target] if target else []
            if action == "BROADCAST" and not recipients:
                recipients = self._neighbors(state, agent_id)
            for recipient in recipients:
                if recipient:
                    self._send_message(state, agent_id, recipient, content or action.lower(), action)
            return
        if action in OFFER_ACTIONS:
            if target:
                self._create_offer(state, agent_id, target, content, action)
            return
        if action == "ACCEPT_OFFER":
            self._accept(state, agent_id, target, content)
            return
        if action == "REJECT_OFFER":
            self._reject(state, agent_id, target, content)
            return

    def _send_message(self, state: Dict[str, Any], sender: str, recipient: str, content: str, action: str) -> bool:
        if not self._can_talk(state, sender, recipient):
            self._event(state, sender, recipient, f"blocked: no edge ({content[:60]})", "log-lie")
            return False
        msg = {
            "id": str(uuid.uuid4())[:8],
            "turn": state["turn"],
            "sender": sender,
            "recipient": recipient,
            "content": content,
            "action": action,
        }
        state.setdefault("pending_messages", []).append(msg)
        state["agents"][sender].setdefault("outbox", []).append(msg)
        self.store.append_message(self.run_id, msg)
        cls = {
            "PROBE": "log-probe",
            "COORDINATE": "log-collude",
            "LIE": "log-lie",
            "BROADCAST": "log-trade",
        }.get(action, "log-trade")
        self._event(state, sender, recipient, content, cls)
        return True

    def _create_offer(self, state: Dict[str, Any], sender: str, recipient: str, content: str, action: str) -> None:
        if not self._can_talk(state, sender, recipient):
            self._event(state, sender, recipient, f"blocked offer: no edge ({content[:60]})", "log-lie")
            return
        price = parse_price(content)
        if price is None:
            self._send_message(state, sender, recipient, content or "I want to negotiate.", action)
            return
        offer = {
            "id": str(uuid.uuid4())[:8],
            "turn": state["turn"],
            "from": sender,
            "to": recipient,
            "price": price,
            "quantity": 1,
            "status": "open",
            "content": content,
            "action": action,
        }
        state.setdefault("offers", []).append(offer)
        self._send_message(state, sender, recipient, content, action)
        self._event(state, sender, recipient, f"offered ${price}", "log-trade")

    def _accept(self, state: Dict[str, Any], agent_id: str, target: Optional[str], content: str) -> None:
        agent = state["agents"][agent_id]
        if agent["role"] == "buyer" and target in state["sellers"]:
            price = parse_price(content) or int(state["agents"][target]["current_price"])
            self._transaction(state, buyer_id=agent_id, seller_id=target, price=price)
            return

        offer = self._latest_open_offer(state, from_id=target, to_id=agent_id)
        if offer is None:
            self._send_message(state, agent_id, target, content or "I accept.", "ACCEPT_OFFER") if target else None
            return

        buyer_id = offer["from"] if state["agents"][offer["from"]]["role"] == "buyer" else offer["to"]
        seller_id = offer["to"] if state["agents"][offer["to"]]["role"] == "seller" else offer["from"]
        if self._transaction(state, buyer_id=buyer_id, seller_id=seller_id, price=int(offer["price"])):
            offer["status"] = "accepted"

    def _reject(self, state: Dict[str, Any], agent_id: str, target: Optional[str], content: str) -> None:
        offer = self._latest_open_offer(state, from_id=target, to_id=agent_id)
        if offer:
            offer["status"] = "rejected"
        if target:
            self._send_message(state, agent_id, target, content or "I reject that offer.", "REJECT_OFFER")

    def _transaction(self, state: Dict[str, Any], buyer_id: str, seller_id: str, price: int) -> bool:
        buyer = state["agents"].get(buyer_id)
        seller = state["agents"].get(seller_id)
        if not buyer or not seller or buyer["role"] != "buyer" or seller["role"] != "seller":
            return False
        if int(seller.get("inventory", 0)) <= 0:
            self._event(state, buyer_id, seller_id, "failed transaction: seller is sold out", "log-lie")
            return False
        if price < int(seller.get("min_price", 0)):
            self._event(state, seller_id, buyer_id, f"rejected ${price}: below floor", "log-trade")
            return False
        if price > int(buyer.get("budget", 0)):
            self._event(state, buyer_id, seller_id, f"failed transaction: ${price} exceeds budget", "log-lie")
            return False

        seller["inventory"] = int(seller["inventory"]) - 1
        seller["revenue"] = int(seller.get("revenue", 0)) + price
        buyer["budget"] = int(buyer["budget"]) - price
        buyer["items_owned"] = int(buyer.get("items_owned", 0)) + 1
        buyer["purchase_price"] = price
        buyer["purchase_seller"] = seller_id
        buyer["bought"] = buyer["items_owned"] >= goal_quantity(buyer)
        under = max_price(buyer) - price
        buyer["satisfaction"] = max(0, min(100, int(65 + 35 * max(0, under) / max(max_price(buyer), 1))))
        self._event(
            state,
            buyer_id,
            seller_id,
            f"BOUGHT from {seller_id} at ${price} ({buyer['archetype']}, {buyer['satisfaction']}% sat)",
            "log-buy",
            letter=buyer_id,
            price=price,
            sat=buyer["satisfaction"],
        )
        return True

    def _latest_open_offer(self, state: Dict[str, Any], from_id: Optional[str], to_id: Optional[str]) -> Optional[Dict[str, Any]]:
        for offer in reversed(state.get("offers", [])):
            if offer.get("status") != "open":
                continue
            if from_id is not None and offer.get("from") != from_id:
                continue
            if to_id is not None and offer.get("to") != to_id:
                continue
            return offer
        return None

    def _deliver_pending(self, state: Dict[str, Any]) -> None:
        pending = state.get("pending_messages", [])
        for msg in pending:
            recipient = state["agents"].get(msg["recipient"])
            if recipient is not None:
                recipient.setdefault("inbox", []).append(msg)
        state["pending_messages"] = []

    def _snapshot_prices(self, state: Dict[str, Any]) -> None:
        sellers = [a for a in state["agents"].values() if a["role"] == "seller"]
        point: Dict[str, Any] = {"turn": state["turn"]}
        for idx, seller in enumerate(sellers):
            key = chr(ord("a") + idx)
            point[key] = int(seller.get("current_price", 0))
            point[f"inv_{key}"] = int(seller.get("inventory", 0))
        state.setdefault("prices_over_time", []).append(point)

    def _event(self, state: Dict[str, Any], sender: str, recipient: Optional[str], msg: str, cls: str, **extra: Any) -> None:
        event = {"turn": state["turn"], "from": sender, "to": recipient, "msg": msg, "cls": cls}
        event.update(extra)
        state.setdefault("events", []).append(event)
        self.store.append_event(self.run_id, event)

    def _local_view(self, state: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
        agent = state["agents"][agent_id]
        neighbors = self._neighbors(state, agent_id)
        sellers = {
            sid: {"price": int(s["current_price"]), "inventory": int(s["inventory"])}
            for sid, s in state["agents"].items()
            if s["role"] == "seller" and (sid == agent_id or sid in neighbors or state["market_rules"].get("public_board"))
        }
        return {
            "turn": state["turn"],
            "simulation": state.get("simulation", {}),
            "market_rules": state.get("market_rules", {}),
            "topology": state.get("topology", {}),
            "model": state.get("model"),
            "agent": self._agent_public(agent),
            "sellers": sellers,
            "neighbors": neighbors,
            "ticks_remaining": max(0, int(state["max_rounds"]) - int(state["turn"]) + 1),
            "offers": [
                o for o in state.get("offers", [])
                if o.get("from") == agent_id or o.get("to") == agent_id
            ],
        }

    def _agent_public(self, agent: Dict[str, Any]) -> Dict[str, Any]:
        excluded = {"inbox", "outbox"}
        return {k: deepcopy(v) for k, v in agent.items() if k not in excluded}

    def _neighbors(self, state: Dict[str, Any], agent_id: str) -> List[str]:
        return sorted([n for n, ok in state["comm_matrix"].get(agent_id, {}).items() if ok])

    def _can_talk(self, state: Dict[str, Any], a: str, b: str) -> bool:
        return bool(state["comm_matrix"].get(a, {}).get(b, False))

    def _inactive(self, agent: Dict[str, Any]) -> bool:
        if agent.get("exited"):
            return True
        if agent["role"] == "buyer" and agent.get("bought"):
            return True
        if agent["role"] == "seller" and int(agent.get("inventory", 0)) <= 0:
            return True
        return False

    def _is_done(self, state: Dict[str, Any]) -> bool:
        sellers = [a for a in state["agents"].values() if a["role"] == "seller"]
        buyers = [a for a in state["agents"].values() if a["role"] == "buyer"]
        return all(int(s.get("inventory", 0)) <= 0 for s in sellers) or all(
            b.get("bought") or b.get("exited") for b in buyers
        )

    def _finish(self, state: Dict[str, Any]) -> None:
        self.store.write_result(self.run_id, state)
        self.store.update_meta(self.run_id, status="completed", current_turn=state["turn"])


def initial_state(compiled: CompiledScenario, run_id: str, seed: int, provider: str, model: str) -> Dict[str, Any]:
    seller_ids = [aid for aid, a in compiled.agents.items() if a["role"] == "seller"]
    buyer_ids = [aid for aid, a in compiled.agents.items() if a["role"] == "buyer"]
    return {
        "run_id": run_id,
        "turn": 0,
        "seed": seed,
        "model": model,
        "llm_provider": provider,
        "scenario_id": compiled.scenario_id,
        "max_rounds": compiled.max_rounds,
        "simulation": {
            "id": compiled.scenario_id,
            "summary": compiled.summary,
            "max_rounds": compiled.max_rounds,
        },
        "market_rules": compiled.market_rules,
        "topology": compiled.topology,
        "agents": deepcopy(compiled.agents),
        "seller_ids": seller_ids,
        "buyer_ids": buyer_ids,
        "sellers": seller_ids,
        "buyers": buyer_ids,
        "comm_matrix": compiled.comm_matrix,
        "pending_messages": [],
        "offers": [],
        "events": [],
        "prices_over_time": [],
    }


def neighbors_from_state(state: Dict[str, Any], agent_id: str) -> List[str]:
    return sorted([n for n, ok in state.get("comm_matrix", {}).get(agent_id, {}).items() if ok])


def agent_public(agent: Dict[str, Any]) -> Dict[str, Any]:
    excluded = {"inbox", "outbox"}
    return {k: deepcopy(v) for k, v in agent.items() if k not in excluded}


def local_view_from_state(state: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    agent = state["agents"][agent_id]
    neighbors = neighbors_from_state(state, agent_id)
    sellers = {
        sid: {"price": int(s["current_price"]), "inventory": int(s["inventory"])}
        for sid, s in state["agents"].items()
        if s["role"] == "seller" and (sid == agent_id or sid in neighbors or state["market_rules"].get("public_board"))
    }
    return {
        "turn": state["turn"],
        "simulation": state.get("simulation", {}),
        "market_rules": state.get("market_rules", {}),
        "topology": state.get("topology", {}),
        "model": state.get("model"),
        "agent": agent_public(agent),
        "sellers": sellers,
        "neighbors": neighbors,
        "ticks_remaining": max(0, int(state["max_rounds"]) - int(state["turn"]) + 1),
        "offers": [
            o for o in state.get("offers", [])
            if o.get("from") == agent_id or o.get("to") == agent_id
        ],
    }


def player_contexts_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    players = []
    for agent_id, agent in sorted(state.get("agents", {}).items()):
        local_view = local_view_from_state(state, agent_id)
        prompt_view = prompt_world_view(local_view)
        players.append({
            "id": agent_id,
            "role": agent.get("role"),
            "archetype": agent.get("archetype"),
            "archetype_description": agent.get("archetype_description"),
            "persona": agent.get("persona"),
            "goal": agent.get("goal"),
            "constraints": agent.get("constraints"),
            "actions": agent.get("actions"),
            "neighbors": local_view.get("neighbors", []),
            "status": {
                "bought": agent.get("bought", False),
                "exited": agent.get("exited", False),
                "purchase_price": agent.get("purchase_price"),
                "purchase_seller": agent.get("purchase_seller"),
                "budget": agent.get("budget"),
                "inventory": agent.get("inventory"),
                "current_price": agent.get("current_price"),
                "revenue": agent.get("revenue"),
                "messages_sent": len(agent.get("outbox", [])),
                "messages_received": len(agent.get("inbox", [])),
            },
            "system_prompt": build_system_prompt(agent, prompt_view),
            "user_prompt": build_user_prompt(agent, prompt_view),
            "local_view": local_view,
        })
    return players


def snapshot_from_state(state: Dict[str, Any], events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    events = events if events is not None else state.get("events", [])
    buyers = {aid: a for aid, a in state.get("agents", {}).items() if a.get("role") == "buyer"}
    sellers = {aid: a for aid, a in state.get("agents", {}).items() if a.get("role") == "seller"}
    bought = [b for b in buyers.values() if b.get("purchase_price") is not None]
    prices = [int(b["purchase_price"]) for b in bought]
    avg_price = round(sum(prices) / len(prices), 1) if prices else 0
    avg_sat = round(sum(int(b.get("satisfaction") or 0) for b in bought) / len(bought), 1) if bought else 0
    by_profile: Dict[str, Optional[float]] = {}
    for buyer in bought:
        profile = buyer.get("persona", {}).get("profile", buyer.get("archetype", "buyer"))
        by_profile.setdefault(profile, [])
        by_profile[profile].append(int(buyer["purchase_price"]))
    by_profile = {
        profile: round(sum(values) / len(values), 1) if values else None
        for profile, values in by_profile.items()
    }
    conversations: Dict[str, List[Dict[str, Any]]] = {}
    for agent in state.get("agents", {}).values():
        for msg in agent.get("outbox", []):
            a, b = msg["sender"], msg["recipient"]
            key = f"{min(a, b)}<->{max(a, b)}"
            conversations.setdefault(key, []).append({
                "turn": msg["turn"],
                "from": a,
                "to": b,
                "content": msg["content"],
            })
    for rows in conversations.values():
        rows.sort(key=lambda row: row["turn"])
    players = {
        aid: {
            "id": aid,
            "role": a.get("role"),
            "profile": a.get("persona", {}).get("profile"),
            "persona": a.get("persona"),
            "archetype": a.get("archetype"),
            "archetype_description": a.get("archetype_description"),
            "goal": a.get("goal"),
            "constraints": a.get("constraints"),
            "actions": a.get("actions"),
            "neighbors": neighbors_from_state(state, aid),
            "bought": a.get("bought", False),
            "exited": a.get("exited", False),
            "purchase_price": a.get("purchase_price"),
            "purchase_seller": a.get("purchase_seller"),
            "satisfaction": a.get("satisfaction"),
            "budget": a.get("budget"),
            "inventory": a.get("inventory"),
            "current_price": a.get("current_price"),
            "messages_sent": len(a.get("outbox", [])),
            "messages_received": len(a.get("inbox", [])),
        }
        for aid, a in state.get("agents", {}).items()
    }

    return {
        "run_id": state.get("run_id"),
        "topology": state.get("scenario_id"),
        "scenario_id": state.get("scenario_id"),
        "seed": state.get("seed"),
        "ticks": state.get("max_rounds"),
        "current_turn": state.get("turn", 0),
        "llm_mode": state.get("llm_provider"),
        "summary": {
            "avg_price": avg_price,
            "by_profile": by_profile,
            "n_bought": len(bought),
            "n_completed_goals": len([b for b in buyers.values() if b.get("bought")]),
            "n_missed": len([b for b in buyers.values() if not b.get("bought") and not b.get("exited")]),
            "avg_satisfaction": avg_sat,
            "total_messages": sum(len(a.get("outbox", [])) for a in state.get("agents", {}).values()),
        },
        "prices_over_time": state.get("prices_over_time", []),
        "events": events,
        "agents": {
            aid: {
                "id": aid,
                "profile": a.get("persona", {}).get("profile"),
                "persona": a.get("persona"),
                "archetype": a.get("archetype"),
                "goal": a.get("goal"),
                "constraints": a.get("constraints"),
                "actions": a.get("actions"),
                "budget": a.get("budget"),
                "bought": a.get("bought", False),
                "exited": a.get("exited", False),
                "purchase_price": a.get("purchase_price"),
                "purchase_seller": a.get("purchase_seller"),
                "satisfaction": a.get("satisfaction"),
                "ticks_waited": a.get("ticks_waited", 0),
                "tools": a.get("tools", []),
                "messages_sent": len(a.get("outbox", [])),
                "messages_received": len(a.get("inbox", [])),
                "inbox": a.get("inbox", []),
                "outbox": a.get("outbox", []),
                "beliefs": a.get("beliefs", {}),
            }
            for aid, a in buyers.items()
        },
        "players": players,
        "sellers": {
            aid: {
                "name": aid,
                "archetype": a.get("archetype"),
                "base_price": a.get("starting_price"),
                "final_price": a.get("current_price"),
                "current_price": a.get("current_price"),
                "min_price": a.get("min_price"),
                "initial_inventory": a.get("initial_inventory"),
                "final_inventory": a.get("inventory"),
                "revenue": a.get("revenue", 0),
            }
            for aid, a in sellers.items()
        },
        "comm_matrix": state.get("comm_matrix", {}),
        "conversations": conversations,
    }


def create_live_run(
    store: RunStore,
    config_path: Path,
    scenario_id: str,
    seed: int,
    max_rounds: Optional[int],
    llm_provider: str,
    model: str,
    speed_ms: int,
) -> Dict[str, Any]:
    compiler = ScenarioCompiler(config_path)
    compiled = compiler.compile(scenario_id, seed=seed, max_rounds=max_rounds)
    run_id = f"{scenario_id}-{uuid.uuid4().hex[:8]}"
    state = initial_state(compiled, run_id, seed, llm_provider, model)
    meta = {
        "run_id": run_id,
        "status": "queued",
        "scenario_id": scenario_id,
        "seed": seed,
        "max_rounds": compiled.max_rounds,
        "current_turn": 0,
        "llm_provider": llm_provider,
        "model": model,
        "speed_ms": speed_ms,
        "summary": compiled.summary,
        "created_at": time.time(),
        "error": None,
    }
    store.create(meta, state)
    atomic_write_json(store.run_dir(run_id) / "config_snapshot.json", compiled.config_snapshot)
    return meta


def run_live_background(store: RunStore, run_id: str) -> None:
    LiveRunEngine(store, run_id).run_to_completion()
