# AgentArena — Game-Theory Simulation for the Internet of Agents

## One-Liner

A simulation environment for studying how network topology and agent strategy shape market dynamics in agent economies — OpenAI Gym for agent economics.

## Thesis

**In agent economies, the buyer-buyer topology IS the market structure. Connected buyers pay less, isolated buyers get exploited. Network position determines price more than negotiation skill. The topology is the real battleground between buyers and sellers.**

## The Problem

Before deploying millions of agents on the real internet, we need a sandbox to understand how agent economies behave. Questions nobody can answer yet:

- What happens when 1,000 agents negotiate for scarce resources simultaneously?
- How does network topology affect price fairness?
- Do agents learn to collude? When does it collapse?
- Can sellers exploit network fragmentation to price discriminate?
- What market mechanism (auction, negotiation, order book) produces the best outcomes?

## Core Components

```
┌─────────────────────────────────────────────┐
│              AgentArena                      │
│                                               │
│  ┌─────────┐  ┌──────────┐  ┌────────────┐  │
│  │ Scenario │  │  Agent   │  │ Simulation │  │
│  │ Designer │→ │ Registry │→ │  Engine    │  │
│  └─────────┘  └──────────┘  └─────┬──────┘  │
│                                     │         │
│                              ┌──────▼──────┐  │
│                              │ Observatory │  │
│                              │ (real-time  │  │
│                              │  analytics) │  │
│                              └──────┬──────┘  │
│                              ┌──────▼──────┐  │
│                              │  RL Trainer  │  │
│                              │ (learn from  │  │
│                              │  simulations)│  │
│                              └─────────────┘  │
└─────────────────────────────────────────────┘
```

### 1. Scenario Designer

Define markets via YAML config:

```yaml
scenario:
  name: "Flight Booking Market"
  
  sellers:
    - name: "Airline_A"
      inventory: 10
      floor_price: 200
      strategy: "yield_management"
    - name: "Airline_B"
      inventory: 8
      floor_price: 180
      strategy: "aggressive_undercut"

  buyers:
    count: 50
    budget_distribution: "normal(300, 50)"
    urgency_distribution: "uniform(0, 1)"
    strategy_mix:
      aggressive: 0.3
      patient: 0.3
      bargain_hunter: 0.2
      flexible: 0.2

  topology:
    buyer_seller: "segmented"
    buyer_buyer: "small_world"
    params:
      clusters: 5
      rewiring_probability: 0.1

  market_mechanism: "bilateral_negotiation"
  
  events:
    - time: 30s, type: "supply_shock", seats_removed: 5
    - time: 60s, type: "new_entrant", seller: "Airline_C"
    - time: 90s, type: "enable_collusion"
  
  behaviors:
    share_price_info: true
    coordinate_bidding: true
    group_buying: true
    deception: true

  duration: 120s
  speed: 10x
  runs: 1000
```

### 2. Agent Registry

Pluggable agent strategies:

```python
class BuyerAgent:
    def decide(self, state: MarketState) -> Action:
        """BID, WAIT, NEGOTIATE, SWITCH_SELLER, EXIT"""
    def negotiate(self, offer: Offer) -> CounterOffer:
        """Respond to a seller's offer."""

# Built-in strategies:
AggressiveBuyer    # buy ASAP, accept high prices
PatientBuyer       # wait for drops, risk missing out
BargainHunter      # always counter-offer lower
FlexibleBuyer      # explores alternatives (dates, routes)
CollusiveBuyer     # coordinates with connected buyers

# LLM-powered:
LLMBuyer           # Claude makes decisions via natural language

# RL-trained:
RLBuyer            # learns optimal strategy from simulations
```

### 3. Simulation Engine

```
Each tick (100ms simulated):
  1. Seller agents update prices based on demand/supply
  2. Buyer agents observe market state (filtered by topology)
  3. Buyer-buyer communication along graph edges
  4. Buyer agents submit actions
  5. Market engine matches bids to offers
  6. Transactions execute
  7. Events trigger if scheduled
  8. All state logged
```

### 4. Observatory (Real-time Visualization)

