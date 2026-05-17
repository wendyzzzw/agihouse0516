"""Simulation engine — round-based scheduler driving per-agent ReAct loops.

Each ROUND:
  1. round_start event
  2. sellers reprice
  3. every not-yet-done buyer runs a full ReAct turn (react.run_react_turn):
       drain inbox -> absorb messages -> observe/act/observe until DONE-verified,
       WAIT, max_steps, or the shared call budget runs out
  4. prices snapshot; unfinished buyers age one round of patience
Stops when all buyers verified, the call budget is spent, or `rounds` is reached.

Inter-agent messages go through the file-based Mailbox. The MarketEnv adapter
is what react.py talks to; it also performs the synchronous peer reply when an
agent sends a COMMUNICATE mid-turn.

Backward compatibility: run() still returns the legacy result shape (topology,
seed, ticks, summary, prices_over_time, events, agents, comm_matrix) so cached
runs and the existing validator keep working; new fields (rounds, llm_mode,
goal/goal_status, sellers) are added alongside.
"""
from __future__ import annotations

import json
import os
import random
import shutil
from typing import Dict, List, Any, Optional

from market import Seller
from personas import make_persona
from config import Scenario, load_named, build_comm_matrix
from mailbox import Mailbox
from react import run_react_turn, CallBudget
from brains import rule_brain, rule_reply, claude_brain, claude_reply, absorb_messages

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DIR = os.path.join(REPO_ROOT, "runs", "live")

MAX_STEPS_PER_TURN = 5
RULE_CALL_BUDGET = 1_000_000      # effectively unlimited (rule brain is instant)
CLAUDE_CALL_BUDGET = 600          # caps total `claude -p` invocations per run


class MarketEnv:
    """The react.py environment contract, backed by an Engine."""

    def __init__(self, engine: "Engine"):
        self.engine = engine

    def observe(self, agent: dict) -> dict:
        return self.engine.world_view(agent["id"])

    def execute(self, agent: dict, action: dict) -> dict:
        return self.engine.execute(agent, action)

    def verify_goal(self, agent: dict) -> bool:
        # The engine is the authority: the goal ("secure a seat") is met iff the
        # agent actually holds a purchase. A self-reported DONE is checked here.
        return bool(agent.get("bought"))

    def emit(self, kind: str, agent_id: str, **data) -> None:
        self.engine.emit(kind, frm=agent_id, **data)


