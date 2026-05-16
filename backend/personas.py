"""Archetype accessor — replaces the old 4-profile persona module.

The YAML schema names them "archetypes" (sellers + buyers), with a free-text
description used to seed the LLM prompt. We keep a thin compatibility shim so
older imports of `make_persona` / `DEFAULT_TOOLS` still work; they now consult
the loaded YAML rather than a hardcoded Python dict.
"""
from __future__ import annotations
from typing import Dict, Any

from config import load, archetype as _archetype, DEFAULT_CONFIG


DEFAULT_TOOLS = [
    {"name": "ask_price", "description": "Observe a seller's currently posted price."},
    {"name": "send_message", "description": "Send a message to one neighbor (if comm matrix permits)."},
    {"name": "buy", "description": "Purchase a seat from a seller at their current price."},
]


def make_persona(role: str, archetype_name: str, config_path: str = DEFAULT_CONFIG) -> Dict[str, Any]:
    """Build a persona dict from an archetype name. Used by the engine when
    spawning each agent from the simulation's sellers/buyers list."""
    cfg = load(config_path)
    return {
        "archetype": archetype_name,
        "role": role,                  # "seller" or "buyer"
        "description": _archetype(cfg, role + "s" if not role.endswith("s") else role, archetype_name),
    }
