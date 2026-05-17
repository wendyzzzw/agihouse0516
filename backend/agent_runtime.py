"""Agent decision layer.

Two backends:
  - "claude":  spawn `claude -p --system-prompt --json-schema ...` per agent per tick
  - "rule":    deterministic rule-based fallback (also the offline default)

Both return the same Action dict: {"action", "target", "content", "reasoning"}.
"""
from __future__ import annotations
import json
import subprocess
from typing import Dict, Any, Optional, List


DEFAULT_ACTIONS_BY_ROLE: Dict[str, List[str]] = {
    "buyer": ["BUY", "COMMUNICATE", "PROBE", "SHARE_INFO", "COORDINATE", "WAIT"],
    "seller": ["SET_PRICE", "ACCEPT_OFFER", "COUNTER_OFFER", "REJECT_OFFER", "BROADCAST", "WAIT"],
}


ACTION_DESCRIPTIONS: Dict[str, str] = {
    "BUY": "Buy immediately from a seller at the currently listed price. target must be seller_id.",
    "BID": "Send a concrete price offer. target is seller_id; content should include price and quantity.",
    "ACCEPT_OFFER": "Accept an available offer. target is the counterparty.",
    "COUNTER_OFFER": "Reject the current terms and propose new terms. target is the counterparty.",
    "REJECT_OFFER": "Reject an offer without buying.",
    "COMMUNICATE": "Send one direct message to one reachable contact. target and content are required.",
    "PROBE": "Ask one reachable contact for information. target and content are required.",
    "SHARE_INFO": "Share price, inventory, trust, or strategy information with one reachable contact.",
    "COORDINATE": "Try to coordinate waiting, group buying, or market pressure with one reachable contact.",
    "LIE": "Send a deceptive message if your persona and incentives justify it.",
    "BROADCAST": "Send a market-facing message. In this prototype, pick one reachable target.",
    "SET_PRICE": "Change your listed price. content should include the new price.",
    "UNDERCUT": "Lower price relative to a competitor. content should explain the target price.",
    "BUILD_TOOL": "Spend this turn building or improving an information tool.",
    "FORM_CONNECTION": "Ask for a new relationship or introduction.",
    "WAIT": "Take no external action this turn.",
    "EXIT": "Leave the market and stop trying to transact.",
}


def actions_for(agent_state: dict) -> List[str]:
    """Return the configured per-agent action list, always including WAIT."""
    role = agent_state.get("type", "buyer")
    configured = agent_state.get("actions") or DEFAULT_ACTIONS_BY_ROLE.get(role, DEFAULT_ACTIONS_BY_ROLE["buyer"])
    actions: List[str] = []
    for action in [*configured, "WAIT"]:
        normalized = str(action).upper()
        if normalized not in actions:
            actions.append(normalized)
    return actions


def build_action_json_schema(agent_state: dict) -> Dict[str, Any]:
    """Build a JSON schema whose action enum is specific to this agent."""
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions_for(agent_state)},
            "target": {"type": ["string", "null"]},
            "content": {"type": ["string", "null"]},
            "reasoning": {"type": "string"},
        },
        "required": ["action", "reasoning"],
        "additionalProperties": False,
    }


ACTION_JSON_SCHEMA: Dict[str, Any] = build_action_json_schema({"type": "buyer"})