class Engine:
    def __init__(self, scenario, seed: Optional[int] = None, ticks: Optional[int] = None,
                 llm_mode: str = "rule", model: str = "haiku",
                 run_dir: Optional[str] = None):
        # Accept either a Scenario or a topology/config name (back-compat).
        if isinstance(scenario, Scenario):
            self.scenario = scenario
        else:
            self.scenario = load_named(str(scenario))
        if seed is not None:
            self.scenario.seed = seed
        if ticks is not None:
            self.scenario.rounds = ticks

        self.topology = self.scenario.buyer_buyer
        self.seed = self.scenario.seed
        self.rounds = self.scenario.rounds
        self.booking_opens = self.scenario.booking_opens_round
        self.llm_mode = llm_mode
        self.model = model
        self.rng = random.Random(self.seed)

        # Sellers from config.
        self.sellers: Dict[str, Seller] = {
            s.id: Seller(s.id, s.id, inventory=s.inventory, base_price=s.base_price)
            for s in self.scenario.sellers
        }
        self.seller_ids = list(self.sellers.keys())

        # Buyers from config — persona, goal, tools, budget all config-driven.
        self.buyer_ids = self.scenario.buyer_ids
        self.buyers: Dict[str, dict] = {}
        for cfg in self.scenario.agents:
            budget = cfg.budget if cfg.budget is not None else 280 + self.rng.randint(0, 120)
            self.buyers[cfg.id] = {
                "id": cfg.id,
                "type": "buyer",
                "persona": make_persona(cfg.persona),
                "goal": cfg.goal,
                "goal_status": "pending",
                "tools": list(cfg.tools),
                "budget": budget,
                "bought": False,
                "purchase_price": None,
                "purchase_seller": None,
                "satisfaction": None,
                "ticks_waited": 0,
                "beliefs": {},
                "inbox": [],
            }

        self.matrix = build_comm_matrix(self.scenario)

        # Run directory: mailbox files + the live events.jsonl stream.
        self.run_dir = run_dir or os.path.join(LIVE_DIR, f"{self.scenario.name}-{self.seed}")
        if os.path.isdir(self.run_dir):
            shutil.rmtree(self.run_dir)
        os.makedirs(self.run_dir, exist_ok=True)
        self.mailbox = Mailbox(self.run_dir, self.matrix, self.buyer_ids)

        self.events: List[Dict[str, Any]] = []
        self.prices_over_time: List[Dict[str, Any]] = []
        self.current_round = 0
        self._events_path = os.path.join(self.run_dir, "events.jsonl")
        self._events_fp = open(self._events_path, "w")
        self._budget = CallBudget(
            limit=RULE_CALL_BUDGET if llm_mode == "rule" else CLAUDE_CALL_BUDGET)

        self._brain = claude_brain if llm_mode == "claude" else rule_brain
        self._reply = claude_reply if llm_mode == "claude" else rule_reply

    # ---------- events ----------

    def emit(self, kind: str, frm: str = "", **data) -> None:
        """Append a lifecycle/activity event. Visual events also carry the legacy
        `turn`/`from`/`cls` fields so the existing frontend keeps rendering."""
        ev = {"round": self.current_round, "turn": self.current_round, "kind": kind,
              "from": frm, **data}
        self.events.append(ev)
        self._events_fp.write(json.dumps(ev) + "\n")
        self._events_fp.flush()

    # ---------- agent view ----------

    def neighbors_of(self, agent_id: str) -> List[str]:
        row = self.matrix.get(agent_id, {})
        return sorted(n for n in row if row[n] and n in self.buyers)

    def world_view(self, agent_id: str) -> dict:
        return {
            "sellers": {sid: {"price": s.posted_price(), "inventory": s.inventory}
                        for sid, s in self.sellers.items()},
            "neighbors": self.neighbors_of(agent_id),
            "rounds_remaining": self.rounds - self.current_round + 1,
            "booking_open": self.current_round >= self.booking_opens,
            "booking_opens_round": self.booking_opens,
        }

    # ---------- action execution ----------

    def execute(self, agent: dict, action: dict) -> dict:
        atype = str(action.get("action", "WAIT")).upper()

        if atype == "BUY":
            # Negotiation window: BUY is closed until the booking round opens.
            if self.current_round < self.booking_opens:
                self.emit("buy_blocked", frm=agent["id"], to=action.get("target"),
                          cls="log-lie",
                          msg=f"BUY rejected — booking window opens round {self.booking_opens}")
                return {"ok": False,
                        "note": (f"BUY is not open yet — the booking window opens in "
                                 f"round {self.booking_opens}. Until then, observe prices "
                                 f"and message your neighbours.")}
            target = action.get("target")
            seller = self.sellers.get(target)
            if seller is None:
                self.emit("buy_failed", frm=agent["id"], to=target, cls="log-lie",
                          msg=f"tried to buy from unknown seller '{target}'")
                return {"ok": False, "note": f"unknown seller {target}"}
            if seller.inventory <= 0 or not seller.attempt_buy():
                self.emit("buy_failed", frm=agent["id"], to=target, cls="log-trade",
                          msg=f"failed buy from {target} (sold out)")
                return {"ok": False, "note": f"{target} sold out"}
            price = seller.posted_price()
            agent["bought"] = True
            agent["purchase_price"] = price
            agent["purchase_seller"] = target
            under = (agent["budget"] - price) / max(agent["budget"], 1)
            agent["satisfaction"] = max(0, min(100, int(round(50 + 100 * max(0.0, under)))))
            agent["beliefs"][target] = {"last_price": price, "source": "self"}
            self.emit("buy", frm=agent["id"], to=None, cls="log-buy", letter=agent["id"],
                      price=price, sat=agent["satisfaction"],
                      msg=f"BOUGHT from {target} at ${price} "
                          f"({agent['persona']['profile']}, {agent['satisfaction']}% sat)")
            return {"ok": True, "price": price}

        if atype == "COMMUNICATE":
            to = action.get("target")
            content = (action.get("content") or "").strip()
            if not to or not content:
                return {"ok": False, "note": "empty message"}
            msg = self.mailbox.send(agent["id"], to, content, round_no=self.current_round)
            if msg is None:
                self.emit("blocked", frm=agent["id"], to=to, cls="log-lie",
                          msg=f"blocked: no edge to {to}")
                return {"ok": False, "note": f"no comm edge to {to}"}
            cls = "log-probe" if "?" in content else "log-trade"
            self.emit("message", frm=agent["id"], to=to, cls=cls,
                      msg=content if len(content) <= 100 else content[:97] + "...")
            # synchronous reply — the peer hears the message and answers at once.
            peer = self.buyers.get(to)
            reply_text = None
            if peer is not None:
                absorb_messages(peer, [msg])
                reply_text = self._reply(peer, msg, self.world_view(to))
                if reply_text:
                    rmsg = self.mailbox.send(to, agent["id"], reply_text,
                                             round_no=self.current_round)
                    if rmsg is not None:
                        rcls = "log-probe" if "?" in reply_text else "log-trade"
                        self.emit("message_reply", frm=to, to=agent["id"], cls=rcls,
                                  msg=reply_text if len(reply_text) <= 100
                                  else reply_text[:97] + "...")
            return {"ok": True, "sent": content, "reply": reply_text}

        return {"ok": True}    # WAIT / unknown — no-op

    # ---------- main loop ----------

    def run(self) -> dict:
        env = MarketEnv(self)
        # First event carries the graph + roster so a live stream can build the
        # visualization before round events arrive (replay synthesizes its own).
        self.emit("sim_init", frm="", topology=self.topology, rounds=self.rounds,
                  comm_matrix=self.matrix,
                  agents={bid: self._agent_snapshot(b) for bid, b in self.buyers.items()})
        for r in range(1, self.rounds + 1):
            self.current_round = r
            self.emit("round_start", frm="", round=r)

            for s in self.sellers.values():
                s.update_price(r, self.rounds)

            order = [b for b in self.buyer_ids if not self.buyers[b]["bought"]]
            self.rng.shuffle(order)
            for bid in order:
                agent = self.buyers[bid]
                if agent["bought"]:
                    continue
                # drain inbox -> hear messages before deciding
                incoming = self.mailbox.drain_inbox(bid)
                if incoming:
                    absorb_messages(agent, incoming)
                    agent["inbox"] = incoming
                run_react_turn(agent, env, self._brain,
                               max_steps=MAX_STEPS_PER_TURN, budget=self._budget,
                               round_no=r)
                self.mailbox.write_state(bid, self._agent_snapshot(agent))

            snap = self._price_snapshot(r)
            self.prices_over_time.append(snap)
            # also emit as an event so live/replay streams can drive the chart
            self.emit("price", frm="", a=snap["a"], b=snap["b"],
                      inv_a=snap["inv_a"], inv_b=snap["inv_b"])

            for bid in self.buyer_ids:
                if not self.buyers[bid]["bought"]:
                    self.buyers[bid]["ticks_waited"] += 1

            if all(b["bought"] for b in self.buyers.values()) or self._budget.exhausted:
                break

        # Final marker so a live event stream knows the run has finished.
        self.emit("run_complete", frm="", rounds_run=self.current_round,
                  n_verified=sum(1 for b in self.buyers.values()
                                 if b["goal_status"] == "verified"))
        self._events_fp.close()
        return self.snapshot()

    # ---------- snapshots ----------

    def _price_snapshot(self, r: int) -> dict:
        sids = self.seller_ids
        a = self.sellers[sids[0]] if sids else None
        b = self.sellers[sids[1]] if len(sids) > 1 else None
        return {
            "turn": r,
            "a": a.posted_price() if a else 0,
            "b": b.posted_price() if b else 0,
            "inv_a": a.inventory if a else 0,
            "inv_b": b.inventory if b else 0,
        }

    def _agent_snapshot(self, b: dict) -> dict:
        return {
            "id": b["id"], "profile": b["persona"]["profile"], "budget": b["budget"],
            "goal": b["goal"], "goal_status": b["goal_status"], "bought": b["bought"],
            "purchase_price": b["purchase_price"], "purchase_seller": b["purchase_seller"],
            "satisfaction": b["satisfaction"], "ticks_waited": b["ticks_waited"],
            "tools": b["tools"], "hold_target": b["beliefs"].get("hold_target"),
        }

    def snapshot(self) -> dict:
        bought = [b for b in self.buyers.values() if b["bought"]]
        n_bought = len(bought)
        avg_price = round(sum(b["purchase_price"] for b in bought) / n_bought, 1) if bought else 0
        avg_sat = round(sum(b["satisfaction"] for b in bought) / n_bought, 1) if bought else 0
        by_profile: Dict[str, Any] = {}
        for prof in ["budget", "family", "investor", "flexible"]:
            ps = [b for b in bought if b["persona"]["profile"] == prof]
            by_profile[prof] = (round(sum(b["purchase_price"] for b in ps) / len(ps), 1)
                                if ps else None)
        total_msgs = sum(1 for e in self.events if e["kind"] in ("message", "message_reply"))
        n_verified = sum(1 for b in self.buyers.values() if b["goal_status"] == "verified")

        return {
            "topology": self.topology,
            "scenario": self.scenario.name,
            "seed": self.seed,
            "ticks": self.rounds,           # legacy alias
            "rounds": self.rounds,
            "llm_mode": self.llm_mode,
            "run_dir": self.run_dir,
            "summary": {
                "avg_price": avg_price,
                "by_profile": by_profile,
                "n_bought": n_bought,
                "n_missed": len(self.buyer_ids) - n_bought,
                "n_goal_verified": n_verified,
                "avg_satisfaction": avg_sat,
                "total_messages": total_msgs,
            },
            "prices_over_time": self.prices_over_time,
            "events": self.events,
            "agents": {bid: self._agent_snapshot(b) for bid, b in self.buyers.items()},
            "sellers": {
                sid: {"name": s.name, "base_price": s.base_price,
                      "final_price": s.posted_price(),
                      "initial_inventory": s.initial_inventory,
                      "final_inventory": s.inventory}
                for sid, s in self.sellers.items()
            },
            "comm_matrix": self.matrix,
        }
