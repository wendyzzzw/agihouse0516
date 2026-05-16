# AgentArena: Game-Theory Simulation for the Internet of Agents

## The Problem

The internet is about to change. Soon every human will have 100+ AI agents acting on their behalf — buying, selling, researching, negotiating. But nobody knows how agent economies actually behave at scale. Will prices converge or diverge? Will agents collude? Will well-connected agents exploit isolated ones? We can't deploy millions of agents on the real internet and hope for the best. We need a simulation sandbox to understand agent market dynamics before they happen.

## The Insight

**Network topology determines market outcomes more than individual agent strategy.** In our simulations, isolated buyers overpay by 22% compared to well-connected ones — same agents, same sellers, same market. The only difference is who can talk to whom. The topology is the real battleground between buyers and sellers.

## What AgentArena Does

AgentArena is a simulation environment where autonomous AI agents compete in realistic markets. Unlike toy game-theory models, our agents:

- **Build their own tools** — valuation models, price trackers, neighborhood scorers — investing time and budget to gain analytical edges
- **Actively probe each other** — requesting information, negotiating data trades, discovering new connections through introductions
- **Operate with local knowledge only** — each agent sees only its own connections, not the full network. Must explore, infer, and decide under uncertainty
- **Pursue diverse goals** — a budget-focused first-time buyer behaves differently from a time-pressured relocating family or a risk-averse investor
- **Deceive and collude** — agents can lie about prices, form cartels, and sabotage competitors. The simulation tests how topology affects these dynamics

## Key Experiments

| Experiment | What We Learn |
|---|---|
| **Topology sweep** | Same market across 6 buyer topologies. Measures price, fairness, convergence. |
| **Information cascade** | How fast does price intelligence spread? Where do information bubbles form? |
| **Collusion dynamics** | Which topologies enable cartels? When do they collapse? |
| **Tool arms race** | Do agents independently specialize? Does a tool-trading meta-market emerge? |
| **Fog of war** | How does local-only knowledge change strategy vs. full-information baselines? |

## Core Findings (Hypothesized)

1. **Buyer connectivity is a public good.** Sellers benefit from keeping buyers isolated. Buyers benefit from connecting. The topology is the battleground.
2. **Network position predicts price more than strategy.** A mediocre agent with great connections outperforms a brilliant agent with none.
3. **Trust networks emerge and restructure the graph.** Agents that lie lose connections. Honest agents attract more. The topology evolves based on trust.
4. **Tool specialization creates a meta-economy.** Some agents become information brokers or tool builders rather than direct market participants.
5. **Diverse goals increase market efficiency.** When agents want different things, they trade rather than fight — reducing conflict and improving outcomes for everyone.

## Demo (5 minutes)

20 agents compete for real estate. They start with nothing — no tools, 3 connections each, local knowledge only. Watch them probe for information, build valuation tools, negotiate, deceive, and trade. Then change the topology — same agents, same market — and watch prices diverge by 22%. Show the satisfaction scorecard: which agents met their human's goals? The punchline: **what you pay depends more on who you know than how smart you are.**

## Technical Approach

- **Agents**: LLM-powered (Claude API) with autonomous decision-making — build tools, probe, negotiate, deceive
- **Markets**: Configurable via YAML — real estate, flights, e-commerce, API pricing
- **Topology**: networkx — fully connected, clustered, small-world, scale-free, hub-and-spoke, with dynamic evolution
- **Visualization**: React + D3.js — live price charts, topology graphs, agent activity feeds, side-by-side comparison
- **RL training** (future): Train agents via reinforcement learning to discover optimal strategies per topology

## Why This Matters

The internet of agents is coming. Services will be built by agents, consumed by agents, and paid for by agents. Before that happens, we need to understand how agent economies behave — what market mechanisms work, what topologies are fair, and how to prevent manipulation. AgentArena is the sandbox where we figure that out.