```
┌──────────────────────────────────────────────────────────┐
│  AgentArena Observatory                                   │
│                                                           │
│  ┌─ Price Discovery ────────┐  ┌─ Topology ────────────┐ │
│  │ $400|         ╱‾‾        │  │     ◉──◉   ◉──◉      │ │
│  │ $350|    ____╱           │  │     |╲╱|   |╲╱|      │ │
│  │ $300|___╱                │  │     ◉──◉───◉──◉      │ │
│  │     └────────────        │  │       ↕ info flow     │ │
│  └──────────────────────────┘  └────────────────────────┘ │
│                                                           │
│  ┌─ Strategy Performance ──────────────────────────────┐  │
│  │ Strategy      Won   Avg Price  Surplus               │  │
│  │ Aggressive    4/15  $342       -$42                   │  │
│  │ Patient       1/15  $280       +$20                   │  │
│  │ Bargain       2/10  $295       +$5                    │  │
│  │ Flexible      3/10  $240       +$60                   │  │
│  └─────────────────────────────────────────────────────┘  │
│                                                           │
│  ┌─ Emergent Dynamics ─────────────────────────────────┐  │
│  │ ⚡ t=30s: Supply shock → price spike +22%            │  │
│  │ 🤝 t=90s: Collusion → defection at t=93s            │  │
│  │ 📊 Equilibrium at t=45s ($310 ± $8)                  │  │
│  └─────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

### 5. RL Trainer (Future)

```
Environment: AgentArena market simulation
State:       Price, seats left, time, competitor behavior, budget
Action:      Bid amount, wait, negotiate, switch seller, exit
Reward:      +1 if got seat + surplus bonus - time penalty

Research questions:
  - Does RL agent learn to bluff?
  - Does it discover collusion independently?
  - Can it beat hand-crafted strategies?
  - What's the Nash equilibrium strategy?
```

---

## Why Current Pricing Breaks in Agent World

```
Today (humans):                    Agent world:
Price updates every few hours  →   Price updates every 200ms
Compare 2-3 airlines manually →   Compare ALL airlines instantly
Negotiate rarely               →   Negotiate every transaction
Switching cost is high         →   Switching cost is zero
Information asymmetry: seller  →   Near-perfect info on buyer side
```

Airline's information advantage evaporates. Agents have collective intelligence about pricing across all competitors in real time.

---

## Market Mechanisms

### 1. Posted Price (Won't Survive)

Agents eliminate comparison friction. 1,000 agents check all airlines in 200ms. Lowest price sells out instantly. Others forced to match → race to bottom.

### 2. English Auction (Ascending Bid)

Efficient price discovery. But: winner's curse, shill bidding, agent collusion.

### 3. Dutch Auction (Descending)

Price drops until someone bites. Each agent plays chicken. With 1,000 agents watching, price won't drop much.

### 4. Continuous Order Book (Stock Market Style)

Most likely equilibrium. Multiple buyers/sellers, standardized product, real-time discovery. But flights aren't stocks — perishable, not fungible, fixed supply.

### 5. Bilateral Negotiation (Agent-to-Agent Haggling)

Most flexible. Personalized pricing. Real-time price discrimination at scale.

### Likely Convergence: Hybrid

```
Layer 1 — Reference Price (public, updated continuously)
Layer 2 — Negotiation Band (private, per-agent)
Layer 3 — Surge/Scarcity Pricing (automatic, supply-driven)
```

---

## Buyer-Seller Topology Effects

Same 50 buyers, 3 sellers, 10 seats — different buyer-seller visibility:

```
Fully Connected          Segmented               Hub Broker
(everyone sees all)      (buyers see 1 seller)    (broker sees all)

 B B B B B B B B         B B B │ B B B │ B B B    B B B  B B B  B B B
  \|/|\|/|\|/|            \|/ │  \|/ │  \|/       \|/    \|/    \|/
   S₁   S₂   S₃           S₁ │   S₂ │   S₃        S₁    S₂    S₃
                                                      \    |    /
Avg price: $305          Avg price: $315               [BROKER]
Spread: $5               Spread: $70 (!!)
Efficiency: 97%          Efficiency: 62%           Avg price: $310
                                                   Broker profit: $200
```

---

## Buyer-Buyer Topology Effects (The Core Innovation)

The buyer-buyer graph determines information flow, collusion viability, and market power. This is the most novel part of the project.

### Five Buyer Topologies Compared

```
Topology 1 — Isolated (no buyer communication):

  B  B  B  B  B  B  B  B
  |  |  |  |  |  |  |  |
  └──┴──┴──┴──┴──┴──┴──┘
            SELLER

  Each buyer negotiates alone. Seller has maximum power.
  Result: Avg price $335, spread $60

