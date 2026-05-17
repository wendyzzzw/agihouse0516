"""FastAPI server — serves the frontend and simulation APIs.

Run:  uvicorn app:app --reload --port 8000
Then open: http://localhost:8000/demo.html
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from analysis_runtime import compare_pairwise, compare_runs, load_or_analyze_run
from engine import Engine
from live_runtime import (
    RunStore,
    ScenarioCompiler,
    create_live_run,
    player_contexts_from_state,
    run_live_background,
    snapshot_from_state,
)
from topology import TOPOLOGIES


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(REPO_ROOT, "runs")
LIVE_RUNS_DIR = os.path.join(RUNS_DIR, "live")
CONFIG_PATH = os.path.join(REPO_ROOT, "test_config.yaml")
LIVE_STORE = RunStore(root=Path(LIVE_RUNS_DIR))

app = FastAPI(title="AgentArena Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class RunRequest(BaseModel):
    topology: str = "small_world"
    seed: int = 42
    ticks: int = 55
    mode: str = Field("rule", description="rule | claude")
    model: str = "haiku"


class LiveRunRequest(BaseModel):
    scenario_id: str = "open_bazaar"
    seed: int = 42
    max_rounds: Optional[int] = None
    llm_provider: str = Field("rule", description="rule | openai | claude")
    model: str = "gpt-4.1-mini"
    speed_ms: int = Field(500, ge=0, le=10000)


@app.get("/api/sim/topologies")
def list_topologies():
    return {"topologies": TOPOLOGIES}


@app.get("/api/scenarios")
def list_scenarios():
    compiler = ScenarioCompiler(Path(CONFIG_PATH))
    return {"scenarios": compiler.list_scenarios()}


@app.post("/api/sim/run")
def run_sim(req: RunRequest):
    if req.topology not in TOPOLOGIES:
        raise HTTPException(400, f"unknown topology: {req.topology}")
    if req.mode not in ("rule", "claude"):
        raise HTTPException(400, f"unknown mode: {req.mode}")
    eng = Engine(req.topology, seed=req.seed, ticks=req.ticks,
                 llm_mode=req.mode, model=req.model)
    return eng.run()


@app.get("/api/sim/cached/{topology}")
def cached_run(topology: str):
    """Return the pre-generated JSON for a topology (no live sim)."""
    if topology not in TOPOLOGIES:
        raise HTTPException(404, f"unknown topology: {topology}")
    path = os.path.join(RUNS_DIR, f"{topology}.json")
    if not os.path.exists(path):
        raise HTTPException(404, f"no cached run; run `python run.py --topology {topology}` first")
    return FileResponse(path, media_type="application/json")


@app.get("/api/sim/compare")
def compare(seeds: int = Query(5, ge=1, le=50), ticks: int = 55):
    """Run each topology N times, return summary stats. The headline 22% data point."""
    out = {}
    for topo in TOPOLOGIES:
        prices, sats, missed = [], [], []
        for seed in range(seeds):
            eng = Engine(topo, seed=seed, ticks=ticks, llm_mode="rule")
            r = eng.run()
            s = r["summary"]
            if s["n_bought"]:
                prices.append(s["avg_price"])
                sats.append(s["avg_satisfaction"])
            missed.append(s["n_missed"])
        out[topo] = {
            "seeds": seeds,
            "mean_price": round(sum(prices) / len(prices), 1) if prices else None,
            "mean_satisfaction": round(sum(sats) / len(sats), 1) if sats else None,
            "mean_missed": round(sum(missed) / len(missed), 1),
        }
    # spread vs best
    best = min((v["mean_price"] for v in out.values() if v["mean_price"]), default=None)
    if best:
        for v in out.values():
            if v["mean_price"]:
                v["overpay_pct"] = round((v["mean_price"] / best - 1) * 100, 1)
    return out


@app.get("/api/runs")
def list_runs():
    return {"runs": LIVE_STORE.list_runs()}


@app.get("/api/analysis/compare")
def get_analysis_compare(scenario_id: Optional[str] = None, refresh: bool = False):
    return compare_runs(LIVE_STORE, scenario_id=scenario_id, refresh=refresh)


@app.get("/api/analysis/pairwise")
def get_pairwise_analysis(left_run_id: str, right_run_id: str, refresh: bool = False):
    try:
        return compare_pairwise(
            LIVE_STORE,
            left_run_id=left_run_id,
            right_run_id=right_run_id,
            refresh=refresh,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "unknown run_id") from exc


@app.post("/api/runs")
def create_run(req: LiveRunRequest, background_tasks: BackgroundTasks):
    try:
        meta = create_live_run(
            LIVE_STORE,
            Path(CONFIG_PATH),
            scenario_id=req.scenario_id,
            seed=req.seed,
            max_rounds=req.max_rounds,
            llm_provider=req.llm_provider,
            model=req.model,
            speed_ms=req.speed_ms,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    background_tasks.add_task(run_live_background, LIVE_STORE, meta["run_id"])
    return {
        "run_id": meta["run_id"],
        "status": meta["status"],
        "scenario_id": meta["scenario_id"],
        "current_turn": meta["current_turn"],
        "max_rounds": meta["max_rounds"],
    }


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    try:
        meta = LIVE_STORE.run_meta(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown run_id: {run_id}") from exc
    payload = dict(meta)
    if meta.get("status") == "completed":
        payload["summary"] = LIVE_STORE.result(run_id).get("summary")
    return payload


@app.get("/api/runs/{run_id}/events")
def get_run_events(run_id: str, after_turn: int = Query(0, ge=0)):
    try:
        LIVE_STORE.run_meta(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown run_id: {run_id}") from exc
    events = [e for e in LIVE_STORE.events(run_id) if int(e.get("turn", 0)) > after_turn]
    return {"run_id": run_id, "events": events}


@app.get("/api/runs/{run_id}/snapshot")
def get_run_snapshot(run_id: str, turn: Optional[int] = None):
    try:
        state = LIVE_STORE.state_at(run_id, turn)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown run_id or turn: {run_id}") from exc
    max_turn = int(state.get("turn", 0))
    events = [e for e in LIVE_STORE.events(run_id) if int(e.get("turn", 0)) <= max_turn]
    return snapshot_from_state(state, events)


@app.get("/api/runs/{run_id}/analysis")
def get_run_analysis(run_id: str, refresh: bool = False):
    try:
        return load_or_analyze_run(LIVE_STORE, run_id, force=refresh)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown run_id: {run_id}") from exc


@app.post("/api/runs/{run_id}/analysis/recompute")
def recompute_run_analysis(run_id: str):
    try:
        return load_or_analyze_run(LIVE_STORE, run_id, force=True)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown run_id: {run_id}") from exc


@app.get("/api/runs/{run_id}/context")
def get_run_context(run_id: str, turn: Optional[int] = None, agent_id: Optional[str] = None):
    try:
        meta = LIVE_STORE.run_meta(run_id)
        state = LIVE_STORE.state_at(run_id, turn)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown run_id or turn: {run_id}") from exc

    players = player_contexts_from_state(state)
    if agent_id:
        players = [player for player in players if player["id"] == agent_id]
        if not players:
            raise HTTPException(404, f"unknown agent_id: {agent_id}")

    try:
        config_snapshot = LIVE_STORE.config_snapshot(run_id)
    except FileNotFoundError:
        config_snapshot = None

    return {
        "run": meta,
        "turn": state.get("turn", 0),
        "config_snapshot": config_snapshot,
        "players": players,
    }


@app.get("/api/runs/{run_id}/messages")
def get_run_messages(run_id: str, agent_id: Optional[str] = None):
    try:
        state = LIVE_STORE.latest_state(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"unknown run_id: {run_id}") from exc
    messages = LIVE_STORE.messages(run_id)
    if agent_id:
        if agent_id not in state.get("agents", {}):
            raise HTTPException(404, f"unknown agent_id: {agent_id}")
        messages = [
            m for m in messages
            if m.get("sender") == agent_id or m.get("recipient") == agent_id
        ]
    return {"run_id": run_id, "messages": messages}


@app.get("/api/runs/{run_id}/debug/agents/{agent_id}/turns/{turn}")
def get_agent_trace(run_id: str, agent_id: str, turn: int):
    try:
        return LIVE_STORE.trace(run_id, agent_id, turn)
    except FileNotFoundError as exc:
        raise HTTPException(404, "trace not found") from exc


# Static: serve repo root so demo.html loads + /runs/*.json is reachable
if os.path.isdir(RUNS_DIR):
    app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")
app.mount("/", StaticFiles(directory=REPO_ROOT, html=True), name="root")
