"""FastAPI server — serves the frontend AND exposes the sim API.

Run:  uvicorn app:app --reload --port 8000
Then open: http://localhost:8000/demo.html
"""
from __future__ import annotations
import json
import os
import queue
import threading
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from engine import Engine
from config import DEFAULT_CONFIG, load, list_simulations


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(REPO_ROOT, "runs")
CONFIG_DIR = REPO_ROOT      # YAML configs live at repo root

FRONTEND_TOPOLOGIES = ["isolated", "clustered", "small_world", "hub_spoke", "fully_connected"]


app = FastAPI(title="AgentArena Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class RunRequest(BaseModel):
    config_path: str = Field(default=DEFAULT_CONFIG)
    simulation_id: Optional[str] = None
    topology: Optional[str] = None        # override
    seed: int = 42
    mode: str = "rule"
    model: str = "haiku"


@app.get("/api/sim/topologies")
def list_topologies():
    return {"topologies": FRONTEND_TOPOLOGIES}


@app.get("/api/sim/configs")
def list_configs():
    """Available YAML config files + the simulations each contains."""
    out = {}
    for fn in sorted(os.listdir(CONFIG_DIR)):
        if fn.endswith(".yaml") or fn.endswith(".yml"):
            path = os.path.join(CONFIG_DIR, fn)
            try:
                out[fn] = list_simulations(load(path))
            except Exception as e:
                out[fn] = {"error": str(e)}
    return out


@app.post("/api/sim/run")
def run_sim(req: RunRequest):
    if req.topology and req.topology not in FRONTEND_TOPOLOGIES:
        raise HTTPException(400, f"unknown topology: {req.topology}")
    if req.mode not in ("rule", "claude"):
        raise HTTPException(400, f"unknown mode: {req.mode}")
    # Resolve relative config paths against the repo root so the frontend can
    # send a plain filename like "flight_booking_live.yaml".
    cfg_path = req.config_path
    if not os.path.isabs(cfg_path):
        candidate = os.path.join(REPO_ROOT, cfg_path)
        if os.path.exists(candidate):
            cfg_path = candidate
    eng = Engine(
        config_path=cfg_path,
        simulation_id=req.simulation_id,
        override_topology=req.topology,
        seed=req.seed,
        llm_mode=req.mode,
        model=req.model,
    )
    return eng.run()


@app.get("/api/sim/stream")
def stream_sim(
    config_path: str = "flight_booking_live.yaml",
    simulation_id: Optional[str] = None,
    topology: str = "small_world",
    mode: str = "claude",
    model: str = "haiku",
    seed: int = 42,
):
    """SSE: every `claude -p` reply is pushed immediately, sequentially.
    Frontend opens an EventSource on this URL.

    Message envelopes:
        data: {"type": "event",   "data": <event dict>}
        data: {"type": "tick",    "data": <prices snapshot>}
        data: {"type": "summary", "data": <full snapshot>}
        data: {"type": "done"}
        data: {"type": "error",   "msg": "..."}
    """
    if topology not in FRONTEND_TOPOLOGIES:
        raise HTTPException(400, f"unknown topology: {topology}")
    if mode not in ("rule", "claude"):
        raise HTTPException(400, f"unknown mode: {mode}")
    cfg_path = config_path
    if not os.path.isabs(cfg_path):
        candidate = os.path.join(REPO_ROOT, cfg_path)
        if os.path.exists(candidate):
            cfg_path = candidate

    def event_stream():
        q: "queue.Queue[Optional[dict]]" = queue.Queue()

        def on_event(e):
            q.put({"type": "event", "data": e})

        def on_tick(t):
            q.put({"type": "tick", "data": t})

        def run_engine():
            try:
                eng = Engine(
                    config_path=cfg_path,
                    simulation_id=simulation_id,
                    override_topology=topology,
                    seed=seed,
                    llm_mode=mode,
                    model=model,
                    llm_workers=1,                 # strictly sequential — stream each call
                    on_event=on_event,
                    on_tick=on_tick,
                )
                final = eng.run()
                q.put({"type": "summary", "data": final})
            except Exception as exc:
                q.put({"type": "error", "msg": str(exc)[:300]})
            finally:
                q.put({"type": "done"})
                q.put(None)

        threading.Thread(target=run_engine, daemon=True).start()

        # Initial comment to flush headers immediately so the browser opens
        # the EventSource connection before the first 15s of claude latency.
        yield ": stream open\n\n"
        while True:
            msg = q.get()
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/sim/cached/{topology}")
def cached_run(topology: str):
    if topology not in FRONTEND_TOPOLOGIES:
        raise HTTPException(404, f"unknown topology: {topology}")
    path = os.path.join(RUNS_DIR, f"{topology}.yaml")
    if not os.path.exists(path):
        raise HTTPException(404, f"no cached run; run `python run.py --topology {topology}` first")
    return FileResponse(path, media_type="application/yaml")


@app.get("/api/sim/compare")
def compare(seeds: int = Query(5, ge=1, le=50)):
    """Run each topology N times via the override sweep; return mean prices + overpay%."""
    out = {}
    for topo in FRONTEND_TOPOLOGIES:
        prices, sats, missed = [], [], []
        for seed in range(seeds):
            eng = Engine(override_topology=topo, seed=seed, llm_mode="rule")
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


# Static: serve repo root so demo.html loads + /runs/*.json is reachable
if os.path.isdir(RUNS_DIR):
    app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")
app.mount("/", StaticFiles(directory=REPO_ROOT, html=True), name="root")
