"""FastAPI server — serves the frontend AND the simulation API.

Run:  uvicorn app:app --reload --port 8000   (or scripts/serve.sh)
Then open: http://localhost:8000/demo.html

Endpoints:
  GET  /api/sim/topologies        list topologies
  POST /api/sim/run               run a sim synchronously, return full result
  POST /api/sim/start             start a sim in the background, return a run_id
  GET  /api/sim/stream/{run_id}   SSE — live events as the background run produces them
  GET  /api/sim/replay/{topology} SSE — replay a cached run, round-paced (demo / tests)
  GET  /api/sim/cached/{topology} the pre-generated runs/*.json
  GET  /api/sim/compare           topology price-spread comparison
"""
from __future__ import annotations
import os
import re
import json
import time
import threading
from typing import Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from engine import Engine
from topology import TOPOLOGIES
from config import ConfigError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(REPO_ROOT, "runs")
LIVE_DIR = os.path.join(RUNS_DIR, "live")

app = FastAPI(title="AgentArena Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# In-memory registry of background runs: run_id -> {run_dir, status, result?}.
_RUNS: Dict[str, Dict[str, Any]] = {}


class RunRequest(BaseModel):
    topology: str = "small_world"
    seed: int = 42
    ticks: int = 55
    mode: str = Field("rule", description="rule | claude")
    model: str = "haiku"


def _validate(req: RunRequest) -> None:
    if req.topology not in TOPOLOGIES:
        raise HTTPException(400, f"unknown topology: {req.topology}")
    if req.mode not in ("rule", "claude"):
        raise HTTPException(400, f"unknown mode: {req.mode}")


def _sse(payload: Any) -> str:
    """Format one Server-Sent Event frame."""
    return f"data: {json.dumps(payload) if not isinstance(payload, str) else payload}\n\n"


@app.get("/api/sim/topologies")
def list_topologies():
    return {"topologies": TOPOLOGIES}


@app.post("/api/sim/run")
def run_sim(req: RunRequest):
    """Run synchronously and return the full result (blocks until done)."""
    _validate(req)
    eng = Engine(req.topology, seed=req.seed, ticks=req.ticks,
                 llm_mode=req.mode, model=req.model)
    return eng.run()


@app.post("/api/sim/start")
def start_sim(req: RunRequest):
    """Start a sim in a background thread. Returns a run_id to stream from.

    `topology` accepts any config name (not just the 5 presets) — e.g.
    `live_demo` — so the browser can drive a real claude-mode run."""
    if req.mode not in ("rule", "claude"):
        raise HTTPException(400, f"unknown mode: {req.mode}")
    # Construct now (cheap — no LLM calls yet) so we know the run_id up front.
    try:
        eng = Engine(req.topology, seed=req.seed, ticks=req.ticks,
                     llm_mode=req.mode, model=req.model)
    except ConfigError as e:
        raise HTTPException(400, f"unknown config '{req.topology}': {e}")
    run_id = f"{eng.scenario.name}-{eng.seed}"
    _RUNS[run_id] = {"run_dir": eng.run_dir, "status": "running"}

    def _go():
        try:
            result = eng.run()
            _RUNS[run_id].update(status="done", result=result)
        except Exception as e:                       # surface failures to the client
            _RUNS[run_id].update(status="error", error=str(e))

    threading.Thread(target=_go, daemon=True).start()
    return {"run_id": run_id, "run_dir": eng.run_dir, "status": "running"}


@app.get("/api/sim/stream/{run_id}")
def stream_run(run_id: str):
    """SSE: tail a background run's events.jsonl live until run_complete."""
    info = _RUNS.get(run_id)
    run_dir = info["run_dir"] if info else os.path.join(LIVE_DIR, run_id)
    events_path = os.path.join(run_dir, "events.jsonl")

    def gen():
        waited = 0.0
        while not os.path.exists(events_path) and waited < 10:
            time.sleep(0.1); waited += 0.1
        if not os.path.exists(events_path):
            yield _sse({"kind": "error", "msg": f"no such run: {run_id}"})
            return
        with open(events_path) as f:
            idle = 0.0
            while True:
                line = f.readline()
                if line:
                    idle = 0.0
                    yield _sse(line.strip())
                    if '"run_complete"' in line:
                        break
                else:
                    time.sleep(0.1)
                    idle += 0.1
                    # a slow LLM turn can pause the event flow for a while —
                    # only give up after a long stall.
                    if idle > 120:
                        yield _sse({"kind": "stream_timeout"})
                        break

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/sim/replay/{topology}")
def replay(topology: str, delay: float = Query(0.15, ge=0.0, le=5.0)):
    """SSE: replay a cached runs/*.json round-by-round. Deterministic — used by
    the Playwright liveness test and for a watchable demo pace. `delay` is the
    pause between ROUNDS (not between every event)."""
    # any cached run is replayable (not just the 5 presets) — e.g. llm_smoke.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", topology):
        raise HTTPException(400, f"bad run name: {topology}")
    path = os.path.join(RUNS_DIR, f"{topology}.json")
    if not os.path.exists(path):
        raise HTTPException(404, f"no cached run for {topology}")
    with open(path) as f:
        d = json.load(f)

    def gen():
        # opening frame: enough to build the graph before events arrive
        yield _sse({"kind": "replay_start", "topology": topology,
                    "comm_matrix": d["comm_matrix"], "agents": d["agents"],
                    "rounds": d.get("rounds", d.get("ticks"))})
        prev_round = None
        for e in d["events"]:
            r = e.get("round")
            if prev_round is not None and r != prev_round and delay > 0:
                time.sleep(delay)
            prev_round = r
            yield _sse(e)
        yield _sse({"kind": "replay_end", "summary": d["summary"]})

    return StreamingResponse(gen(), media_type="text/event-stream")


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
    """Run each topology N times, return summary stats. The headline spread."""
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
    best = min((v["mean_price"] for v in out.values() if v["mean_price"]), default=None)
    if best:
        for v in out.values():
            if v["mean_price"]:
                v["overpay_pct"] = round((v["mean_price"] / best - 1) * 100, 1)
    return out


# Static: serve repo root so demo.html loads + /runs/*.json is reachable.
if os.path.isdir(RUNS_DIR):
    app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")
app.mount("/", StaticFiles(directory=REPO_ROOT, html=True), name="root")
