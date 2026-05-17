"""Scenario config loader — the single source of truth for a simulation run.

Reads `configs/*.yaml` into typed dataclasses the engine consumes. This is what
makes the system config-controlled: topology, agents, goals, and tools all come
from the YAML, not from hardcoded constants.

Public API:
    load_scenario(path)        -> Scenario
    build_comm_matrix(scenario) -> Dict[node][node] -> bool
    ConfigError                 (raised on any invalid config)
"""
from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

import yaml

from topology import generate_graph, graph_to_matrix, matrix_from_explicit, TOPOLOGIES
from personas import PERSONAS

CONFIGS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "configs")
)

# buyer_buyer accepts any named topology plus the "explicit" edge-list mode.
TOPOLOGY_MODES = list(TOPOLOGIES) + ["explicit"]


class ConfigError(ValueError):
    """Raised for any malformed or inconsistent scenario config."""


@dataclass
class SellerConfig:
    id: str
    inventory: int
    base_price: int


@dataclass
class AgentConfig:
    id: str
    persona: str
    goal: str
    tools: List[str]
    budget: Optional[int] = None       # None => engine randomizes (seeded)


@dataclass
class Scenario:
    name: str
    seed: int
    rounds: int
    buyer_buyer: str                   # named topology or "explicit"
    explicit_edges: List[List[str]]    # buyer-buyer edges, used when explicit
    sellers: List[SellerConfig]
    agents: List[AgentConfig]
    booking_opens_round: int = 1       # BUY is rejected before this round
    source_path: str = ""

    @property
    def buyer_ids(self) -> List[str]:
        return [a.id for a in self.agents]

    @property
    def seller_ids(self) -> List[str]:
        return [s.id for s in self.sellers]

    def agent(self, agent_id: str) -> AgentConfig:
        for a in self.agents:
            if a.id == agent_id:
                return a
        raise KeyError(agent_id)


