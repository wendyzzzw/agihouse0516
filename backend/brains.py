"""Decision brains for the ReAct loop.

A "brain" is `brain(agent, world_view, history) -> action dict`. Two backends:

  rule_brain    — deterministic; the offline default. Crucially it ACTS on what
                  it hears: a buyer that received a "hold" proposal respects a
                  lower price ceiling. That is what makes topology matter — in
                  `isolated` no proposals propagate, so nobody holds.
  claude_brain  — one direct Anthropic SDK call per step (goal + scratchpad in
                  the prompt), with structured JSON output matching the action
                  schema. ~1-3s per call vs ~15-20s for the old `claude -p`
                  subprocess. Falls back to rule_brain if the API is unreachable
                  (e.g. ANTHROPIC_API_KEY unset).

Plus the synchronous-reply brains used when one agent messages another:
  rule_reply / claude_reply — produce a short reply the peer sends straight back.

`absorb_messages` is the shared "hearing" step: it scans incoming messages for
hold proposals and updates the agent's beliefs. The engine calls it when an
agent drains its inbox and when a peer receives a message it is about to reply
to. It is idempotent.
"""
from __future__ import annotations

import json
import re
from typing import Optional, List, Dict, Any

# Per-profile knobs — the calibration surface for the topology price spread.
#   accept_pct : buy when cheapest price <= budget * accept_pct
#   max_wait   : rounds of patience before a forced buy
#   joins_hold : will this persona honor a peer's hold proposal?
#   proposes   : will this persona originate a hold proposal?
PROFILE_KNOBS = {
    "budget":   {"accept_pct": 0.85, "max_wait": 30, "joins_hold": True,  "proposes": True},
    "family":   {"accept_pct": 0.98, "max_wait": 10, "joins_hold": False, "proposes": False},
    "investor": {"accept_pct": 0.80, "max_wait": 25, "joins_hold": True,  "proposes": True},
    "flexible": {"accept_pct": 0.82, "max_wait": 40, "joins_hold": True,  "proposes": True},
}
_DEFAULT_KNOBS = {"accept_pct": 0.90, "max_wait": 20, "joins_hold": True, "proposes": False}

_HOLD_RE = re.compile(r"\$(\d+)")


def knobs_for(agent: dict) -> dict:
    return PROFILE_KNOBS.get(agent["persona"]["profile"], _DEFAULT_KNOBS)


# ---------------------------------------------------------------- absorb ----

def absorb_messages(agent: dict, messages: List[dict]) -> None:
    """The agent 'hears' incoming messages. A hold proposal lowers the agent's
    price ceiling (beliefs['hold_target']) if its persona joins holds. Idempotent."""
    k = knobs_for(agent)
    if not k["joins_hold"]:
        return
    for m in messages:
        content = (m.get("content") or "").lower()
        if "hold" not in content:
            continue
        nums = _HOLD_RE.findall(content)
        if not nums:
            continue
        proposed = int(nums[0])
        current = agent["beliefs"].get("hold_target")
        # adopt the proposal; keep the lowest ceiling heard so far
        if current is None or proposed < current:
            agent["beliefs"]["hold_target"] = proposed
            agent["beliefs"]["hold_source"] = m.get("sender")


# ------------------------------------------------------------ rule brain ----

def _cheapest(world: dict):
    avail = [(sid, info) for sid, info in world["sellers"].items() if info["inventory"] > 0]
    if not avail:
        return None, None
    return min(avail, key=lambda x: x[1]["price"])


