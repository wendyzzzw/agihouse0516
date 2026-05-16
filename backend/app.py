"""FastAPI server — serves the frontend AND exposes the sim API.

Run:  uvicorn app:app --reload --port 8000
Then open: http://localhost:8000/demo.html
"""
from __future__ import annotations
import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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
    eng = Engine(
        config_path=req.config_path,
        simulation_id=req.simulation_id,
        override_topology=req.topology,
        seed=req.seed,
        llm_mode=req.mode,
        model=req.model,
    )
    return eng.run()


@app.get("/api/sim/cached/{topology}")
def cached_run(topology: str):
    if topology not in FRONTEND_TOPOLOGIES:
        raise HTTPException(404, f"unknown topology: {topology}")
    path = os.path.join(RUNS_DIR, f"{topology}.json")
    if not os.path.exists(path):
        raise HTTPException(404, f"no cached run; run `python run.py --topology {topology}` first")
    return FileResponse(path, media_type="application/json")


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
