"""Agent decision layer.

Two backends:
  - "claude":  spawn `claude -p --bare --json-schema ...` per agent per tick
  - "rule":    deterministic rule-based fallback (also the offline default)

Both return the same Action dict: {"action", "target", "content", "reasoning"}.
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


SYSTEM_PROMPT = """You are a buyer agent in a multi-agent flight booking simulation.

GAME:
- A finite set of airline seats. Demand > supply.
- Sellers post prices that change each tick based on demand + inventory + time.
- You and other buyers compete. Some are patient, some aggressive, some social.

EACH TICK YOU PICK ONE ACTION:
- BUY: purchase a seat from a seller. `target` = seller_id (e.g. "Airline_A").
- COMMUNICATE: send ONE message to ONE neighbor. `target` = neighbor agent_id, `content` = the message text.
- WAIT: do nothing — useful when you expect prices to drop.

CRITICAL CONSTRAINTS:
- You can only message neighbors listed in your context. The communication matrix is fixed.
- Other buyers may be honest, evasive, or deceptive. Use judgment.
- You see only the currently posted seller prices (and whatever neighbors told you).
- If you wait too long and seats sell out, you get nothing.

STRATEGY:
- Use your persona traits. A patient/budget buyer should rarely BUY early.
- A family/aggressive buyer should lock in seats once price is acceptable.
- Asking neighbors can reveal what they've seen — but costs a tick.
- Coordinated waiting can pressure sellers to lower prices (but defectors profit individually).

Output STRICT JSON matching the schema. Be decisive."""


def build_user_prompt(agent_state: dict, world_view: dict) -> str:
    """Render the agent's full local view as a prompt."""
    seller_lines = []
    for sid, info in world_view["sellers"].items():
        if info["inventory"] > 0:
            seller_lines.append(f"  - {sid}: ${info['price']} ({info['inventory']} seats left)")
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

    persona = agent_state["persona"]
    return f"""YOU ARE AGENT {agent_state['id']}.

PERSONA:
  profile     = {persona['profile']}
  description = {persona['description']}
  traits      = {persona['traits']}

YOUR STATE:
  budget               = ${agent_state['budget']}
  ticks_waited         = {agent_state['ticks_waited']}
  ticks_remaining      = {world_view['ticks_remaining']}

SELLERS (currently posted prices):
{seller_block}

NEIGHBORS you can message: {neighbor_str}

RECENT INBOX:
{inbox_str}

YOUR BELIEFS:
{beliefs_str}

Pick ONE action. Respond with strict JSON only."""


# Tools we explicitly disallow so Claude Code doesn't try to use them mid-decision.
_DISALLOWED_TOOLS = [
    "Bash", "Edit", "Read", "Write", "Grep", "Glob",
    "TodoWrite", "TaskCreate", "WebFetch", "WebSearch", "Task", "Agent",
    "NotebookEdit", "Skill",
]


def decide_via_claude(
    agent_state: dict,
    world_view: dict,
    model: str = "haiku",
    timeout: int = 60,
    cwd: Optional[str] = "/tmp",       # run from a CLAUDE.md-free dir
) -> dict:
    """Shell out to `claude -p` and parse a JSON action.

    Notes:
      - We DON'T use --bare because that requires ANTHROPIC_API_KEY (no OAuth).
        Instead we override --system-prompt (skips CLAUDE.md auto-load) and
        --disallowedTools (stops the agentic loop from spinning up tools).
      - The structured JSON output lives in `.structured_output`, not `.result`.
        `.result` is the freeform text channel (often empty when schema is set).
    """
    user_prompt = build_user_prompt(agent_state, world_view)
    cmd = [
        "claude", "-p", user_prompt,
        "--system-prompt", SYSTEM_PROMPT,
        "--json-schema", json.dumps(ACTION_JSON_SCHEMA),
        "--output-format", "json",
        "--model", model,
        "--disallowedTools", *_DISALLOWED_TOOLS,
    ]
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

    # Prefer .structured_output (always conforms to json-schema). Fall back to .result.
    inner = wrapper.get("structured_output") or wrapper.get("result")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            return _fallback(agent_state, world_view, error=f"result not json: {inner[:200]}")

    if not isinstance(inner, dict) or "action" not in inner:
        return _fallback(agent_state, world_view, error=f"malformed inner: {str(inner)[:200]}")
    return inner


def _fallback(agent_state: dict, world_view: dict, error: str = "") -> dict:
    """Rule-based decision. Used as fallback AND as the default offline mode."""
    if agent_state.get("bought"):
        return {"action": "WAIT", "reasoning": "already bought"}

    sellers = world_view["sellers"]
    available = [(sid, info) for sid, info in sellers.items() if info["inventory"] > 0]
    if not available:
        return {"action": "WAIT", "reasoning": "no inventory available"}

    cheapest_id, cheapest = min(available, key=lambda x: x[1]["price"])
    traits = agent_state["persona"].get("traits", {})
    profile = agent_state["persona"]["profile"]
    budget = agent_state["budget"]
    ticks_waited = agent_state.get("ticks_waited", 0)
    ticks_remaining = world_view.get("ticks_remaining", 10)

    # Per-profile thresholds — these are the knobs to calibrate the 22% spread.
    if profile == "budget":
        accept_pct, max_wait = 0.85, 30
    elif profile == "family":
        accept_pct, max_wait = 0.98, 10
    elif profile == "investor":
        accept_pct, max_wait = 0.80, 25
    elif profile == "flexible":
        accept_pct, max_wait = 0.82, 40
    else:
        accept_pct, max_wait = 0.90, 20

    deal_price = budget * accept_pct
    if cheapest["price"] <= deal_price:
        return {"action": "BUY", "target": cheapest_id,
                "reasoning": f"{cheapest_id} @ ${cheapest['price']} <= deal ${int(deal_price)}"}

    # Last-chance buy near end of window
    if ticks_remaining <= 3 and cheapest["price"] <= budget:
        return {"action": "BUY", "target": cheapest_id,
                "reasoning": "last-chance grab before window closes"}

    # Patience exhausted
    if ticks_waited > max_wait and cheapest["price"] <= budget:
        return {"action": "BUY", "target": cheapest_id,
                "reasoning": "patience exhausted"}

    # Social agents probe neighbors while waiting
    social = traits.get("social", 0.3)
    neighbors = world_view.get("neighbors") or []
    if neighbors and social >= 0.4 and ticks_waited >= 1 and (ticks_waited % 4 == 1):
        import random
        target = random.choice(neighbors)
        msg = f"What price are you seeing? I'm seeing ${cheapest['price']} for {cheapest_id}."
        return {"action": "COMMUNICATE", "target": target, "content": msg,
                "reasoning": "social probe for price signal"}

    # High-social patient agents try to coordinate a hold
    patience = traits.get("patience", 0.5)
    if neighbors and social >= 0.6 and patience >= 0.7 and ticks_waited == 5:
        import random
        target = random.choice(neighbors)
        target_price = int(budget * 0.75)
        msg = f"Let's all hold out — don't buy above ${target_price}. Forces them to drop."
        return {"action": "COMMUNICATE", "target": target, "content": msg,
                "reasoning": "propose collude/hold"}

    return {"action": "WAIT", "reasoning": "holding for better price"}
