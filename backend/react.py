"""ReAct turn loop — one agent reasons and acts until its goal is verified.

A "turn" is: observe -> think (brain) -> act (env) -> repeat. The loop ends when

  * the agent emits DONE and the engine VERIFIES the goal against real state, or
  * the per-turn step cap is hit, or
  * the shared call budget for the whole run is exhausted.

This module is deliberately free of any LLM or engine internals. It depends only
on two injected collaborators, so it can be exercised end-to-end with fakes:

  brain(agent, world_view, history) -> action dict
      The decision maker. `history` is the agent's scratchpad — a list of
      {step, action, result} from earlier steps THIS turn, including any reply
      received from a peer. Real brains: rule-based or `claude -p` (agent_runtime).

  env  — an object exposing:
      observe(agent)        -> world_view dict
      execute(agent, action)-> result dict   (performs BUY / COMMUNICATE etc.)
      verify_goal(agent)    -> bool          (engine's authoritative goal check)
      emit(kind, agent_id, **data)           (lifecycle events for the frontend)

The DONE action is "LLM proposes, engine verifies": the agent only *claims*
completion; `env.verify_goal` is the authority. A rejected DONE is fed back into
the scratchpad so the agent can see it didn't actually finish.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Callable


@dataclass
class CallBudget:
    """Shared across a whole run — caps total brain invocations so a swarm of
    looping agents can't spin forever or burn unbounded tokens."""
    limit: int = 600
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self) -> None:
        self.used += 1

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


@dataclass
class TurnResult:
    agent_id: str
    done: bool                       # True iff goal was verified this turn
    steps: int                       # brain calls made this turn
    stop_reason: str                 # verified | max_steps | budget
    history: List[Dict[str, Any]] = field(default_factory=list)


def run_react_turn(
    agent: dict,
    env: Any,
    brain: Callable[[dict, dict, list], dict],
    *,
    max_steps: int = 6,
    budget: CallBudget | None = None,
    round_no: int = 0,
) -> TurnResult:
    """Run one agent's ReAct turn. Mutates `agent` (sets goal_status); returns a
    TurnResult. Emits lifecycle events via env.emit for the frontend."""
    agent_id = agent["id"]
    history: List[Dict[str, Any]] = []
    env.emit("agent_activated", agent_id, round=round_no, goal=agent.get("goal", ""))

    stop_reason = "max_steps"
    step = 0
    while step < max_steps:
        if budget is not None and budget.exhausted:
            stop_reason = "budget"
            break
        step += 1
        if budget is not None:
            budget.consume()

        world = env.observe(agent)
        env.emit("agent_thinking", agent_id, round=round_no, step=step)
        action = brain(agent, world, history) or {"action": "WAIT"}
        atype = str(action.get("action", "WAIT")).upper()

        if atype == "DONE":
            if env.verify_goal(agent):
                agent["goal_status"] = "verified"
                history.append({"step": step, "action": action,
                                 "result": {"ok": True, "note": "goal verified"}})
                env.emit("goal_reached", agent_id, round=round_no, step=step)
                return TurnResult(agent_id, True, step, "verified", history)
            # rejected — feed the rejection back so the agent sees it isn't done
            agent["goal_status"] = "pending"
            note = "DONE rejected: the engine could not verify your goal — you have not met it yet."
            history.append({"step": step, "action": action,
                             "result": {"ok": False, "note": note}})
            env.emit("done_rejected", agent_id, round=round_no, step=step)
            continue

        if atype == "WAIT":
            # Nothing changes until the round advances, so WAIT ends the turn —
            # the agent yields and will be re-activated next round.
            history.append({"step": step, "action": action, "result": {"ok": True}})
            env.emit("agent_idle", agent_id, round=round_no, steps=step, reason="wait")
            return TurnResult(agent_id, False, step, "wait", history)

        result = env.execute(agent, action)
        history.append({"step": step, "action": action, "result": result})

    if budget is not None and budget.exhausted and stop_reason != "budget":
        stop_reason = "budget"
    env.emit("agent_idle", agent_id, round=round_no, steps=step, reason=stop_reason)
    return TurnResult(agent_id, False, step, stop_reason, history)
