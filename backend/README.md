# AgentArena Backend

Python backend for the IoA dynamic-pricing simulation. Drives the existing
`demo.html` frontend with **real** event streams produced by either rule-based
agents or `claude -p` LLM agents.

## Config — all YAML

Human-edited config lives under `config/`, in the schema shared with teammates'
`test_config.yaml` (canonical) and `example_config.yaml` (reference).

```
agihouse0516/                  # repo root — YAML configs live here
├── flight_booking.yaml        # our flagship — 15 buyers / 2 airlines / 10 seats
├── test_config.yaml           # teammate canonical — 5 sims (open_bazaar, etc.)
└── example_config.yaml        # teammate alternate — 33-agent GPU flash sale
```

Each YAML has the same top-level shape:

```yaml
config_version: 1
global_defaults: { ... }
archetypes:
  sellers: { name: "free-text description", ... }
  buyers:  { name: "free-text description", ... }
simulations:
  - id: <string>
    settings:      { max_rounds, num_sellers, num_buyers, ... }
    market_rules:  { pricing, allow_negotiation, ... }
    topology:      { input_type: generated | edge_list, generator | edges }
    sellers:       [ { id, archetype, inventory, starting_price, min_price, goal }, ... ]
    buyers:        [ { id, archetype, budget, goal }, ... ]
    pricing:       { ... }       # optional, our extension — yield-mgmt coefficients
    llm:           { ... }       # optional, our extension — claude -p knobs
```

JSON is **only** used at wire boundaries that can't be YAML:
- `runs/*.json` — browsers fetch this; native JSON support
- `claude -p --json-schema` — CLI flag literally requires a JSON-schema string
- REST API request/response — standard

## Architecture (the 5-bullet version)

- **Single-agent schema** (`schemas.py::AgentState`) — id, persona (archetype +
  role + description), budget, beliefs, tools, inbox, outbox, goal.
- **Communication matrix** (`config/*.yaml::topology` → `topology.py::build_matrix`)
  — `Dict[node][node] -> bool`. **Topology only affects `matrix[a][b]`**. Swap
  matrix → different topology. Agent logic doesn't change.
- **Two actions** (`schemas.py::Action`) — `BUY(target=seller_id)` or
  `COMMUNICATE(target=agent_id, content=text)`. (`WAIT` is the no-op default.)
- **Per-tick decision** — engine calls `agent_runtime.decide()` which either
  shells out to `claude -p --json-schema ...` (LLM mode) or runs the
  goal-driven rule fallback. Both return the same Action JSON.
- **Output** — `runs/{topology}.json` with `events`, `prices_over_time`,
  `agents`, `comm_matrix`, `summary`, `conversations`, `config`.

## Files

```
backend/
├── config.py           # YAML loader; load(path) + simulation(cfg, id) + archetype(cfg, role, name)
├── personas.py         # make_persona(role, archetype) reads archetypes from YAML
├── topology.py         # build_matrix(sim, override_topology=None) — generator + edge_list
├── market.py           # Seller dynamic pricing (yield management, 3 signals)
├── agent_runtime.py    # claude -p invocation + goal-driven rule fallback
├── engine.py           # Tick loop, event emission, snapshot
├── run.py              # CLI runner
├── app.py              # FastAPI: /api/sim/run, /api/sim/compare, /api/sim/cached, /api/sim/configs
├── schemas.py          # Pydantic types
├── requirements.txt
└── README.md
```

## Install

```bash
cd backend
pip install -r requirements.txt
```

## Quick start

```bash
# List simulations in the default config
python run.py --list-simulations

# Single topology, rule-based (fast)
python run.py --topology small_world --mode rule

# All 5 topologies (overrides buyer_buyer_edges per run)
python run.py --all --mode rule

# Pick a different YAML / simulation
python run.py --config ../test_config.yaml --simulation open_bazaar --mode rule

# Real LLM agents (slower; one claude -p per agent per tick)
python run.py --topology isolated --mode claude --model haiku

# Start API + static frontend
uvicorn app:app --port 8000 --reload
# then open http://localhost:8000/demo.html
```

## REST API

- `GET  /api/sim/topologies` — list frontend topologies
- `GET  /api/sim/configs` — list YAML files and their simulation ids
- `POST /api/sim/run` — body: `{config_path, simulation_id, topology, seed, mode, model}` → full result
- `GET  /api/sim/cached/{topology}` — serve pre-generated `runs/*.json`
- `GET  /api/sim/compare?seeds=5` — overpay% across the 5 topologies

## Action JSON schema

Every agent decision conforms to:

```json
{
  "action": "BUY" | "COMMUNICATE" | "WAIT",
  "target": "Airline_A" | "buyer_3" | null,
  "content": "What price are you seeing?" | null,
  "reasoning": "one-sentence why"
}
```

Passed to `claude -p --json-schema` so the LLM is forced to comply.

## Verifying the frontend is actually reading backend data

The leaderboard has a **Tokens** column. Numbers come straight from the
`claude -p` wrapper's `usage` + `total_cost_usd` (Anthropic API response).
They cannot be forged client-side.

- Rule mode → `0` for every agent (zero LLM calls).
- Claude mode → real thousands-scale token counts; hover any cell to see
  the breakdown (input / output / cache_read / cache_creation / cost / wall).
- `summary.total_tokens`, `summary.total_llm_calls`, `summary.total_cost_usd`
  also live on each `runs/*.json` so you can `jq` them straight from disk.

Quick claude proof (≤3 min with parallel workers):

```bash
python run.py --topology small_world --mode claude --ticks 3 --workers 8
```

This overwrites `runs/small_world.json` with a real LLM run. Refresh the
demo, pick Small World, click Run — the Tokens column lights up.

## Smoke testing

```bash
./scripts/quick.sh small_world     # 1s: single topology run
SKIP_CLAUDE=1 ./scripts/test.sh    # 5s: regen + validate + compare
./scripts/test.sh                  # 40s: above + 1 real claude -p call
```
