"""Unit tests for the ReAct turn loop. No LLM, no real engine.

A scripted FakeBrain replays a fixed action list; a FakeEnv records events and
fakes execution + goal verification. This pins the loop's control flow:
  - multi-step looping
  - stop only on engine-VERIFIED DONE
  - rejected DONE is fed back and the loop continues
  - max_steps cap
  - shared call budget cap
  - the synchronous reply from a peer reaches the next step's scratchpad

Run:  python3 test_react.py        (exit 0 = pass)
"""
from __future__ import annotations

import sys

from react import run_react_turn, CallBudget

_failures = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK    {label}")
    else:
        print(f"  FAIL  {label}")
        _failures.append(label)


class FakeBrain:
    """Replays a scripted list of actions; records the history it was handed."""
    def __init__(self, actions):
        self.actions = list(actions)
        self.seen_histories = []

    def __call__(self, agent, world, history):
        self.seen_histories.append([h for h in history])
        if self.actions:
            return self.actions.pop(0)
        return {"action": "WAIT"}


class FakeEnv:
    """Fakes the engine. `bought` flips when a BUY executes; verify_goal is True
    iff the agent has bought. COMMUNICATE returns a canned synchronous reply."""
    def __init__(self):
        self.events = []

    def observe(self, agent):
        return {"sellers": {"S1": {"price": 100, "inventory": 5}}, "neighbors": ["B"]}

    def execute(self, agent, action):
        atype = str(action.get("action", "WAIT")).upper()
        if atype == "BUY":
            agent["bought"] = True
            return {"ok": True, "price": 100}
        if atype == "COMMUNICATE":
            return {"ok": True, "reply": "I saw $90 on S1"}
        return {"ok": True}

    def verify_goal(self, agent):
        return bool(agent.get("bought"))

    def emit(self, kind, agent_id, **data):
        self.events.append({"kind": kind, "agent": agent_id, **data})

    def kinds(self):
        return [e["kind"] for e in self.events]


def new_agent():
    return {"id": "A", "goal": "buy a seat", "goal_status": "pending", "bought": False}


def main() -> int:
    # --- Test 1: multi-step loop, stops on engine-verified DONE ---
    env = FakeEnv()
    brain = FakeBrain([
        {"action": "COMMUNICATE", "target": "B"},
        {"action": "COMMUNICATE", "target": "B"},
        {"action": "BUY", "target": "S1"},
        {"action": "DONE"},
    ])
    r = run_react_turn(new_agent(), env, brain, max_steps=8)
    check(r.done and r.stop_reason == "verified", "loop stops on engine-verified DONE")
    check(r.steps == 4, "loop ran exactly 4 steps (3 actions + DONE)")
    check(env.kinds().count("agent_thinking") == 4, "emitted agent_thinking once per step")
    check("goal_reached" in env.kinds(), "emitted goal_reached")
    check("agent_activated" == env.kinds()[0], "emitted agent_activated first")

    # --- Test 2: DONE before the goal is met is rejected, loop continues ---
    env = FakeEnv()
    agent = new_agent()
    brain = FakeBrain([
        {"action": "DONE"},                       # premature — bought is False
        {"action": "BUY", "target": "S1"},
        {"action": "DONE"},                       # now legitimate
    ])
    r = run_react_turn(agent, env, brain, max_steps=8)
    check(r.done and r.steps == 3, "premature DONE rejected; loop continued to a real DONE")
    rej = r.history[0]["result"]
    check(rej["ok"] is False and "rejected" in rej["note"].lower(),
          "rejected DONE recorded in scratchpad with a rejection note")
    check("done_rejected" in env.kinds(), "emitted done_rejected for the premature DONE")
    check(agent["goal_status"] == "verified", "agent goal_status ends 'verified'")

    # --- Test 3: max_steps cap when the agent keeps acting but never finishes ---
    env = FakeEnv()
    agent = new_agent()
    brain = FakeBrain([{"action": "COMMUNICATE", "target": "B"}] * 20)
    r = run_react_turn(agent, env, brain, max_steps=5)
    check(not r.done and r.steps == 5 and r.stop_reason == "max_steps",
          "loop stops at max_steps when goal never met")
    check(agent["goal_status"] == "pending", "unfinished agent stays 'pending'")
    check("agent_idle" in env.kinds(), "emitted agent_idle on unfinished turn")

    # --- Test 3b: WAIT ends the turn immediately (yield to next round) ---
    env = FakeEnv()
    agent = new_agent()
    brain = FakeBrain([{"action": "WAIT"}, {"action": "BUY", "target": "S1"}])
    r = run_react_turn(agent, env, brain, max_steps=8)
    check(not r.done and r.steps == 1 and r.stop_reason == "wait",
          "WAIT ends the turn after one step (does not spin)")

    # --- Test 4: shared call budget caps total brain calls ---
    env = FakeEnv()
    budget = CallBudget(limit=3)
    brain = FakeBrain([{"action": "COMMUNICATE", "target": "B"}] * 20)
    r = run_react_turn(new_agent(), env, brain, max_steps=10, budget=budget)
    check(r.steps == 3 and r.stop_reason == "budget", "loop stops when call budget is exhausted")
    check(budget.used == 3, "budget recorded exactly 3 calls used")

    # --- Test 5: synchronous reply reaches the NEXT step's scratchpad ---
    env = FakeEnv()
    brain = FakeBrain([
        {"action": "COMMUNICATE", "target": "B"},
        {"action": "BUY", "target": "S1"},
        {"action": "DONE"},
    ])
    run_react_turn(new_agent(), env, brain, max_steps=8)
    # brain was called 3x; the 2nd call's history must contain the reply from step 1
    second_call_history = brain.seen_histories[1]
    got_reply = (second_call_history
                 and second_call_history[0]["result"].get("reply") == "I saw $90 on S1")
    check(bool(got_reply), "peer's synchronous reply is visible in the next step's history")

    print()
    if _failures:
        print(f"react: {len(_failures)} FAILURE(S)")
        return 1
    print("react: ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
