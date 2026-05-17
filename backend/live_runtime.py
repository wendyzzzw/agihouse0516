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

    def _buyer(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        if agent.get("bought") or agent.get("exited"):
            return {"action": "WAIT", "reasoning": "already done"}

        offers = [o for o in local_view.get("offers", []) if o.get("to") == agent["id"] and o.get("status") == "open"]
        acceptable_offer = next((o for o in offers if int(o.get("price", 10**9)) <= max_price(agent)), None)
        if acceptable_offer:
            return {
                "action": "ACCEPT_OFFER",
                "target": acceptable_offer["from"],
                "content": f"I accept your offer at ${acceptable_offer['price']}.",
                "reasoning": "seller offer is within max price",
            }

        sellers = [
            (sid, info)
            for sid, info in local_view.get("sellers", {}).items()
            if info.get("inventory", 0) > 0 and sid in local_view.get("neighbors", [])
        ]
        if not sellers:
            return {"action": "WAIT", "reasoning": "no connected seller with inventory"}
        seller_id, seller = min(sellers, key=lambda item: item[1]["price"])
        price = int(seller["price"])
        target_price = max_price(agent)
        if price <= target_price and agent.get("budget", 0) >= price:
            return {
                "action": "ACCEPT_OFFER",
                "target": seller_id,
                "content": f"I accept the listed price of ${price}.",
                "reasoning": "listed price satisfies buyer goal",
            }

        ticks_waited = int(agent.get("ticks_waited", 0))
        bid_price = min(target_price, max(1, int(price * 0.86)))
        if ticks_waited % 3 == 0:
            return {
                "action": "BID",
                "target": seller_id,
                "content": f"I can offer ${bid_price} for one item today.",
                "reasoning": "testing seller willingness to discount",
            }

        buyer_neighbors = [n for n in local_view.get("neighbors", []) if n.startswith("buyer_")]
        if buyer_neighbors and ticks_waited % 3 == 1:
            return {
                "action": "PROBE",
                "target": buyer_neighbors[0],
                "content": f"What price are you seeing? I see {seller_id} at ${price}.",
                "reasoning": "gather local price intelligence",
            }

        return {"action": "WAIT", "reasoning": "waiting for a better price or response"}

    def _seller(self, agent: Dict[str, Any], local_view: Dict[str, Any]) -> Dict[str, Any]:
        if int(agent.get("inventory", 0)) <= 0:
            return {"action": "WAIT", "reasoning": "sold out"}

        open_offers = [
            o for o in local_view.get("offers", [])
            if o.get("to") == agent["id"] and o.get("status") == "open"
        ]
        if open_offers:
            best = max(open_offers, key=lambda o: int(o.get("price", 0)))
            price = int(best.get("price", 0))
            floor = int(agent.get("min_price", 0))
            current = int(agent.get("current_price", floor))
            if price >= floor and (price >= current * 0.88 or agent.get("goal", {}).get("type") == "sell_all"):
                return {
                    "action": "ACCEPT_OFFER",
                    "target": best["from"],
                    "content": f"Accepted at ${price}.",
                    "reasoning": "offer clears floor and advances seller goal",
                }
            counter = max(floor, min(current, int(price * 1.10)))
            return {
                "action": "COUNTER_OFFER",
                "target": best["from"],
                "content": f"I cannot do ${price}. I can do ${counter}.",
                "reasoning": "countering above floor",
            }

        ticks_waited = int(agent.get("ticks_waited", 0))
        if ticks_waited and ticks_waited % 4 == 0:
            floor = int(agent.get("min_price", 0))
            new_price = max(floor, int(agent.get("current_price", floor) * 0.96))
            return {
                "action": "SET_PRICE",
                "target": agent["id"],
                "content": f"New listed price: ${new_price}.",
                "reasoning": "lowering price after limited demand",
            }

        neighbors = local_view.get("neighbors", [])
        buyer = next((n for n in neighbors if n.startswith("buyer_")), None)
        if buyer and ticks_waited % 3 == 1:
            return {
                "action": "BROADCAST",
                "target": buyer,
                "content": f"{agent['id']} has {agent['inventory']} units listed at ${agent['current_price']}.",
                "reasoning": "advertise current listing to a connected buyer",
            }

        return {"action": "WAIT", "reasoning": "holding current price"}


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
