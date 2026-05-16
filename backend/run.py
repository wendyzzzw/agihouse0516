"""CLI runner.

Usage:
  python run.py --topology small_world --mode rule --out ../runs/small_world.json
  python run.py --topology isolated --mode claude --model haiku
  python run.py --all --mode rule          # generate JSON for all 5 topologies
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time

from engine import Engine
from topology import TOPOLOGIES


def run_one(topology: str, seed: int, ticks: int, mode: str, model: str, out: str) -> dict:
    t0 = time.time()
    eng = Engine(topology, seed=seed, ticks=ticks, llm_mode=mode, model=model)
    result = eng.run()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    s = result["summary"]
    elapsed = time.time() - t0
    print(f"[{topology:>16}] avg=${s['avg_price']:>6} bought={s['n_bought']:>2}/15 "
          f"sat={s['avg_satisfaction']:>5.1f}% msgs={s['total_messages']:>3} "
          f"events={len(result['events']):>3} ({elapsed:.1f}s) → {out}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology", choices=TOPOLOGIES, default=None)
    ap.add_argument("--all", action="store_true", help="run all 5 topologies")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ticks", type=int, default=55)
    ap.add_argument("--mode", choices=["claude", "rule"], default="rule")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--out", default=None, help="output JSON path (single mode)")
    ap.add_argument("--outdir", default=None, help="output directory (--all mode)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    runs_dir = args.outdir or os.path.normpath(os.path.join(here, "..", "runs"))

    if args.all:
        for topo in TOPOLOGIES:
            out = os.path.join(runs_dir, f"{topo}.json")
            run_one(topo, args.seed, args.ticks, args.mode, args.model, out)
        return

    if not args.topology:
        ap.error("must pass --topology or --all")
    out = args.out or os.path.join(runs_dir, f"{args.topology}.json")
    run_one(args.topology, args.seed, args.ticks, args.mode, args.model, out)


if __name__ == "__main__":
    sys.exit(main())