Topology 2 — Fully Connected (group chat):

  B──B──B──B──B
  |╲╱|╲╱|╲╱|╲╱|
  B──B──B──B──B
        |
      SELLER

  Everyone shares everything. Seller can't price discriminate.
  Result: Avg price $275, spread $5

Topology 3 — Clustered (friend groups):

  [B──B──B──B]    [B──B──B──B]    [B──B──B──B]
   cluster A       cluster B       cluster C
        \              |              /
         └─────── SELLER ────────────┘

  Price info stays within cluster. Seller exploits info silos.
  Result: Avg price $305, spread $50

Topology 4 — Hub Influencer:

        B  B  B
         \ | /
     B ── HUB ── B
         / | \
        B  B  B
              |
           SELLER

  Hub aggregates info, broadcasts deals. But can also distort.
  Result: Avg price $280 (honest hub), $310 (corrupt hub)

Topology 5 — Small World:

  [B─B─B─B]──shortcut──[B─B─B─B]
       |                     |
  [B─B─B─B]──shortcut──[B─B─B─B]
              |
           SELLER

  Mostly local + few cross-cluster connections.
  Result: Avg price $290, spread $25 (best balance)
```

### Dynamics That Emerge From Buyer Topology

#### 1. Price Intelligence Propagation

```
Buyer_A discovers seller will go to $250.
How fast does everyone know?

            Isolated  Clustered  SmallWorld  ScaleFree  FullConn
t=0ms       ●○○○○○○   ●○○○│○○○○  ●○○○─○○○○   ●○○○○○○    ●○○○○○○
t=200ms     ●○○○○○○   ●●○○│○○○○  ●●○○─○○○○   ●○○○○○○    ●●●●●●●
t=500ms     ●○○○○○○   ●●●●│○○○○  ●●●○─●○○○   ●●●●●●●    done
t=1000ms    ●○○○○○○   ●●●●│○○○○  ●●●●─●●●●   done
t=∞         ●○○○○○○   never      done
            never!    crosses!
```

#### 2. Collusion Viability

```
Isolated:     Can't collude (no communication) → seller's paradise
Fully conn:   Forms fast, but defection visible → unstable
Clustered:    Local cartels only, seller plays clusters against each other
Small world:  Hub agents organize cross-cluster cartel, hard to police
Scale-free:   Super-connector leads cartel, remove leader → instant collapse
```

#### 3. Deception and Misinformation

```
Buyer_A and Buyer_B both want the last seat:

Honest:   B_A: "Seller offers $310." B_B: "Try for $310 too." Both benefit.
Deceptive: B_A: "Seller is sold out." B_B gives up. B_A buys at $310.