def rule_brain(agent: dict, world: dict, history: list) -> dict:
    """Deterministic ReAct step. Returns one Action dict."""
    if agent.get("bought"):
        return {"action": "DONE", "reasoning": "seat secured — goal met"}

    cid, cheapest = _cheapest(world)
    if cheapest is None:
        return {"action": "WAIT", "reasoning": "no inventory available this round"}

    k = knobs_for(agent)
    budget = agent["budget"]
    waited = agent.get("ticks_waited", 0)
    remaining = world.get("rounds_remaining", 10)
    hold_target = agent["beliefs"].get("hold_target")
    booking_open = world.get("booking_open", True)   # negotiation window?

    # Effective price ceiling: persona's deal price, lowered by any hold joined.
    deal_price = budget * k["accept_pct"]
    ceiling = deal_price if hold_target is None else min(deal_price, hold_target)

    # BUY branches only fire once the booking window is open.
    if booking_open and cheapest["price"] <= ceiling:
        return {"action": "BUY", "target": cid,
                "reasoning": f"{cid} at ${cheapest['price']} <= ceiling ${int(ceiling)}"}

    # Last-chance and patience-exhausted buys ignore the hold (don't miss the seat).
    if booking_open and remaining <= 2 and cheapest["price"] <= budget:
        return {"action": "BUY", "target": cid, "reasoning": "last-chance grab before window closes"}
    if booking_open and waited > k["max_wait"] and cheapest["price"] <= budget:
        return {"action": "BUY", "target": cid, "reasoning": "patience exhausted"}

    # Social step: probe a neighbor or propose a hold — at most once per turn.
    already_talked = any(
        str(h["action"].get("action", "")).upper() == "COMMUNICATE" for h in history
    )
    neighbors = world.get("neighbors") or []
    traits = agent["persona"].get("traits", {})
    social = traits.get("social", 0.3)
    patience = traits.get("patience", 0.5)
    if neighbors and not already_talked and social >= 0.4:
        target = sorted(neighbors)[waited % len(neighbors)]   # deterministic pick
        if k["proposes"] and patience >= 0.6 and hold_target is None:
            hold = int(budget * 0.72)
            return {"action": "COMMUNICATE", "target": target,
                    "content": f"Let's all hold out — don't buy above ${hold}. "
                               f"Forces the sellers to drop their price.",
                    "reasoning": "propose a coordinated hold to neighbors"}
        return {"action": "COMMUNICATE", "target": target,
                "content": f"What price are you seeing? I'm seeing ${cheapest['price']} on {cid}.",
                "reasoning": "probe a neighbor for price signal"}

    return {"action": "WAIT", "reasoning": "holding for a better price"}


def rule_reply(peer: dict, incoming: dict, world: dict) -> Optional[str]:
    """Deterministic synchronous reply. The peer has already absorbed `incoming`."""
    content = (incoming.get("content") or "").lower()
    if peer.get("bought"):
        return f"Already bought — paid ${peer.get('purchase_price')}."
    if "hold" in content:
        ht = peer["beliefs"].get("hold_target")
        if ht is not None:
            return f"Count me in — I'll hold below ${ht}."
        return "I can't wait that long, I need a seat soon. Good luck though."
    # treat anything else as a price probe
    cid, cheapest = _cheapest(world)
    if cheapest is None:
        return "Everything looks sold out from where I sit."
    return f"I'm seeing ${cheapest['price']} on {cid} right now."


# ----------------------------------------------------------- claude brain ----

# Structured-output schema for one agent action. All four properties are listed
# in `required` (with target/content nullable via anyOf) — the form the API's
# structured outputs accepts most reliably.
_REACT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "COMMUNICATE", "WAIT", "DONE"]},
        "target": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "content": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "target", "content", "reasoning"],
    "additionalProperties": False,
}

_REACT_SYSTEM = """You are a buyer agent in a multi-agent flight-booking market.

You run a ReAct loop: observe -> act -> observe the result -> act again, until
your goal is met. Each step you output ONE action:
  BUY        — purchase a seat. target = seller id.
  COMMUNICATE— send one message to one neighbor. target = neighbor id, content = text.
  WAIT       — yield the rest of this round (prices only change between rounds).
  DONE       — claim your goal is met. The engine VERIFIES this; a false DONE is
               rejected and you must keep going.

MARKET DYNAMICS — read carefully:
- Prices are HIGHEST in the first rounds and FALL over the booking window as
  sellers compete and discount. Buying in round 1 almost always overpays.
- Check the seats-vs-buyers numbers. If seats are plentiful, you are NOT under
  time pressure — wait for the price to drop.
- Connected buyers who agree to a "hold" (nobody buys above $X) push sellers to
  cut prices faster. Probe your neighbours, propose a hold, THEN buy.

Notes:
- You can only message neighbors listed in your context.
- WAIT and COMMUNICATE are normal early moves — don't rush to BUY.
- Only buy early if the price is already a genuine bargain or seats are scarce.
Output STRICT JSON matching the schema."""

# Direct Anthropic SDK — one call per agent step, ~1-3s vs ~15-20s for the old
# `claude -p` subprocess. The client is created lazily and cached; if it cannot
# be created (e.g. ANTHROPIC_API_KEY unset) the brains fall back to rule mode.
_MODEL_IDS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

_client = None
_client_unavailable = False


def _get_client():
    """Lazily build and cache the Anthropic client. Returns None if unavailable."""
    global _client, _client_unavailable
    if _client is not None or _client_unavailable:
        return _client
    try:
        import anthropic
        _client = anthropic.Anthropic()        # reads ANTHROPIC_API_KEY from env
    except Exception:
        _client_unavailable = True
    return _client


