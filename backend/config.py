"""Config loader — reads YAML in the schema used across the team.

All scenario files (flight_booking.yaml, test_config.yaml, example_config.yaml)
share the same top-level shape:

    config_version: 1
    global_defaults: {...}
    archetypes:
      sellers: { name: description, ... }
      buyers:  { name: description, ... }
    simulations:
      - id: <string>
        settings: {...}
        market_rules: {...}
        topology: {...}
        sellers: [...]
        buyers:  [...]
        pricing: {...}        # optional, our extension
        llm:     {...}        # optional, our extension

`load(path)` returns the parsed dict; `simulation(cfg, id)` picks one sim;
`archetype(cfg, role, name)` resolves an archetype description by name.
"""
from __future__ import annotations
import os
import yaml
from functools import lru_cache
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# YAML configs live at the repo root (matches teammates' file layout).
CONFIG_DIR = REPO_ROOT

DEFAULT_CONFIG = os.path.join(CONFIG_DIR, "flight_booking.yaml")


@lru_cache(maxsize=16)
def load(path: str = DEFAULT_CONFIG) -> Dict[str, Any]:
    """Load and cache a YAML config file."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_simulations(cfg: Dict[str, Any]) -> List[str]:
    return [s["id"] for s in cfg.get("simulations", [])]


def simulation(cfg: Dict[str, Any], sim_id: Optional[str] = None) -> Dict[str, Any]:
    """Return the simulation dict by id. If id is None, return the first one."""
    sims = cfg.get("simulations") or []
    if not sims:
        raise ValueError("config has no `simulations:` block")
    if sim_id is None:
        return sims[0]
    for s in sims:
        if s["id"] == sim_id:
            return s
    raise KeyError(f"simulation id not found: {sim_id} (available: {[s['id'] for s in sims]})")


def archetype(cfg: Dict[str, Any], role: str, name: str) -> str:
    """Resolve an archetype description by role ('sellers'|'buyers') and name.
    Returns the trimmed description string, or a placeholder if not found."""
    pool = (cfg.get("archetypes") or {}).get(role) or {}
    desc = pool.get(name)
    if not desc:
        return f"({role[:-1]} archetype `{name}` — no description in config)"
    return " ".join(str(desc).split())


def global_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return dict(cfg.get("global_defaults") or {})
