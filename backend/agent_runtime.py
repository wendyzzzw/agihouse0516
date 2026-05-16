"""Agent decision layer.

Two backends:
  - "claude":  spawn `claude -p --system-prompt ... --json-schema ...` per agent per tick
  - "rule":    deterministic goal-aware rule fallback (also the default offline mode)

Both return the same Action dict: {"action", "target", "content", "reasoning"}.

The buyer's archetype description (from config/*.yaml) and goal block are both
fed to the LLM — the rule fallback uses the goal block directly so behavior is
goal-driven rather than archetype-name-driven.
"""
from __future__ import annotations
import json
import subprocess
from typing import Dict, Any, Optional


ACTION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "COMMUNICATE", "WAIT"]},
        "target": {"type": ["string", "null"]},
        "content": {"type": ["string", "null"]},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are a buyer agent in a multi-agent marketplace simulation.

GAME:
- A finite set of items (e.g. airline seats). Demand > supply.
- Sellers post prices that change each tick based on demand + inventory + time.
- You compete with other buyers. Each one has its own archetype and goal.

EACH TICK YOU PICK ONE ACTION:
- BUY: purchase one unit from a seller. `target` = seller_id.
- COMMUNICATE: send ONE message to ONE neighbor. `target` = neighbor agent_id, `content` = the message text.
- WAIT: do nothing — useful if you expect prices to drop or want to gather more info.

CRITICAL CONSTRAINTS:
- You can only message neighbors listed in your context. The communication matrix is fixed.
- Other buyers may be honest, evasive, or deceptive. Use judgment.
- You only see sellers' currently posted prices.
- Stick to YOUR goal — it defines what counts as success for you.

Output STRICT JSON matching the schema. Be decisive."""


def build_user_prompt(agent_state: dict, world_view: dict) -> str:
    """Render the buyer's full local view into a prompt."""
    seller_lines = []
    for sid, info in world_view["sellers"].items():
        if info["inventory"] > 0:
            seller_lines.append(f"  - {sid}: ${info['price']} ({info['inventory']} units left)")
        else:
            seller_lines.append(f"  - {sid}: SOLD OUT")
    seller_block = "\n".join(seller_lines) or "  (none)"

    neighbors = world_view.get("neighbors") or []
    neighbor_str = ", ".join(neighbors) if neighbors else "(none — you are isolated)"

    inbox = agent_state.get("inbox", [])[-5:]
    if not inbox:
        inbox_str = "(none)"
    else:
        inbox_str = "\n".join(
            f"  - t={m.get('turn','?')} from {m['sender']}: {m['content']}" for m in inbox
        )

    beliefs = agent_state.get("beliefs") or {}
    beliefs_str = json.dumps(beliefs, ensure_ascii=False) if beliefs else "(none yet)"

    persona = agent_state.get("persona") or {}
    goal = agent_state.get("goal") or {}

    return f"""YOU ARE AGENT {agent_state['id']}.

ARCHETYPE: {persona.get('archetype') or agent_state.get('archetype', 'unknown')}
ROLE: {persona.get('role', 'buyer')}
ARCHETYPE DESCRIPTION: {persona.get('description', '')}

YOUR GOAL:
{json.dumps(goal, ensure_ascii=False, indent=2)}

YOUR STATE:
  budget          = ${agent_state['budget']}
  items_owned     = {agent_state.get('items_owned', 0)} / target {agent_state.get('target_items', 1)}
  ticks_waited    = {agent_state.get('ticks_waited', 0)}
  ticks_remaining = {world_view['ticks_remaining']}

SELLERS (currently posted prices):
{seller_block}

NEIGHBORS you can message: {neighbor_str}

RECENT INBOX:
{inbox_str}

YOUR BELIEFS:
{beliefs_str}

Pick ONE action. Respond with strict JSON only."""


def _llm_config() -> Dict[str, Any]:
    """Pull LLM knobs from the current simulation's `llm:` block."""
    from config import load, simulation as _sim, DEFAULT_CONFIG
    cfg = load(DEFAULT_CONFIG)
    sim = _sim(cfg)
    return sim.get("llm") or {}


