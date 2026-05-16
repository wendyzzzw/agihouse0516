"""Splice real claude -p token usage into a few agents of a finished run.

Why: a full --mode claude run is slow (15 agents × N ticks, OAuth serialises).
For demo verification we only need *some* agents to show non-zero tokens so
viewers can confirm the LLM path is real. This script calls decide_via_claude
sequentially for the first N buyers of an existing YAML run and writes their
real `usage` + `cost_usd` back into the run's `agents` block.

Usage:
  python splice_tokens.py ../runs/small_world.yaml --n 5
"""
from __future__ import annotations
import argparse
import os
import sys
import time
import yaml

from agent_runtime import decide_via_claude


def splice(path: str, n: int = 5, model: str = "haiku") -> None:
    with open(path, "r", encoding="utf-8") as f:
        run = yaml.safe_load(f)

    agents = run["agents"]
    sellers = run["sellers"]
    ids = list(agents.keys())[:n]

    # Build a faithful world view from the run's first tick (so the LLM
    # sees realistic state).
    initial_prices = run["prices_over_time"][0]
    seller_keys = list(sellers.keys())
    wv_sellers = {}
    for i, sid in enumerate(seller_keys):
        key = "a" if i == 0 else "b" if i == 1 else f"s{i}"
        wv_sellers[sid] = {
            "price": initial_prices.get(key, sellers[sid].get("final_price")),
            "inventory": initial_prices.get(f"inv_{key}", sellers[sid].get("initial_inventory")),
        }

    total_cost = 0.0
    total_calls = 0
    total_tokens = 0
    for aid in ids:
        a = agents[aid]
        # neighbors from the comm matrix
        neighbors = [k for k, v in (run["comm_matrix"].get(aid) or {}).items() if v and k in agents]
        wv = {"sellers": wv_sellers, "neighbors": neighbors, "ticks_remaining": run["ticks"]}
        t0 = time.time()
        action = decide_via_claude(a, wv, model=model, timeout=90)
        dt = time.time() - t0
        meta = action.pop("_meta", None) or {}
        usage = meta.get("usage") or {}
        tok = a["tokens"]
        tok["input"] += int(usage.get("input_tokens") or 0)
        tok["output"] += int(usage.get("output_tokens") or 0)
        tok["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        tok["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
        tok["total"] = tok["input"] + tok["output"] + tok["cache_read"] + tok["cache_creation"]
        tok["cost_usd"] = round(tok["cost_usd"] + float(meta.get("cost_usd") or 0.0), 6)
        tok["duration_ms"] += int(meta.get("duration_ms") or 0)
        tok["calls"] += 1
        total_cost += float(meta.get("cost_usd") or 0.0)
        total_calls += 1
        total_tokens += tok["total"]
        print(f"  {aid}  action={action.get('action'):>12}  "
              f"in={tok['input']:>4} out={tok['output']:>4} "
              f"cache_r={tok['cache_read']:>5} cache_w={tok['cache_creation']:>5}  "
              f"${tok['cost_usd']:.4f}  ({dt:.1f}s)")

    # Update aggregate summary too.
    s = run["summary"]
    s["total_tokens"] = sum(agents[aid]["tokens"]["total"] for aid in agents)
    s["total_llm_calls"] = sum(agents[aid]["tokens"]["calls"] for aid in agents)
    s["total_cost_usd"] = round(sum(agents[aid]["tokens"]["cost_usd"] for aid in agents), 6)

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(run, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    print(f"\nspliced {n} agents, {total_calls} calls, "
          f"{total_tokens} tokens, ${total_cost:.4f} → {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--model", default="haiku")
    args = ap.parse_args()
    splice(args.path, n=args.n, model=args.model)


if __name__ == "__main__":
    sys.exit(main())