def _agent_id(index: int) -> str:
    """0->A, 1->B, ... 25->Z, 26->AA, ... so rosters larger than 26 still work."""
    if index < 26:
        return string.ascii_uppercase[index]
    return _agent_id(index // 26 - 1) + string.ascii_uppercase[index % 26]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def load_scenario(path: str) -> Scenario:
    """Load + validate a scenario YAML. Raises ConfigError on any problem."""
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path) as f:
        raw = yaml.safe_load(f)
    _require(isinstance(raw, dict), f"{path}: top level must be a mapping")

    # --- scenario block ---
    sc = raw.get("scenario") or {}
    _require(isinstance(sc, dict), f"{path}: missing `scenario` block")
    name = sc.get("name")
    _require(isinstance(name, str) and name, f"{path}: scenario.name must be a non-empty string")
    seed = sc.get("seed", 42)
    _require(isinstance(seed, int), f"{path}: scenario.seed must be an int")
    rounds = sc.get("rounds", 55)
    _require(isinstance(rounds, int) and rounds > 0, f"{path}: scenario.rounds must be a positive int")

    # --- market / sellers ---
    market = raw.get("market") or {}
    raw_sellers = market.get("sellers") or []
    _require(isinstance(raw_sellers, list) and raw_sellers, f"{path}: market.sellers must be a non-empty list")
    sellers: List[SellerConfig] = []
    seen_sellers = set()
    for i, s in enumerate(raw_sellers):
        _require(isinstance(s, dict), f"{path}: market.sellers[{i}] must be a mapping")
        sid = s.get("id")
        _require(isinstance(sid, str) and sid, f"{path}: market.sellers[{i}].id required")
        _require(sid not in seen_sellers, f"{path}: duplicate seller id {sid!r}")
        seen_sellers.add(sid)
        inv = s.get("inventory")
        bp = s.get("base_price")
        _require(isinstance(inv, int) and inv > 0, f"{path}: seller {sid} inventory must be a positive int")
        _require(isinstance(bp, int) and bp > 0, f"{path}: seller {sid} base_price must be a positive int")
        sellers.append(SellerConfig(id=sid, inventory=inv, base_price=bp))

    # Booking window: BUY is rejected before this round (default 1 = always open).
    booking_opens = market.get("booking_opens_round", 1)
    _require(isinstance(booking_opens, int) and booking_opens >= 1,
             f"{path}: market.booking_opens_round must be an int >= 1")

    # --- agents / roster ---
    agents_block = raw.get("agents") or {}
    _require(isinstance(agents_block, dict), f"{path}: missing `agents` block")
    defaults = agents_block.get("defaults") or {}
    _require(isinstance(defaults, dict), f"{path}: agents.defaults must be a mapping")
    default_goal = defaults.get("goal", "")
    default_tools = defaults.get("tools", [])
    _require(isinstance(default_tools, list), f"{path}: agents.defaults.tools must be a list")

    roster = agents_block.get("roster") or []
    _require(isinstance(roster, list) and roster, f"{path}: agents.roster must be a non-empty list")
    agents: List[AgentConfig] = []
    for i, entry in enumerate(roster):
        _require(isinstance(entry, dict), f"{path}: agents.roster[{i}] must be a mapping")
        persona = entry.get("persona")
        _require(persona in PERSONAS,
                 f"{path}: roster[{i}] unknown persona {persona!r} (have {sorted(PERSONAS)})")
        count = entry.get("count", 1)
        _require(isinstance(count, int) and count > 0, f"{path}: roster[{i}].count must be a positive int")
        goal = entry.get("goal", default_goal)
        tools = entry.get("tools", default_tools)
        _require(isinstance(tools, list), f"{path}: roster[{i}].tools must be a list")
        _require(isinstance(goal, str) and goal, f"{path}: roster[{i}] has no goal (set agents.defaults.goal)")
        budget = entry.get("budget")
        _require(budget is None or isinstance(budget, int),
                 f"{path}: roster[{i}].budget must be an int or omitted")
        for _ in range(count):
            agents.append(AgentConfig(
                id=_agent_id(len(agents)),
                persona=persona,
                goal=goal,
                tools=list(tools),
                budget=budget,
            ))

    # --- topology ---
    topo = raw.get("topology") or {}
    _require(isinstance(topo, dict), f"{path}: missing `topology` block")
    buyer_buyer = topo.get("buyer_buyer", "isolated")
    _require(buyer_buyer in TOPOLOGY_MODES,
             f"{path}: topology.buyer_buyer {buyer_buyer!r} not in {TOPOLOGY_MODES}")

    explicit_edges: List[List[str]] = []
    if buyer_buyer == "explicit":
        explicit_edges = topo.get("edges") or []
        _require(isinstance(explicit_edges, list) and explicit_edges,
                 f"{path}: topology.buyer_buyer=explicit requires a non-empty `edges` list")
        valid = {a.id for a in agents}
        for j, edge in enumerate(explicit_edges):
            _require(isinstance(edge, list) and len(edge) == 2,
                     f"{path}: topology.edges[{j}] must be a [from, to] pair")
            a, b = edge
            _require(a in valid, f"{path}: topology.edges[{j}] references unknown agent {a!r}")
            _require(b in valid, f"{path}: topology.edges[{j}] references unknown agent {b!r}")
            _require(a != b, f"{path}: topology.edges[{j}] is a self-loop ({a})")

    return Scenario(
        name=name,
        seed=seed,
        rounds=rounds,
        buyer_buyer=buyer_buyer,
        explicit_edges=explicit_edges,
        sellers=sellers,
        agents=agents,
        booking_opens_round=booking_opens,
        source_path=path,
    )


def build_comm_matrix(scenario: Scenario) -> Dict[str, Dict[str, bool]]:
    """Produce the who-can-talk-to-whom boolean matrix for a scenario.

    For named topologies this delegates to the same generator the legacy engine
    used, so a preset config reproduces the legacy graph exactly. For `explicit`
    it builds the matrix from the config's edge list.
    """
    buyer_ids = scenario.buyer_ids
    seller_ids = scenario.seller_ids
    if scenario.buyer_buyer == "explicit":
        return matrix_from_explicit(buyer_ids, seller_ids, scenario.explicit_edges)
    graph = generate_graph(scenario.buyer_buyer, buyer_ids, seller_ids, scenario.seed)
    return graph_to_matrix(graph)


def load_named(name: str) -> Scenario:
    """Convenience: load configs/<name>.yaml."""
    return load_scenario(os.path.join(CONFIGS_DIR, f"{name}.yaml"))