def decide_via_claude(
    agent_state: dict,
    world_view: dict,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Shell out to `claude -p` and parse a JSON action.

    The agentic loop is suppressed via --disallowedTools so Claude Code returns
    a clean structured response rather than trying to use Bash/Edit/Read/etc.
    Output lives in `.structured_output`.
    """
    cfg = _llm_config()
    model = model or cfg.get("model", "haiku")
    timeout = timeout if timeout is not None else int(cfg.get("timeout_seconds", 60))
    cwd = cwd or cfg.get("cwd", "/tmp")
    disallowed = cfg.get("disallowed_tools") or []

    user_prompt = build_user_prompt(agent_state, world_view)
    cmd = [
        "claude", "-p", user_prompt,
        "--system-prompt", SYSTEM_PROMPT,
        "--json-schema", json.dumps(ACTION_JSON_SCHEMA),
        "--output-format", "json",
        "--model", model,
    ]
    if disallowed:
        cmd += ["--disallowedTools", *disallowed]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return _fallback(agent_state, world_view, error="timeout")

    if proc.returncode != 0:
        return _fallback(agent_state, world_view, error=f"rc={proc.returncode}: {proc.stderr[:200]}")

    try:
        wrapper = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _fallback(agent_state, world_view, error=f"non-json stdout: {proc.stdout[:200]}")

    inner = wrapper.get("structured_output") or wrapper.get("result")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            return _fallback(agent_state, world_view, error=f"result not json: {inner[:200]}")

    if not isinstance(inner, dict) or "action" not in inner:
        return _fallback(agent_state, world_view, error=f"malformed inner: {str(inner)[:200]}")
    return inner


# ============================================================================
# Rule-based fallback — goal-driven (no archetype dispatch).
# ============================================================================


def _fallback(agent_state: dict, world_view: dict, error: str = "") -> dict:
    """Rule-based decision driven by the goal block.

      buy_if_below_price : only buy when price <= max_price_per_item (or budget).
                           Mostly waits and probes social neighbors.
      must_buy_quantity  : must close the deal — pulls trigger earlier as the
                           deadline approaches and price stays within cap.
    """
    if agent_state.get("bought"):
        return {"action": "WAIT", "reasoning": "already bought"}

    sellers = world_view["sellers"]
    available = [(sid, info) for sid, info in sellers.items() if info["inventory"] > 0]
    if not available:
        return {"action": "WAIT", "reasoning": "no inventory left"}

    cheapest_id, cheapest = min(available, key=lambda x: x[1]["price"])
    price = cheapest["price"]

    goal = agent_state.get("goal") or {}
    gtype = goal.get("type")
    budget = agent_state["budget"]
    cap = int(goal.get("max_price_per_item") or budget)
    ticks_remaining = world_view.get("ticks_remaining", 10)
    ticks_waited = agent_state.get("ticks_waited", 0)

    # --- Goal: buy only if cheap enough ----------------------------------
    if gtype == "buy_if_below_price":
        if price <= cap and price <= budget:
            return {"action": "BUY", "target": cheapest_id,
                    "reasoning": f"{cheapest_id} ${price} <= cap ${cap}"}
        # Else: hold; occasionally probe neighbors for price info.
        return _maybe_probe(agent_state, world_view, cheapest_id, price)

    # --- Goal: must secure the unit --------------------------------------
    if gtype == "must_buy_quantity":
        # Time-urgent buyers ramp aggressiveness as deadline approaches.
        urgency = 1.0 - (ticks_remaining / max(1, agent_state.get("ticks_waited", 0) + ticks_remaining))
        effective_cap = int(min(cap, budget) * (0.85 + 0.15 * urgency))
        if price <= effective_cap:
            return {"action": "BUY", "target": cheapest_id,
                    "reasoning": f"must_buy: ${price} <= effective_cap ${effective_cap}"}
        if ticks_remaining <= 3 and price <= cap and price <= budget:
            return {"action": "BUY", "target": cheapest_id,
                    "reasoning": "last-chance grab (must_buy)"}
        return _maybe_probe(agent_state, world_view, cheapest_id, price)

    # --- Default: classic patient buyer ----------------------------------
    if price <= budget * 0.85:
        return {"action": "BUY", "target": cheapest_id, "reasoning": "default: below 85% of budget"}
    if ticks_remaining <= 3 and price <= budget:
        return {"action": "BUY", "target": cheapest_id, "reasoning": "default: last-chance"}
    return _maybe_probe(agent_state, world_view, cheapest_id, price)


def _maybe_probe(agent_state: dict, world_view: dict, cheapest_id: str, price: int) -> dict:
    """Periodically probe a neighbor about the cheapest known price."""
    neighbors = world_view.get("neighbors") or []
    if not neighbors:
        return {"action": "WAIT", "reasoning": "isolated, no one to ask"}
    waited = agent_state.get("ticks_waited", 0)
    if waited >= 1 and waited % 4 == 1:
        import random
        target = random.choice(neighbors)
        msg = f"What price are you seeing? I'm seeing ${price} for {cheapest_id}."
        return {"action": "COMMUNICATE", "target": target, "content": msg,
                "reasoning": "social probe for price signal"}
    return {"action": "WAIT", "reasoning": "holding for better price"}