def _compact_json(value: Any) -> str:
    if value in (None, {}, []):
        return "(none)"
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def build_system_prompt(agent_state: dict, world_view: Optional[dict] = None) -> str:
    """Build stable role/persona/game context for one LLM agent."""
    world_view = world_view or {}
    role = agent_state.get("type", "buyer")
    persona = agent_state.get("persona") or {}
    profile = persona.get("profile") or agent_state.get("archetype") or role
    description = agent_state.get("archetype_description") or persona.get("description") or "(none)"
    traits = persona.get("traits") or {}
    actions = actions_for(agent_state)
    action_block = "\n".join(
        f"- {action}: {ACTION_DESCRIPTIONS.get(action, 'Use this action only if it fits your goal.')}"
        for action in actions
    )

    simulation = world_view.get("simulation") or {}
    market_rules = world_view.get("market_rules") or {}
    topology = world_view.get("topology") or {}

    if role == "seller":
        role_directive = (
            "You are a seller agent. Manage inventory, protect your private floor economics, "
            "use messages strategically, and choose prices or responses that satisfy your goal."
        )
    else:
        role_directive = (
            "You are a buyer agent. Acquire the item only when it advances your goal, use local "
            "information strategically, and account for the risk that inventory may disappear."
        )

    return f"""You are agent {agent_state.get('id')} in AgentArena.

ROLE
{role_directive}

SIMULATION CONTEXT
- Simulation: {simulation.get('id', '(unspecified)')}
- Summary: {simulation.get('summary', '(unspecified)')}
- Max rounds: {simulation.get('max_rounds', '(unspecified)')}
- Market rules: {_compact_json(market_rules)}
- Topology: {_compact_json(topology)}

YOUR IDENTITY
- Role: {role}
- Archetype/profile: {profile}
- Description: {description}
- Traits: {_compact_json(traits)}
- Goal: {_compact_json(agent_state.get('goal') or agent_state.get('goals'))}
- Constraints: {_compact_json(agent_state.get('constraints'))}

LOCAL KNOWLEDGE RULES
- The adjacency matrix is the communication boundary.
- You may message only contacts listed in the user prompt.
- You can read all historical direct messages visible to you with those contacts.
- You cannot read private conversations between other agents unless someone tells you about them.
- Other agents can be honest, mistaken, evasive, or deceptive.
- Treat seller prices, inventory, and private goals as local observations, not global truth, unless the prompt explicitly marks them public.

AVAILABLE ACTIONS
Pick exactly one action from this agent-specific list:
{action_block}

OUTPUT CONTRACT
Return strict JSON matching the schema. Use target when the action addresses another agent. Use content for any message, offer, price change, or explanation visible to another agent. Be decisive and stay in character."""


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
    neighbor_str = ", ".join(neighbors) if neighbors else "(none - you are isolated)"

    local_history = _visible_message_history(agent_state, neighbors)
    history_str = "\n".join(local_history) if local_history else "(none)"

    beliefs = agent_state.get("beliefs") or {}
    beliefs_str = json.dumps(beliefs, ensure_ascii=True, sort_keys=True) if beliefs else "(none yet)"

    persona = agent_state.get("persona") or {
        "profile": agent_state.get("archetype") or agent_state.get("type", "agent"),
        "description": agent_state.get("archetype_description") or "(none)",
        "traits": {},
    }
    budget = agent_state.get("budget", agent_state.get("cash", "n/a"))
    return f"""YOU ARE AGENT {agent_state['id']}.

PERSONA:
  profile     = {persona['profile']}
  description = {persona['description']}
  traits      = {persona['traits']}

YOUR STATE:
  budget               = ${budget}
  ticks_waited         = {agent_state['ticks_waited']}
  ticks_remaining      = {world_view['ticks_remaining']}
  goal                 = {_compact_json(agent_state.get('goal') or agent_state.get('goals'))}
  constraints          = {_compact_json(agent_state.get('constraints'))}

SELLERS (currently posted prices):
{seller_block}

CONTACTS you can message through the adjacency matrix: {neighbor_str}

LOCAL MESSAGE HISTORY (all visible direct messages, oldest first):
{history_str}

YOUR BELIEFS:
{beliefs_str}

YOUR AVAILABLE ACTIONS THIS TURN:
{", ".join(actions_for(agent_state))}

Pick ONE action. Respond with strict JSON only."""


def _visible_message_history(agent_state: dict, neighbors: List[str]) -> List[str]:
    """Return all direct messages visible to this agent through current contacts."""
    agent_id = agent_state.get("id")
    reachable = set(neighbors or [])
    rows = []

    for msg in agent_state.get("inbox", []):
        sender = msg.get("sender")
        if reachable and sender not in reachable:
            continue
        rows.append((
            msg.get("turn", 0),
            msg.get("id", ""),
            f"  - t={msg.get('turn', '?')} {sender} -> {agent_id}: {msg.get('content', '')}",
        ))

    for msg in agent_state.get("outbox", []):
        recipient = msg.get("recipient")
        if reachable and recipient not in reachable:
            continue
        rows.append((
            msg.get("turn", 0),
            msg.get("id", ""),
            f"  - t={msg.get('turn', '?')} {agent_id} -> {recipient}: {msg.get('content', '')}",
        ))

    rows.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[2] for row in rows]


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
    system_prompt = build_system_prompt(agent_state, world_view)
    action_schema = build_action_json_schema(agent_state)
    cmd = [
        "claude", "-p", user_prompt,
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(action_schema),
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
    neighbors = world_view.get("buyer_neighbors")
    if neighbors is None:
        seller_ids = set((world_view.get("sellers") or {}).keys())
        neighbors = [n for n in (world_view.get("neighbors") or []) if n not in seller_ids]
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
