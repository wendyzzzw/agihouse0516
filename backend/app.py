"""FastAPI server — serves the frontend AND exposes /api/sim/run, /api/sim/compare.

Run:  uvicorn app:app --reload --port 8000
Then open: http://localhost:8000/demo.html
"""
from __future__ import annotations
import os
import json
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from engine import Engine
from topology import TOPOLOGIES


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(REPO_ROOT, "runs")

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


@app.get("/api/sim/topologies")
def list_topologies():
    return {"topologies": TOPOLOGIES}


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


# Static: serve repo root so demo.html loads + /runs/*.json is reachable
if os.path.isdir(RUNS_DIR):
    app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")
app.mount("/", StaticFiles(directory=REPO_ROOT, html=True), name="root")