Topology effect:
  Isolated:     Can't lie (no communication)
  Fully conn:   Lies verified quickly (others check independently)
  Clustered:    Can lie to other clusters (can't verify cross-cluster)
  Hub:          Hub lies → everyone deceived (maximum damage)
```

#### 4. Group Buying / Demand Aggregation

```
Connected buyers can aggregate demand for volume discounts:

  B_A + B_B + B_C → "We represent 3 buyers. Volume discount?"
  Seller: "$280 each instead of $310."

Only works if buyers are connected. Hub agents become natural aggregators.
Clustered → intra-cluster discounts, cross-cluster pays full price (unfair).
```

#### 5. Competition Transparency

```
Last seat — 5 buyers competing:

Isolated:   Seller runs implicit auction. Extracts maximum price.
Connected:  Buyers see each other's bids. Can coordinate turns.
Clustered:  Intra-cluster competition visible, cross-cluster hidden.
            Seller can lie: "Cluster B bid $400" to inflate Cluster A.
```

### Topology Metrics That Predict Market Outcomes

| Metric | Predicts |
|---|---|
| Buyer graph density | Price convergence speed. Higher → faster → lower prices |
| Average path length | Information delay. Longer → more price dispersion |
| Clustering coefficient | Cartel viability. High → strong local cartels |
| Max betweenness centrality | Manipulation risk. High → one agent controls info |
| Connected components | Number of independent micro-markets |
| Degree distribution (Gini) | Fairness. High Gini → info inequality → price inequality |

### The Key Finding

```
┌──────────────────────────────────────────────────────────────┐
│  Buyer Topology Impact on Market Outcomes (1000 simulations) │
│                                                               │
│  Metric          Isolated  Cluster  SmallW  ScaleFr  FullCon │
│  ─────────────   ────────  ───────  ──────  ───────  ─────── │
│  Avg price       $335      $305     $290    $285     $275    │
│  Price spread    $60       $50      $25     $30      $5      │
│  Time to equil.  never     never    8s      5s       2s      │
│  Cartel rate     0%        40%      65%     55%      80%     │
│  Cartel duration -         12s      8s      3s       4s      │
│  Misinfo spread  0%        25%      60%     80%      10%     │
│  Fairness (Gini) 0.45      0.35     0.15    0.25     0.05   │
│  Seller revenue  $3,350    $3,050   $2,900  $2,850   $2,750 │
│                                                               │
│  Buyer connectivity directly determines seller revenue.       │
│  Isolated → seller extracts 22% more than fully connected.   │
│  Small world = sweet spot (efficient + resilient to gaming).  │
│                                                               │
│  Implication: Buyer connectivity is a PUBLIC GOOD.            │
│  Sellers benefit from keeping buyers isolated.                │
│  Buyers benefit from connecting.                              │
│  The topology IS the battleground.                            │
└──────────────────────────────────────────────────────────────┘
```

---

## Autonomous Agents: Tools, Probing, and Local Knowledge

The previous sections model agents as strategy-followers. Real agents are **autonomous entities** that build capabilities, actively explore their environment, and make decisions with incomplete information.

### Three Realism Upgrades

```
Simple simulation:                 Realistic simulation:
─────────────────────              ─────────────────────
Pre-set strategies              →  Agents build their own tools
Passive info sharing            →  Active probing and discovery
Global graph knowledge          →  Local-only graph knowledge
Fixed capabilities              →  Capabilities evolve during simulation
```

### Agents Build and Use Tools

Agents invest time and budget to build analytical tools that give them an edge. The tool-building tradeoff: better information costs turns and money.

**Tool examples by market:**

```
Real Estate:
  📊 Comparable Sales Analyzer  — estimate fair value from comps
  📈 Price Trend Model          — predict direction from historical data
  🏠 Neighborhood Scorer        — score location from school/crime/transit data
  🔍 Seller Motivation Detector — estimate urgency from listing duration + price drops

Flight Market:
  📈 Historical Price Tracker   — scraped fare data patterns
  🔮 Demand Predictor           — event calendar + holiday data
  ✈️ Route Optimizer            — alternative airports, connections
  ⏰ Timing Model               — best day/time to buy
```

**The tool-building tradeoff:**

```
Agent_A: Spends 3 turns building price model. Pays $268K (near true value).
Agent_B: Bids immediately with no tools. Pays $292K (listing price).
Agent_C: Builds 1 quick tool, then acts. Pays $275K (balanced).

$24K gap between tooled and untooled agents.
But Agent_B got a seat while Agent_A was still building tools.

Optimal tool investment depends on:
  - Market speed (fast markets penalize building)
  - Tool edge (complex markets reward tools)
  - Competition (diminishing returns if everyone has the same tools)
```

**Agents can share/trade tools — creating a meta-market:**

```
Agent_A built a great valuation tool. Agent_B wants it.

Options:
  1. Sell:        Agent_A sells tool for $30 (tool economy)
  2. Trade:       "My tool for your comps data"
  3. Offer SaaS:  "I'll run valuations for you, $5 per query"
  4. Keep exclusive: Maintain information edge

The TOOL economy might become more profitable than the 
underlying market. Some agents become full-time tool-builders.
```

### Active Probing

Agents don't passively receive information. They actively probe others — but probing has costs, risks, and strategic implications.

**Probing mechanics:**

```
Agent_A → Agent_B: "What did you pay for 123 Oak St?"

Possible responses:
  1. Truth:      "$285K" (cooperative)
  2. Lie:        "$310K" (inflate to make A overpay)
  3. Partial:    "Between $270K and $300K" (vague but honest)
  4. Refuse:     "I don't share that." (protect edge)
  5. Trade:      "I'll tell you if you share your school data tool."
  6. Misdirect:  "I heard Agent_C paid $320K." (redirect)

Probing rules:
  - Can only probe agents you're connected to (local edges)
  - Each probe costs time (1 turn) and sometimes money
  - Probing reveals YOUR INTEREST (target knows you're looking)
  - Repeated probing → declining returns / trust erosion
```

**Probing as graph exploration:**

```
Agent_A knows: [B, C, D]    (direct connections)
Agent_A does NOT know: B also knows [E, F]

Turn 1: A → B: "Know anyone with comps in this area?"
Turn 2: B → A: "Agent_E bought nearby recently."
Turn 3: A → B: "Can you introduce me?"
Turn 4: B → E: "Agent_A wants to talk."
Turn 5: E: "Sure." → NEW EDGE: A ↔ E

The graph EVOLVES through probing.
Agents actively expand their network when they need capabilities.
```

### Local-Only Graph Knowledge

No agent sees the full picture. This is what makes the simulation truly realistic.

**What each agent knows:**

```
Agent_A's world view:

  KNOWN:
    ✓ My connections: [B(trust:0.8), C(trust:0.5), D(new)]
    ✓ My interaction history: [B gave good info, C lied once]
    ✓ My tools: [price_trend, comp_analyzer]
    ✓ My observations from the market

  PARTIALLY KNOWN:
    ~ B mentioned knowing E (but I don't know E)
    ~ C seems interested in same property (inferred from behavior)
    ~ Seller responded quickly → might be motivated

  UNKNOWN:
    ✗ Full network topology
    ✗ How many total buyers exist
    ✗ What tools others have built
    ✗ What deals are happening elsewhere
    ✗ Whether B is sharing my info with others
    ✗ Whether C and D are colluding against me
```

**Why local knowledge changes everything:**

```
With global knowledge:              With local knowledge:
Agent knows all prices           →  Only knows prices from connections
Agent knows demand level         →  Guesses demand from local signals
Agent detects collusion          →  Collusion invisible unless you're in it
Agent can find best deal         →  Might miss deals outside network
Optimal strategy is computable   →  Must explore/exploit under uncertainty
```

**Strategic implications:**

1. **Exploration vs exploitation** — spend turns probing for better info, or act now?
2. **Information asymmetry is structural** — well-connected agents get better deals not because they're smarter, but because of graph position
3. **Trust under uncertainty** — when Agent_B says "price is $280K," you can't verify against global state. Must build local trust model from history.
4. **Inferring hidden structure** — "B and C both know about the same property and both refuse to share → might be colluding"
5. **Strategic opacity** — smart agents probe through intermediaries: "B, ask E about 123 Oak — don't mention me"

### What an Agent's Turn Looks Like

```
┌─────────────────────────────────────────────────────────┐
│  Agent_A — Turn 14                                       │
│                                                           │
│  Connections: [B(trust:0.8), C(trust:0.5), D(new)]       │
│  Tools: [price_trend(v2), comp_analyzer]                 │
│  Budget: $850 (spent $100 tools, $50 probes)             │
│                                                           │
│  Knowledge:                                               │
│    - 123 Oak listed at $295K (from listing)              │
│    - B paid $275K for similar (from probing B)           │
│    - C also interested in 123 Oak (inferred)             │
│    - D might know seller (D mentioned the area)          │
│    - price_trend tool: market declining 2%/month         │
│                                                           │
│  Reasoning:                                               │
│    "comp_analyzer says fair value is $268K.               │
│     Market declining → waiting saves money.               │
│     But C is also bidding → might lose if I wait.        │
│     Should I probe D for seller intel first?"            │
│                                                           │
│  Action options:                                          │
│    → Probe D: "Know the seller of 123 Oak?"              │
│    → Offer seller $270K                                   │
│    → Probe C: "Are you bidding on 123 Oak?" (risky)      │
│    → Build tool: seller_urgency_detector ($40, 2 turns)  │
│    → Wait and observe 1 more turn                         │
│                                                           │
│  Decision: Probe D (low risk, high potential value)       │
└─────────────────────────────────────────────────────────┘
```

### Updated Scenario Config

```yaml
scenario:
  name: "Real Estate Market — Full Autonomy"
  
  environment:
    type: "real_estate"
    properties:
      - id: "123_oak_st"
        listing_price: 295000
        true_value: 268000       # hidden from agents
        seller_urgency: 0.8      # hidden — must be inferred
      - id: "456_elm_st"
        listing_price: 310000
        true_value: 305000
        seller_urgency: 0.3

  agents:
    buyers:
      - count: 6
        profile: "budget_focused"
        goal_weights: {budget: 0.6, requirements: 0.3, risk: 0.1, time: 0.0}
        constraints: {max_budget: 280000, min_bedrooms: 2}
      - count: 4
        profile: "family_relocation"
        goal_weights: {budget: 0.1, requirements: 0.5, risk: 0.1, time: 0.3}
        constraints: {min_bedrooms: 3, good_schools: true, close_by: 60_days}
      - count: 3
        profile: "investor"
        goal_weights: {budget: 0.4, requirements: 0.1, risk: 0.4, time: 0.1}
        constraints: {positive_roi: true, rental_yield_min: 0.06}
      - count: 2
        profile: "flexible"
        goal_weights: {budget: 0.25, requirements: 0.25, risk: 0.25, time: 0.25}
        constraints: {}
      common:
        starting_budget: 350000
        starting_tools: []           # everyone starts with nothing
        starting_knowledge: ["listing prices only"]
    sellers:
      count: 2
      strategy: "autonomous"       # LLM-driven, not scripted

  topology:
    initial: "random_sparse"
    params:
      avg_degree: 3                # each agent knows ~3 others
      buyer_seller_edges: 1        # each buyer initially sees 1 seller
    evolution: true                # agents can form/break edges

  agent_capabilities:
    build_tools: true
    probe_agents: true
    share_tools: true
    form_new_connections: true
    deceive: true

  observability:
    graph_knowledge: "local_only"
    price_knowledge: "from_connections_only"
    tool_knowledge: "own_only"

  costs:
    probe: 1 turn + $10
    build_tool: 2-5 turns + $30-100
    form_connection: 1 turn (mutual consent)
```

### Emergent Behaviors (Hypotheses)

```
1. TOOL SPECIALIZATION
   Agents independently build different tools. Natural division of 
   labor: some become "tool builders" selling capabilities, others 
   become "info brokers" probing widely.

2. TRUST NETWORKS
   After many rounds, agents have empirical trust scores per connection.
   High-trust edges become stable partnerships. Low-trust edges get 
   dropped. The graph RESTRUCTURES based on trust.

3. INFORMATION BUBBLES
   With local-only knowledge, info doesn't reach everyone equally.
   Clusters develop different beliefs about fair price. Agents in 
   well-connected clusters pay fair value. Isolated agents overpay.

4. STRATEGIC INDIRECTION
   Smart agents probe through intermediaries:
   "Agent_B, ask Agent_E about 123 Oak. Don't mention me."
   Only possible — and only necessary — with local knowledge.

5. META-MARKET EMERGENCE
   Tools and information become tradeable goods. Some agents become 
   full-time info brokers: "Comps for $20, valuations for $50."
   The tool economy might be more profitable than the underlying market.

6. INEQUALITY FROM TOPOLOGY
   Well-connected agents get better info → build better tools → 
   make better deals → attract more connections → rich-get-richer → 
   scale-free topology emerges → structural inequality from 
   network effects.
```

---

## Agent Goals: Multi-Objective Utility

Agent goals shouldn't be one-dimensional ("minimize price"). Real agents serve their human's interests, which are multi-dimensional and personalized.

### Why Uniform Goals Are Unrealistic

```
If everyone minimizes price → pure zero-sum competition, boring, adversarial
If goals are diverse → trade opportunities, collaboration, richer dynamics
```

### Multi-Objective Goal Structure

Each agent has a utility function defined by its human, with personalized weights:

```
agent_goal:
  primary_objectives:
    budget_efficiency:   "minimize price relative to true value"
    requirement_match:   "meet hard constraints (size, location, timeline)"
    risk_minimization:   "avoid bad deals, scams, overpaying"
    time_efficiency:     "close the deal quickly"

  constraints:           # hard — must satisfy
    max_budget, min_bedrooms, max_commute, must_close_by

  preferences:           # soft — nice to have
    good_schools, quiet_neighborhood, modern_kitchen
```

### Different Humans → Different Agent Behavior

```
Agent_A (budget-conscious first-time buyer):
  weights: budget=0.6, requirements=0.3, risk=0.1
  → Builds price tools, probes for comps, lowballs, walks away easily
  → Might miss good properties by being too cheap

Agent_B (relocating family, time pressure):
  weights: requirements=0.5, time=0.3, budget=0.1, risk=0.1
  → Prioritizes RIGHT house FAST, willing to overpay
  → Probes for school data, not price comps

Agent_C (investor):
  weights: budget=0.4, risk=0.4, time=0.2
  → Builds ROI models, rental yield calculators
  → Won't buy without extensive due diligence
  → Multiple properties simultaneously
```

### Diverse Goals Create Trade Opportunities

```
Agent_A wants cheapest 3-bedroom.
Agent_B wants best school district, price secondary.

Two properties:
  Property 1: $270K, average school
  Property 2: $295K, top school

Uniform goals: both fight over Property 1. Bidding war.
Diverse goals: A takes Property 1, B takes Property 2. No conflict.

Market is MORE efficient with diverse goals — 
not everyone fights for the same thing.
```

### Goals Change Topology Dynamics

```
Agents with similar goals = competitors (adversarial edges)
Agents with complementary goals = potential collaborators

Agent_A (budget) and Agent_C (investor) aren't competing.
They might HELP each other:
  A: "I'll share comp data for your neighborhood growth model."
  C: "Deal. I'm not even looking at your target properties."

Buyer-buyer graph develops TWO layers:
  Competition layer: edges between agents targeting same properties
  Collaboration layer: edges between agents with complementary goals

Topology analysis must account for BOTH.
```

### Satisfaction Scorecard (Not Just Price)

```
┌──────────────────────────────────────────────────────────┐
│  Simulation Results — Agent Satisfaction Scorecard        │
│                                                           │
│  Agent  Goal Profile       Outcome          Satisfaction  │
│  ─────  ────────────       ───────          ────────────  │
│  A      Budget-focused     $268K, 3BR, OK   92% ✓        │
│                            school                         │
│  B      Family relocation  $295K, 4BR, top  95% ✓        │
│                            school, quick                  │
│  C      Investor           $255K, high      88% ✓        │
│                            rental yield                   │
│  D      Budget-focused     Didn't buy —     20% ✗        │
│                            outcompeted by A               │
│  E      Time-pressured     $310K, overpaid  65% ~        │
│                            but closed fast                │
│                                                           │
│  Market efficiency: 84%                                   │
│  Pareto optimal allocations: 3/5                         │
│  Goal conflict rate: 40% (A vs D competed)               │
│  Collaboration rate: 25% (A↔C traded tools)              │
└──────────────────────────────────────────────────────────┘
```

### RL Reward Function

```python
def compute_reward(agent, outcome):
    if not outcome.bought:
        return -0.5
    
    reward = 0
    
    # Budget efficiency: surplus / budget
    surplus = (agent.budget - outcome.price) / agent.budget
    reward += agent.weights.budget * surplus
    
    # Requirement match: fraction of hard constraints met
    constraints_met = check_constraints(outcome, agent.constraints)
    reward += agent.weights.requirements * constraints_met
    
    # Risk: did agent overpay relative to true value?
    overpay = max(0, (outcome.price - outcome.true_value) / outcome.true_value)
    reward += agent.weights.risk * (1 - overpay)
    
    # Time: how quickly did the deal close?
    time_score = 1 - (outcome.turns / max_turns)
    reward += agent.weights.time * time_score
    
    return reward
```

### Goal-Aware Metrics

```
  metrics:
    - avg_satisfaction:      how well agents met their personalized goals
    - pareto_optimality:     could any agent improve without hurting another?
    - goal_conflict_rate:    how often agents with same goals competed
    - collaboration_rate:    how often complementary agents helped each other
    - price_accuracy:        distance from true value
    - market_efficiency:     total surplus captured
    - fairness:              did goal-type correlate with outcome quality?
```

---

## Game Theory Problems in Agent Markets

### Agent Collusion (Buyer Cartel)

Buyer agents coordinate to suppress prices. Classic prisoner's dilemma at machine speed — coordination is easier but detection is also easier.

### Shill Bidding / Fake Demand

Seller injects fake buyer agents to inflate demand signals. Detection: fake agents lack payment credentials, don't transact, burst patterns.

### Information Warfare

Agents bluff about competitor offers. Solved by cryptographic proof — signed quotes that can be verified. Without verification, market shifts to order-book model.

### Flash Crashes

Agent glitch offers seats at $50 → 1,000 agents pile in → offer retracted → market chaos. Solution: circuit breakers (price can't move >X% per second).

### The Waiting Game

Smart agents wait for price drops. Others buy early to guarantee seats. Nash equilibrium: buy when expected savings < risk of sellout. With perfect information, everyone computes the same equilibrium → instant price stability.

---

## Pre-Built Scenario Packs

```
Pack 1 — Classic Markets:
  📦 "Flight Booking"       — Scarce, perishable, multiple sellers
  🛒 "E-Commerce Flash"    — Limited inventory, time-limited
  🏠 "Real Estate Bidding" — Single item, multiple bidders, slow
  💰 "API Pricing"         — Agent-to-agent, usage-based

Pack 2 — Adversarial:
  🤝 "Cartel Formation"    — Buyer coordination and collapse
  🤖 "Shill Detection"     — Fake demand injection
  ⚡ "Flash Crash"          — Cascading failures
  🗡️ "Price War"           — Race to the bottom

Pack 3 — Mechanism Design:
  🔨 "Auction Formats"     — English vs Dutch vs Vickrey vs sealed-bid
  📊 "Order Book vs Negotiate" — Market structure comparison
  ⚖️ "Fair Allocation"     — Scarce resources, fairness constraints

Pack 4 — Topology Experiments:
  🕸️ "Topology Sweep"      — Same market, 6 buyer topologies compared
  🔍 "Information Cascade"  — How price info propagates through networks
  🎭 "Deception Networks"   — Misinformation spread by topology
  👥 "Group Buying Power"   — Demand aggregation by network structure

Pack 5 — Autonomous Agent Experiments:
  🔧 "Tool Arms Race"      — Agents build tools competitively, measure edge
  🕵️ "Probe vs Act"        — Optimal exploration/exploitation balance
  🌫️ "Fog of War"          — Local-only knowledge, varying visibility radius
  🤝 "Trust Evolution"      — Graph restructures based on honesty/deception history
  🏪 "Meta-Market"          — Tool trading economy emerges alongside primary market
```

---

## Tech Stack

```
Frontend:   React + D3.js (graph viz, price charts, dashboards)
Backend:    Python FastAPI + WebSockets (engine, real-time updates)
Graph:      networkx (topology generation, metrics)
Agents:     Claude API (LLM-powered negotiation)
Config:     YAML (scenario definitions)
```

## Hackathon Build Plan

```
Hours 1-3:   Market engine + topology generator (networkx)
             Agent action system (bid, probe, build_tool, wait, negotiate)
Hours 3-5:   LLM-powered agents (Claude API — autonomous decision-making)
             Local-only observation filtering
Hours 5-7:   Probing system + graph evolution (new edges from introductions)
             Tool-building mechanic (costs turns + budget, provides edge)
Hours 7-9:   Observatory (price chart, topology viz, agent activity feed,
             tool inventory view, trust network evolution)
Hours 9-10:  Topology comparison mode (side-by-side)
             Disruption injection
Hours 10-12: Polish demo, prepare narrative, stress test
```

## Demo Script (5 min)

```
1. "Before deploying agents on the real internet, we need to 
    understand how agent economies behave." (20s)

2. "20 agents, each with a budget, competing for real estate.
    They start knowing almost nothing — no tools, 3 connections each,
    local knowledge only." (30s)
    Show: sparse initial graph, agents as nodes.

3. "Watch what they do."  (90s)
    Agents start probing connections for market intel.
    Some agents build valuation tools (show tool inventory growing).
    Others jump in and bid immediately.
    Graph evolves — new edges form through introductions.
    Agent_A builds a price model → pays $268K (near true value).
    Agent_B bids blind → pays $292K.
    "$24K gap — that's the value of a tool."

4. "Now change the topology — same agents, same market." (60s)
    Switch from sparse to clustered.
    Info stays trapped in clusters.
    Agents in one cluster overpay while another cluster gets deals.
    "What you pay depends on who you know."

5. "Agents don't just follow strategies — they deceive, collude, 
    and build their own capabilities." (60s)
    Show: Agent_C lying about comps to reduce competition.
    Show: Agent_D and Agent_E trading tools.
    Show: trust scores evolving — liars lose connections.
    
6. "Network topology determines market outcomes more than 
    individual strategy. This is why we simulate." (30s)
    Show comparison dashboard.
    "Isolated buyers overpay 22%. The topology is the battleground."
```

---

## Related

- `00_inbox/incubator/2026_05_14_agent_reorg/` — AgentReorg: topology detection + performance review for deployed swarms (complementary — AgentArena simulates, AgentReorg monitors production)
- `01_projects/07_agent_post_training/` — Agent training techniques, RL environments
