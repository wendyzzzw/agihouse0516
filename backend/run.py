"""CLI runner.

Usage:
  python run.py                                       # default config + simulation, rule mode
  python run.py --topology isolated                   # override buyer-buyer topology
  python run.py --all                                 # run all 5 topologies (override sweep)
  python run.py --mode claude --topology small_world  # real LLM agents
  python run.py --config ../config/test_config.yaml --simulation open_bazaar
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from typing import Optional

import yaml

from engine import Engine
from config import DEFAULT_CONFIG, load, list_simulations


def save_run(result: dict, path: str) -> None:
    """Save a sim result to disk. Extension picks the format (.yaml or .json)."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, "w", encoding="utf-8") as f:
        if ext in (".yaml", ".yml"):
            yaml.safe_dump(result, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
        else:
            json.dump(result, f, indent=2, ensure_ascii=False)

# Topologies the frontend exposes via buttons.
FRONTEND_TOPOLOGIES = ["isolated", "clustered", "small_world", "hub_spoke", "fully_connected"]


def run_one(config_path: str, simulation_id, override_topology, seed: int,
            mode: str, model: str, out: str, ticks: Optional[int] = None,
            workers: int = 8) -> dict:
    t0 = time.time()
    eng = Engine(
        config_path=config_path,
        simulation_id=simulation_id,
        override_topology=override_topology,
        seed=seed,
        llm_mode=mode,
        model=model,
        ticks_override=ticks,
        llm_workers=workers,
    )
    result = eng.run()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    save_run(result, out)
    s = result["summary"]
    elapsed = time.time() - t0
    print(f"[{result['topology']:>16}] avg=${s['avg_price']:>6} bought={s['n_bought']:>2}/{s['n_bought']+s['n_missed']:>2} "
          f"sat={s['avg_satisfaction']:>5.1f}% msgs={s['total_messages']:>3} "
          f"events={len(result['events']):>3} ({elapsed:.1f}s) → {out}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="YAML config file")
    ap.add_argument("--simulation", default=None,
                    help="simulation id within the config file (defaults to first)")
    ap.add_argument("--topology", choices=FRONTEND_TOPOLOGIES, default=None,
                    help="override buyer-buyer topology (else uses what's in the config)")
    ap.add_argument("--all", action="store_true",
                    help="run all 5 frontend topologies (override sweep)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ticks", type=int, default=None,
                    help="override max_rounds from YAML (useful for quick claude proofs)")
    ap.add_argument("--workers", type=int, default=8,
                    help="parallel claude -p workers per tick (claude mode only)")
    ap.add_argument("--mode", choices=["claude", "rule"], default="rule")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--out", default=None)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--list-simulations", action="store_true",
                    help="just list simulation ids in the config and exit")
    args = ap.parse_args()

    if args.list_simulations:
        for sim_id in list_simulations(load(args.config)):
            print(sim_id)
        return 0

    here = os.path.dirname(os.path.abspath(__file__))
    runs_dir = args.outdir or os.path.normpath(os.path.join(here, "..", "runs"))

    if args.all:
        for topo in FRONTEND_TOPOLOGIES:
            out = os.path.join(runs_dir, f"{topo}.yaml")
            run_one(args.config, args.simulation, topo, args.seed, args.mode, args.model, out,
                ticks=args.ticks, workers=args.workers)
        return 0

    topo = args.topology
    out = args.out or os.path.join(runs_dir, f"{topo or 'run'}.yaml")
    run_one(args.config, args.simulation, topo, args.seed, args.mode, args.model, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