def _resolve_model(model: str) -> str:
    """Map a short name (haiku/sonnet/opus) to a full model id; pass ids through."""
    return _MODEL_IDS.get(model, model)


def _scratchpad(history: list) -> str:
    if not history:
        return "(this is your first step this round)"
    lines = []
    for h in history[-6:]:
        a = h["action"]
        r = h.get("result", {})
        desc = a.get("action", "?")
        if a.get("target"):
            desc += f" -> {a['target']}"
        note = r.get("note") or r.get("reply") or ("ok" if r.get("ok") else "failed")
        lines.append(f"  step {h['step']}: {desc} | result: {note}")
    return "\n".join(lines)


def _build_react_prompt(agent: dict, world: dict, history: list) -> str:
    sellers = "\n".join(
        f"  - {sid}: ${i['price']} ({i['inventory']} seats)" if i["inventory"] > 0
        else f"  - {sid}: SOLD OUT"
        for sid, i in world["sellers"].items()
    ) or "  (none)"
    neighbors = ", ".join(world.get("neighbors") or []) or "(none — you are isolated)"
    persona = agent["persona"]
    if world.get("booking_open", True):
        booking = "BUY is OPEN — you may purchase a seat now."
    else:
        booking = (f"BUY is CLOSED until round {world.get('booking_opens_round', 1)} — "
                   f"this is the negotiation window. Your ONLY useful move is "
                   f"COMMUNICATE: message a neighbour to probe their price or propose "
                   f"a coordinated hold (\"nobody buys above $X\"). Pick WAIT only if "
                   f"you have already messaged everyone you can.")
    return f"""YOU ARE AGENT {agent['id']}.

YOUR GOAL: {agent.get('goal', 'secure a seat at a good price')}

PERSONA: {persona['profile']} — {persona['description']}
  traits: {persona['traits']}
STATE: budget=${agent['budget']}  rounds_remaining={world.get('rounds_remaining')}
  rounds_waited={agent.get('ticks_waited', 0)}

BOOKING: {booking}

SELLERS:
{sellers}

NEIGHBORS you may message: {neighbors}

YOUR STEPS SO FAR THIS ROUND:
{_scratchpad(history)}

Pick ONE action. Respond with strict JSON only."""


def claude_brain(agent: dict, world: dict, history: list,
                 model: str = "haiku", timeout: int = 30) -> dict:
    """One direct Anthropic API ReAct step with structured JSON output.

    Thinking is intentionally OFF (no `thinking` param) — a single-step agent
    decision doesn't need it, and omitting it is the fastest path. Falls back to
    rule_brain on any failure (including a missing API key)."""
    if agent.get("bought"):
        return {"action": "DONE", "reasoning": "seat secured"}
    client = _get_client()
    if client is None:
        return rule_brain(agent, world, history)

    # During the negotiation window BUY is illegal — drop it from the action
    # enum so the model literally cannot emit it and must observe/communicate.
    schema = _REACT_SCHEMA
    if not world.get("booking_open", True):
        props = dict(_REACT_SCHEMA["properties"])
        props["action"] = {"type": "string", "enum": ["COMMUNICATE", "WAIT"]}
        schema = dict(_REACT_SCHEMA, properties=props)

    try:
        resp = client.with_options(timeout=timeout).messages.create(
            model=_resolve_model(model),
            max_tokens=1024,
            # Static system prompt — cache_control future-proofs a longer prompt
            # (today it's below the cache minimum, so it's a harmless no-op).
            system=[{"type": "text", "text": _REACT_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": _build_react_prompt(agent, world, history)}],
            # Structured outputs — the response is constrained to the schema.
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text:
            action = json.loads(text)
            if isinstance(action, dict) and "action" in action:
                return action
    except Exception:
        pass
    return rule_brain(agent, world, history)


def claude_reply(peer: dict, incoming: dict, world: dict,
                 model: str = "haiku", timeout: int = 20) -> Optional[str]:
    """One direct Anthropic API synchronous reply. Falls back to rule_reply."""
    client = _get_client()
    if client is None:
        return rule_reply(peer, incoming, world)
    prompt = (f"You are agent {peer['id']} ({peer['persona']['profile']}). "
              f"A neighbor sent you: \"{incoming.get('content')}\". "
              f"Reply in ONE short sentence (plain text, no JSON).")
    try:
        resp = client.with_options(timeout=timeout).messages.create(
            model=_resolve_model(model),
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        if text.strip():
            return text.strip()[:160]
    except Exception:
        pass
    return rule_reply(peer, incoming, world)
