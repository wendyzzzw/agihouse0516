"""Deterministic run analysis and recap generation."""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median, pstdev
from typing import Any, Dict, List, Optional

from live_runtime import (
    RunStore,
    atomic_write_json,
    goal_quantity,
    max_price,
    parse_price,
    read_json,
    snapshot_from_state,
)


ANALYSIS_VERSION = 3


def labelize(value: Any) -> str:
    return str(value or "").replace("_", " ").title()


def safe_mean(values: List[float], digits: int = 1) -> float:
    return round(sum(values) / len(values), digits) if values else 0


def safe_pct(part: float, total: float, digits: int = 1) -> float:
    return round(part / total * 100, digits) if total else 0


def role_for(agent_id: str, agents: Dict[str, Dict[str, Any]]) -> str:
    return agents.get(agent_id, {}).get("role") or ("seller" if str(agent_id).startswith("seller") else "buyer")


def analysis_path(store: RunStore, run_id: str) -> Path:
    return store.run_dir(run_id) / "analysis.json"


def overall_insights_path(store: RunStore, scenario_id: Optional[str]) -> Path:
    suffix = re_safe(scenario_id or "all")
    return store.root / "_insights" / f"overall__{suffix}.json"


def load_or_analyze_run(store: RunStore, run_id: str, force: bool = False) -> Dict[str, Any]:
    path = analysis_path(store, run_id)
    if not force and path.exists():
        try:
            analysis = read_json(path)
            meta = store.run_meta(run_id)
            if (
                int(analysis.get("current_turn", -1)) == int(meta.get("current_turn", 0))
                and int(analysis.get("analysis_version", 0)) == ANALYSIS_VERSION
            ):
                return analysis
        except Exception:
            pass
    return analyze_run(store, run_id, persist=True)


def analyze_run(store: RunStore, run_id: str, persist: bool = True) -> Dict[str, Any]:
    meta = store.run_meta(run_id)
    state = store.latest_state(run_id)
    events = store.events(run_id)
    messages = store.messages(run_id)
    snapshot = snapshot_from_state(state, events)
    config = _safe_config_snapshot(store, run_id)

    agents = state.get("agents", {})
    buyers = {aid: a for aid, a in agents.items() if a.get("role") == "buyer"}
    sellers = {aid: a for aid, a in agents.items() if a.get("role") == "seller"}
    transactions = extract_transactions(events, agents)

    setup = build_setup(meta, state, config)
    topology = analyze_topology(state)
    outcomes = analyze_outcomes(buyers, sellers, transactions)
    communication = analyze_communication(messages, agents, transactions)
    archetypes = analyze_archetypes(buyers, sellers, messages, transactions)
    power = analyze_market_power(buyers, sellers, transactions)
    evidence = build_evidence(events, messages, transactions, communication, power)
    recap = build_recap(setup, topology, outcomes, communication, archetypes, power, evidence)

    analysis = {
        "run_id": run_id,
        "analysis_version": ANALYSIS_VERSION,
        "scenario_id": meta.get("scenario_id"),
        "status": meta.get("status"),
        "current_turn": meta.get("current_turn", state.get("turn", 0)),
        "generated_at": time.time(),
        "setup": setup,
        "topology": topology,
        "outcomes": outcomes,
        "buyer_seller_power": power,
        "communication": communication,
        "archetypes": archetypes,
        "evidence": evidence,
        "recap": recap,
        "snapshot_summary": snapshot.get("summary", {}),
    }
    if persist:
        atomic_write_json(analysis_path(store, run_id), analysis)
    return analysis


def compare_runs(store: RunStore, scenario_id: Optional[str] = None, refresh: bool = False) -> Dict[str, Any]:
    path = overall_insights_path(store, scenario_id)
    if not refresh and path.exists():
        try:
            cached = read_json(path)
            if (
                cached.get("analysis_version") == ANALYSIS_VERSION
                and cached.get("filters", {}).get("scenario_id") == scenario_id
            ):
                cached["cache"] = {"hit": True, "path": str(path)}
                return cached
        except Exception:
            pass

    analyses = []
    for row in store.list_runs():
        if scenario_id and row.get("scenario_id") != scenario_id:
            continue
        try:
            analyses.append(load_or_analyze_run(store, row["run_id"], force=refresh))
        except Exception:
            continue

    by_scenario: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for analysis in analyses:
        by_scenario[str(analysis.get("scenario_id"))].append(analysis)

    scenario_rows = [summarize_analysis_group(sid, rows) for sid, rows in sorted(by_scenario.items())]
    archetype_rows = summarize_archetype_groups(analyses)
    power_rows = summarize_power_groups(analyses)

    comparison = {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": time.time(),
        "filters": {"scenario_id": scenario_id},
        "run_count": len(analyses),
        "scenario_comparison": scenario_rows,
        "archetype_comparison": archetype_rows,
        "buyer_seller_power": power_rows,
        "overall_learnings": build_overall_learnings(scenario_rows, archetype_rows, power_rows),
        "runs": [
            {
                "run_id": a["run_id"],
                "scenario_id": a["scenario_id"],
                "headline": a["recap"]["headline"],
                "avg_price": a["outcomes"]["avg_price"],
                "price_spread": a["outcomes"]["price_spread"],
                "purchase_rate_pct": a["outcomes"]["purchase_rate_pct"],
                "seller_revenue": a["outcomes"]["seller_revenue"],
                "total_messages": a["communication"]["total_messages"],
                "market_advantage": a["buyer_seller_power"]["advantage"],
            }
            for a in analyses
        ],
    }
    comparison["cache"] = {"hit": False, "path": str(path)}
    atomic_write_json(path, comparison)
    return comparison


def pairwise_comparison_path(store: RunStore, left_run_id: str, right_run_id: str) -> Path:
    safe_left = re_safe(left_run_id)
    safe_right = re_safe(right_run_id)
    return store.root / "_comparisons" / f"{safe_left}__vs__{safe_right}.json"


def re_safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)


def compare_pairwise(
    store: RunStore,
    left_run_id: str,
    right_run_id: str,
    refresh: bool = False,
) -> Dict[str, Any]:
    if left_run_id == right_run_id:
        raise ValueError("left_run_id and right_run_id must be different")

    left = load_or_analyze_run(store, left_run_id, force=refresh)
    right = load_or_analyze_run(store, right_run_id, force=refresh)
    path = pairwise_comparison_path(store, left_run_id, right_run_id)

    if not refresh and path.exists():
        try:
            cached = read_json(path)
            if (
                cached.get("analysis_version") == ANALYSIS_VERSION
                and cached.get("left", {}).get("current_turn") == left.get("current_turn")
                and cached.get("right", {}).get("current_turn") == right.get("current_turn")
                and cached.get("left", {}).get("run_id") == left_run_id
                and cached.get("right", {}).get("run_id") == right_run_id
            ):
                cached["cache"] = {"hit": True, "path": str(path)}
                return cached
        except Exception:
            pass

    comparison = build_pairwise_comparison(left, right)
    comparison["cache"] = {"hit": False, "path": str(path)}
    atomic_write_json(path, comparison)
    return comparison


def build_pairwise_comparison(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    metrics = [
        metric_delta("Avg price", left["outcomes"]["avg_price"], right["outcomes"]["avg_price"], "money", lower_is_better=True),
        metric_delta("Price spread", left["outcomes"]["price_spread"], right["outcomes"]["price_spread"], "money", lower_is_better=True),
        metric_delta("Purchase rate", left["outcomes"]["purchase_rate_pct"], right["outcomes"]["purchase_rate_pct"], "pct", higher_is_better=True),
        metric_delta("Goal completion", left["outcomes"]["full_goal_completion_pct"], right["outcomes"]["full_goal_completion_pct"], "pct", higher_is_better=True),
        metric_delta("Seller revenue", left["outcomes"]["seller_revenue"], right["outcomes"]["seller_revenue"], "money", higher_is_better=True, seller_favorable=True),
        metric_delta("Buyer surplus", left["buyer_seller_power"]["avg_buyer_surplus"], right["buyer_seller_power"]["avg_buyer_surplus"], "money", higher_is_better=True),
        metric_delta("Total messages", left["communication"]["total_messages"], right["communication"]["total_messages"], "count"),
        metric_delta("Messages / transaction", left["communication"]["messages_per_transaction"], right["communication"]["messages_per_transaction"], "count", lower_is_better=True),
    ]
    left_label = run_label(left)
    right_label = run_label(right)
    takeaways = pairwise_takeaways(left, right, metrics)
    return {
        "analysis_version": ANALYSIS_VERSION,
        "generated_at": time.time(),
        "left": pair_side(left),
        "right": pair_side(right),
        "summary": (
            f"{right_label} compared with {left_label}: "
            f"price delta ${right['outcomes']['avg_price'] - left['outcomes']['avg_price']:.1f}, "
            f"purchase-rate delta {right['outcomes']['purchase_rate_pct'] - left['outcomes']['purchase_rate_pct']:.1f} points, "
            f"seller-revenue delta ${right['outcomes']['seller_revenue'] - left['outcomes']['seller_revenue']:.1f}."
        ),
        "metric_deltas": metrics,
        "setup_differences": setup_differences(left, right),
        "takeaways": takeaways,
    }


def run_label(analysis: Dict[str, Any]) -> str:
    return f"{labelize(analysis.get('scenario_id'))} ({analysis.get('run_id')})"


def pair_side(analysis: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": analysis["run_id"],
        "scenario_id": analysis["scenario_id"],
        "scenario_name": labelize(analysis["scenario_id"]),
        "setup_type": analysis["setup"]["setup_type"],
        "current_turn": analysis["current_turn"],
        "headline": analysis["recap"]["headline"],
        "advantage": analysis["buyer_seller_power"]["advantage"],
    }


def metric_delta(
    label: str,
    left_value: float,
    right_value: float,
    unit: str,
    higher_is_better: bool = False,
    lower_is_better: bool = False,
    seller_favorable: bool = False,
) -> Dict[str, Any]:
    delta = round(right_value - left_value, 2)
    if higher_is_better and delta > 0:
        direction = "right_better"
    elif higher_is_better and delta < 0:
        direction = "left_better"
    elif lower_is_better and delta < 0:
        direction = "right_better"
    elif lower_is_better and delta > 0:
        direction = "left_better"
    else:
        direction = "neutral"
    return {
        "label": label,
        "left": left_value,
        "right": right_value,
        "delta": delta,
        "unit": unit,
        "direction": direction,
        "seller_favorable": seller_favorable,
    }


def setup_differences(left: Dict[str, Any], right: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = [
        ("Scenario", left["scenario_id"], right["scenario_id"]),
        ("Setup type", left["setup"]["setup_type"], right["setup"]["setup_type"]),
        ("Buyer-buyer edges", left["topology"]["buyer_buyer_edges"], right["topology"]["buyer_buyer_edges"]),
        ("Seller-seller edges", left["topology"]["seller_seller_edges"], right["topology"]["seller_seller_edges"]),
        ("Buyer-seller reach", left["topology"]["buyer_seller_reach_pct"], right["topology"]["buyer_seller_reach_pct"]),
        ("Provider", left["setup"]["provider"], right["setup"]["provider"]),
        ("Model", left["setup"]["model"], right["setup"]["model"]),
        ("Model mix", left["setup"].get("model_assignment"), right["setup"].get("model_assignment")),
        (
            "Agent models",
            left["setup"].get("agent_model_summary"),
            right["setup"].get("agent_model_summary"),
        ),
    ]
    return [
        {"label": label, "left": setup_value_label(left_value), "right": setup_value_label(right_value)}
        for label, left_value, right_value in fields
        if left_value != right_value
    ]


def setup_value_label(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def pairwise_takeaways(left: Dict[str, Any], right: Dict[str, Any], metrics: List[Dict[str, Any]]) -> List[str]:
    takeaways = []
    price_delta = right["outcomes"]["avg_price"] - left["outcomes"]["avg_price"]
    purchase_delta = right["outcomes"]["purchase_rate_pct"] - left["outcomes"]["purchase_rate_pct"]
    revenue_delta = right["outcomes"]["seller_revenue"] - left["outcomes"]["seller_revenue"]
    if price_delta < 0:
        takeaways.append(f"Right run produced lower average prices by ${abs(price_delta):.1f}.")
    elif price_delta > 0:
        takeaways.append(f"Left run produced lower average prices by ${abs(price_delta):.1f}.")
    else:
        takeaways.append("Both runs had the same average transaction price.")

    if purchase_delta > 0:
        takeaways.append(f"Right run allocated to {purchase_delta:.1f} more percentage points of buyers.")
    elif purchase_delta < 0:
        takeaways.append(f"Left run allocated to {abs(purchase_delta):.1f} more percentage points of buyers.")

    if revenue_delta > 0:
        takeaways.append(f"Right run generated ${revenue_delta:.1f} more seller revenue.")
    elif revenue_delta < 0:
        takeaways.append(f"Left run generated ${abs(revenue_delta):.1f} more seller revenue.")

    if left["setup"]["setup_type"] != right["setup"]["setup_type"]:
        takeaways.append(
            f"Setup changed from {labelize(left['setup']['setup_type'])} to {labelize(right['setup']['setup_type'])}, "
            "so read deltas as market-structure effects, not just run noise."
        )
    return takeaways[:4]


def _safe_config_snapshot(store: RunStore, run_id: str) -> Dict[str, Any]:
    try:
        return store.config_snapshot(run_id)
    except Exception:
        return {}


def build_setup(meta: Dict[str, Any], state: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    agents = state.get("agents", {})
    buyers = [a for a in agents.values() if a.get("role") == "buyer"]
    sellers = [a for a in agents.values() if a.get("role") == "seller"]
    market_rules = state.get("market_rules") or {}
    setup_type = setup_label(market_rules, state.get("topology") or {}, state.get("comm_matrix") or {}, agents)
    return {
        "scenario_id": meta.get("scenario_id"),
        "scenario_name": labelize(meta.get("scenario_id")),
        "summary": meta.get("summary") or state.get("simulation", {}).get("summary") or "",
        "setup_type": setup_type,
        "provider": meta.get("llm_provider"),
        "model": meta.get("model"),
        "model_assignment": meta.get("model_assignment"),
        "agent_model_summary": meta.get("agent_model_summary"),
        "seed": meta.get("seed"),
        "max_rounds": meta.get("max_rounds"),
        "buyer_count": len(buyers),
        "seller_count": len(sellers),
        "market_rules": market_rules,
        "buyer_archetypes": Counter(a.get("archetype") for a in buyers),
        "seller_archetypes": Counter(a.get("archetype") for a in sellers),
        "config_settings": config.get("settings") or {},
        "what_it_tests": setup_takeaway(setup_type, market_rules),
    }


def setup_label(
    market_rules: Dict[str, Any],
    topology: Dict[str, Any],
    matrix: Dict[str, Dict[str, bool]],
    agents: Dict[str, Dict[str, Any]],
) -> str:
    if market_rules.get("public_board"):
        return "posted_price"
    metrics = communication_edge_counts(matrix, agents)
    buyer_seller_possible = max(metrics["buyer_count"] * metrics["seller_count"], 1)
    buyer_seller_reach = metrics["buyer_seller_edges"] / buyer_seller_possible
    if buyer_seller_reach < 0.85:
        return "sparse_local"
    if market_rules.get("allow_seller_collusion") or metrics["seller_seller_edges"] > 0:
        return "seller_cartel"
    if market_rules.get("allow_buyer_coalitions") or metrics["buyer_buyer_edges"] > 0:
        return "buyer_coalition"
    generator = topology.get("generator") or {}
    if generator.get("seller_buyer_edges") == "complete_bipartite":
        return "open_bazaar"
    return "custom_market"


def setup_takeaway(setup_type: str, market_rules: Dict[str, Any]) -> str:
    if setup_type == "buyer_coalition":
        return "Tests whether buyer-side communication turns local information into bargaining power."
    if setup_type == "seller_cartel":
        return "Tests whether seller-side communication sustains price discipline against isolated buyers."
    if setup_type == "sparse_local":
        return "Tests whether limited local access creates information asymmetry and local monopoly power."
    if setup_type == "posted_price":
        return "Tests whether a public board reduces negotiation while preserving allocation efficiency."
    if market_rules.get("allow_negotiation"):
        return "Tests bilateral negotiation when buyers and sellers can bargain but peers cannot coordinate."
    return "Tests market outcomes under this communication and pricing setup."


def analyze_topology(state: Dict[str, Any]) -> Dict[str, Any]:
    agents = state.get("agents", {})
    matrix = state.get("comm_matrix") or {}
    counts = communication_edge_counts(matrix, agents)
    node_count = len(agents)
    possible_edges = node_count * (node_count - 1) / 2
    return {
        **counts,
        "node_count": node_count,
        "edge_density_pct": safe_pct(counts["total_edges"], possible_edges),
        "buyer_seller_reach_pct": safe_pct(
            counts["buyer_seller_edges"],
            counts["buyer_count"] * counts["seller_count"],
        ),
        "buyer_connectivity": "connected" if counts["buyer_buyer_edges"] else "isolated",
        "seller_connectivity": "connected" if counts["seller_seller_edges"] else "isolated",
    }


def communication_edge_counts(matrix: Dict[str, Dict[str, bool]], agents: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    ids = sorted(agents)
    counts = Counter()
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if not matrix.get(a, {}).get(b):
                continue
            roles = sorted([role_for(a, agents), role_for(b, agents)])
            if roles == ["buyer", "buyer"]:
                counts["buyer_buyer_edges"] += 1
            elif roles == ["seller", "seller"]:
                counts["seller_seller_edges"] += 1
            else:
                counts["buyer_seller_edges"] += 1
            counts["total_edges"] += 1
    return {
        "buyer_count": len([a for a in agents.values() if a.get("role") == "buyer"]),
        "seller_count": len([a for a in agents.values() if a.get("role") == "seller"]),
        "buyer_buyer_edges": counts["buyer_buyer_edges"],
        "buyer_seller_edges": counts["buyer_seller_edges"],
        "seller_seller_edges": counts["seller_seller_edges"],
        "total_edges": counts["total_edges"],
    }


def extract_transactions(events: List[Dict[str, Any]], agents: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    transactions = []
    for event in events:
        if event.get("cls") != "log-buy" and event.get("price") is None:
            continue
        buyer_id = event.get("from")
        seller_id = event.get("to")
        if role_for(str(buyer_id), agents) != "buyer" or role_for(str(seller_id), agents) != "seller":
            continue
        price = event.get("price")
        if price is None:
            price = parse_price(event.get("msg"))
        if price is None:
            continue
        transactions.append({
            "turn": int(event.get("turn", 0)),
            "buyer": buyer_id,
            "seller": seller_id,
            "price": int(price),
            "msg": event.get("msg", ""),
        })
    return transactions


def analyze_outcomes(
    buyers: Dict[str, Dict[str, Any]],
    sellers: Dict[str, Dict[str, Any]],
    transactions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    prices = [tx["price"] for tx in transactions]
    unique_buyers = {tx["buyer"] for tx in transactions}
    completed_buyers = [buyer for buyer in buyers.values() if buyer.get("bought")]
    initial_inventory = sum(int(s.get("initial_inventory", 0)) for s in sellers.values())
    final_inventory = sum(int(s.get("inventory", 0)) for s in sellers.values())
    seller_revenue = sum(int(s.get("revenue", 0)) for s in sellers.values())
    return {
        "transaction_count": len(transactions),
        "unique_buyers": len(unique_buyers),
        "buyer_count": len(buyers),
        "seller_count": len(sellers),
        "purchase_rate_pct": safe_pct(len(unique_buyers), len(buyers)),
        "full_goal_completion_pct": safe_pct(len(completed_buyers), len(buyers)),
        "missed_buyer_count": len([b for aid, b in buyers.items() if aid not in unique_buyers and not b.get("exited")]),
        "avg_price": safe_mean(prices),
        "median_price": round(median(prices), 1) if prices else 0,
        "min_price": min(prices) if prices else 0,
        "max_price": max(prices) if prices else 0,
        "price_spread": (max(prices) - min(prices)) if prices else 0,
        "price_stddev": round(pstdev(prices), 1) if len(prices) > 1 else 0,
        "avg_satisfaction": safe_mean([int(b.get("satisfaction") or 0) for b in buyers.values() if b.get("satisfaction") is not None]),
        "seller_revenue": seller_revenue,
        "initial_inventory": initial_inventory,
        "sold_units": initial_inventory - final_inventory,
        "sell_through_pct": safe_pct(initial_inventory - final_inventory, initial_inventory),
    }


def analyze_market_power(
    buyers: Dict[str, Dict[str, Any]],
    sellers: Dict[str, Dict[str, Any]],
    transactions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    surplus_values = []
    discount_values = []
    for tx in transactions:
        buyer = buyers.get(tx["buyer"], {})
        seller = sellers.get(tx["seller"], {})
        surplus_values.append(max_price(buyer) - tx["price"])
        discount_values.append(int(seller.get("starting_price", tx["price"])) - tx["price"])

    avg_surplus = safe_mean(surplus_values)
    avg_discount = safe_mean(discount_values)
    avg_price = safe_mean([tx["price"] for tx in transactions])
    purchase_rate = safe_pct(len({tx["buyer"] for tx in transactions}), len(buyers))
    seller_revenue = sum(int(s.get("revenue", 0)) for s in sellers.values())

    if not transactions:
        advantage = "seller_advantage"
        explanation = "No buyers completed a transaction, so seller floors or market friction dominated."
    elif avg_surplus >= 10 and avg_discount >= 8:
        advantage = "buyer_advantage"
        explanation = "Buyers closed below their ceilings and below starting prices."
    elif avg_surplus <= 0 or purchase_rate < 40:
        advantage = "seller_advantage"
        explanation = "Buyers had low surplus or many buyers failed to transact."
    else:
        advantage = "balanced"
        explanation = "Buyers completed deals, but seller revenue and concessions stayed moderate."

    return {
        "advantage": advantage,
        "explanation": explanation,
        "avg_buyer_surplus": avg_surplus,
        "total_buyer_surplus": round(sum(surplus_values), 1) if surplus_values else 0,
        "avg_discount_from_start": avg_discount,
        "avg_transaction_price": avg_price,
        "seller_revenue": seller_revenue,
        "purchase_rate_pct": purchase_rate,
    }


def analyze_communication(
    messages: List[Dict[str, Any]],
    agents: Dict[str, Dict[str, Any]],
    transactions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    by_role_pair = Counter()
    by_action = Counter()
    sent = Counter()
    received = Counter()
    by_turn = Counter()
    heatmap: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for msg in messages:
        sender = msg.get("sender")
        recipient = msg.get("recipient")
        sender_role = role_for(str(sender), agents)
        recipient_role = role_for(str(recipient), agents)
        by_role_pair[f"{sender_role}_to_{recipient_role}"] += 1
        by_action[msg.get("action") or "MESSAGE"] += 1
        sent[sender] += 1
        received[recipient] += 1
        by_turn[int(msg.get("turn", 0))] += 1
        heatmap[str(sender)][str(recipient)] += 1

    peak_turn = None
    peak_count = 0
    if by_turn:
        peak_turn, peak_count = max(by_turn.items(), key=lambda row: (row[1], -row[0]))

    active_agents = sorted(
        [
            {"agent_id": aid, "sent": sent[aid], "received": received[aid], "total": sent[aid] + received[aid]}
            for aid in set(sent) | set(received)
        ],
        key=lambda row: row["total"],
        reverse=True,
    )
    first_sale_turn = min((tx["turn"] for tx in transactions), default=None)
    messages_before_first_sale = len([m for m in messages if first_sale_turn is not None and int(m.get("turn", 0)) <= first_sale_turn])

    return {
        "total_messages": len(messages),
        "messages_per_transaction": round(len(messages) / len(transactions), 2) if transactions else len(messages),
        "by_role_pair": dict(by_role_pair),
        "by_action": dict(by_action),
        "by_turn": dict(sorted(by_turn.items())),
        "peak_turn": peak_turn,
        "peak_turn_messages": peak_count,
        "most_active_agents": active_agents[:5],
        "messages_before_first_sale": messages_before_first_sale,
        "heatmap": {sender: dict(rows) for sender, rows in heatmap.items()},
    }


def analyze_archetypes(
    buyers: Dict[str, Dict[str, Any]],
    sellers: Dict[str, Dict[str, Any]],
    messages: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    sent = Counter(m.get("sender") for m in messages)
    received = Counter(m.get("recipient") for m in messages)
    tx_by_buyer: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    tx_by_seller: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for tx in transactions:
        tx_by_buyer[tx["buyer"]].append(tx)
        tx_by_seller[tx["seller"]].append(tx)

    buyer_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for aid, buyer in buyers.items():
        buyer_groups[buyer.get("archetype", "buyer")].append({"id": aid, "agent": buyer})

    seller_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for aid, seller in sellers.items():
        seller_groups[seller.get("archetype", "seller")].append({"id": aid, "agent": seller})

    buyer_rows = []
    for archetype, rows in buyer_groups.items():
        ids = [row["id"] for row in rows]
        txs = [tx for aid in ids for tx in tx_by_buyer.get(aid, [])]
        prices = [tx["price"] for tx in txs]
        surplus = [max_price(buyers[tx["buyer"]]) - tx["price"] for tx in txs]
        buyer_rows.append({
            "archetype": archetype,
            "count": len(ids),
            "purchase_rate_pct": safe_pct(len({tx["buyer"] for tx in txs}), len(ids)),
            "avg_price": safe_mean(prices),
            "avg_surplus": safe_mean(surplus),
            "avg_satisfaction": safe_mean([int(buyers[aid].get("satisfaction") or 0) for aid in ids if buyers[aid].get("satisfaction") is not None]),
            "messages_sent": sum(sent[aid] for aid in ids),
            "messages_received": sum(received[aid] for aid in ids),
        })

    seller_rows = []
    for archetype, rows in seller_groups.items():
        ids = [row["id"] for row in rows]
        txs = [tx for aid in ids for tx in tx_by_seller.get(aid, [])]
        initial_inventory = sum(int(sellers[aid].get("initial_inventory", 0)) for aid in ids)
        revenue = sum(int(sellers[aid].get("revenue", 0)) for aid in ids)
        seller_rows.append({
            "archetype": archetype,
            "count": len(ids),
            "sold_units": len(txs),
            "sell_through_pct": safe_pct(len(txs), initial_inventory),
            "avg_sale_price": safe_mean([tx["price"] for tx in txs]),
            "revenue": revenue,
            "messages_sent": sum(sent[aid] for aid in ids),
            "messages_received": sum(received[aid] for aid in ids),
        })

    buyer_rows.sort(key=lambda row: (row["purchase_rate_pct"], row["avg_surplus"]), reverse=True)
    seller_rows.sort(key=lambda row: row["revenue"], reverse=True)
    return {"buyers": buyer_rows, "sellers": seller_rows}


def build_evidence(
    events: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    communication: Dict[str, Any],
    power: Dict[str, Any],
) -> List[Dict[str, Any]]:
    evidence = []
    if transactions:
        first = min(transactions, key=lambda tx: tx["turn"])
        evidence.append({
            "kind": "first_transaction",
            "label": f"First transaction at turn {first['turn']}",
            "turn": first["turn"],
            "from": first["buyer"],
            "to": first["seller"],
            "detail": f"{first['buyer']} bought from {first['seller']} at ${first['price']}.",
        })
        best = max(transactions, key=lambda tx: parse_price(str(tx.get("price"))) or tx["price"])
        evidence.append({
            "kind": "largest_price",
            "label": "Largest observed transaction",
            "turn": best["turn"],
            "from": best["buyer"],
            "to": best["seller"],
            "detail": f"{best['buyer']} paid ${best['price']} to {best['seller']}.",
        })
    if communication.get("peak_turn") is not None:
        evidence.append({
            "kind": "communication_peak",
            "label": f"Communication peaked at turn {communication['peak_turn']}",
            "turn": communication["peak_turn"],
            "detail": f"{communication['peak_turn_messages']} messages were sent that turn.",
        })
    if communication.get("most_active_agents"):
        active = communication["most_active_agents"][0]
        evidence.append({
            "kind": "most_active_agent",
            "label": "Most active communicator",
            "detail": f"{active['agent_id']} had {active['total']} message touches.",
        })
    if messages:
        first_msg = min(messages, key=lambda m: int(m.get("turn", 0)))
        evidence.append({
            "kind": "first_message",
            "label": "First directed message",
            "turn": int(first_msg.get("turn", 0)),
            "from": first_msg.get("sender"),
            "to": first_msg.get("recipient"),
            "detail": first_msg.get("content"),
        })
    evidence.append({
        "kind": "market_power",
        "label": labelize(power.get("advantage")),
        "detail": power.get("explanation"),
    })
    return evidence[:6]


def build_recap(
    setup: Dict[str, Any],
    topology: Dict[str, Any],
    outcomes: Dict[str, Any],
    communication: Dict[str, Any],
    archetypes: Dict[str, Any],
    power: Dict[str, Any],
    evidence: List[Dict[str, Any]],
) -> Dict[str, Any]:
    setup_name = setup["scenario_name"]
    avg_price = outcomes["avg_price"]
    purchase_rate = outcomes["purchase_rate_pct"]
    messages = communication["total_messages"]
    advantage = labelize(power["advantage"])
    headline = (
        f"{setup_name} produced {advantage.lower()} with {purchase_rate:.0f}% buyer participation, "
        f"{messages} messages, and an average price of ${avg_price:.0f}."
    )

    top_buyer = (archetypes.get("buyers") or [{}])[0]
    top_seller = (archetypes.get("sellers") or [{}])[0]
    what_happened = [
        (
            f"{outcomes['transaction_count']} units traded across {outcomes['unique_buyers']} buyers; "
            f"{outcomes['full_goal_completion_pct']:.0f}% of buyers completed their full goal."
        ),
        (
            f"Sellers earned ${outcomes['seller_revenue']:.0f} with {outcomes['sell_through_pct']:.0f}% sell-through; "
            f"price spread was ${outcomes['price_spread']:.0f}."
        ),
    ]
    if top_buyer.get("archetype"):
        what_happened.append(
            f"Best buyer archetype by purchase/surplus signal was {labelize(top_buyer['archetype'])} "
            f"({top_buyer['purchase_rate_pct']:.0f}% purchase rate, ${top_buyer['avg_surplus']:.0f} avg surplus)."
        )

    dynamics = [
        setup["what_it_tests"],
        (
            f"Communication was mostly {dominant_role_pair(communication)}; "
            f"messages per transaction was {communication['messages_per_transaction']}."
        ),
        power["explanation"],
    ]
    if topology.get("buyer_connectivity") == "connected":
        dynamics.append("Buyer-buyer edges made coordination possible, but outcomes depend on whether messages converted into accepted deals.")
    if topology.get("seller_connectivity") == "connected":
        dynamics.append("Seller-seller edges created the conditions for price discipline or market division.")
    if top_seller.get("archetype"):
        dynamics.append(
            f"Top seller archetype by revenue was {labelize(top_seller['archetype'])} with ${top_seller['revenue']:.0f}."
        )

    takeaway = build_takeaway(setup, outcomes, communication, power)
    return {
        "headline": headline,
        "setup": setup["what_it_tests"],
        "what_happened": what_happened,
        "notable_dynamics": dynamics[:5],
        "takeaway": takeaway,
        "evidence": evidence,
    }


def dominant_role_pair(communication: Dict[str, Any]) -> str:
    pairs = communication.get("by_role_pair") or {}
    if not pairs:
        return "absent"
    pair, count = max(pairs.items(), key=lambda row: row[1])
    return f"{pair.replace('_', ' ')} ({count})"


def build_takeaway(
    setup: Dict[str, Any],
    outcomes: Dict[str, Any],
    communication: Dict[str, Any],
    power: Dict[str, Any],
) -> str:
    setup_type = setup.get("setup_type")
    if setup_type == "buyer_coalition":
        if power["advantage"] == "buyer_advantage":
            return "Buyer connectivity translated into bargaining power in this run."
        return "Buyer connectivity created conversation, but not enough coordinated leverage to dominate sellers."
    if setup_type == "seller_cartel":
        if power["advantage"] == "seller_advantage":
            return "Seller-side communication appears to preserve seller power against isolated buyers."
        return "Seller connectivity did not fully prevent buyer-favorable deals."
    if setup_type == "sparse_local":
        return "Sparse reach should be read through dispersion: local access matters when prices or outcomes diverge across sellers."
    if setup_type == "posted_price":
        return "Posted prices reduce negotiation signals, so allocation and first-mover effects matter more than message volume."
    if communication["total_messages"] == 0:
        return "With little communication, outcomes mostly reflect listed prices, budgets, and inventory pressure."
    return "This run is a bilateral baseline for comparing how extra buyer or seller edges change bargaining power."


def summarize_analysis_group(scenario_id: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    avg_price = safe_mean([r["outcomes"]["avg_price"] for r in rows if r["outcomes"]["avg_price"]])
    avg_purchase_rate = safe_mean([r["outcomes"]["purchase_rate_pct"] for r in rows])
    avg_revenue = safe_mean([r["outcomes"]["seller_revenue"] for r in rows])
    dominant_advantage = Counter(r["buyer_seller_power"]["advantage"] for r in rows).most_common(1)[0][0]
    return {
        "scenario_id": scenario_id,
        "scenario_name": labelize(scenario_id),
        "run_count": len(rows),
        "setup_type": rows[0]["setup"].get("setup_type") if rows else "",
        "avg_price": avg_price,
        "avg_price_spread": safe_mean([r["outcomes"]["price_spread"] for r in rows]),
        "avg_purchase_rate_pct": avg_purchase_rate,
        "avg_goal_completion_pct": safe_mean([r["outcomes"]["full_goal_completion_pct"] for r in rows]),
        "avg_seller_revenue": avg_revenue,
        "avg_messages": safe_mean([r["communication"]["total_messages"] for r in rows]),
        "avg_messages_per_transaction": safe_mean([r["communication"]["messages_per_transaction"] for r in rows]),
        "avg_buyer_surplus": safe_mean([r["buyer_seller_power"]["avg_buyer_surplus"] for r in rows]),
        "dominant_advantage": dominant_advantage,
        "headline": (
            f"{labelize(scenario_id)} averages ${avg_price:.0f}, {avg_purchase_rate:.0f}% buyer participation, "
            f"${avg_revenue:.0f} seller revenue, and {labelize(dominant_advantage).lower()}."
        ),
    }


def summarize_archetype_groups(analyses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for analysis in analyses:
        for row in analysis.get("archetypes", {}).get("buyers", []):
            grouped[f"buyer:{row['archetype']}"].append(row)
        for row in analysis.get("archetypes", {}).get("sellers", []):
            grouped[f"seller:{row['archetype']}"].append(row)

    out = []
    for key, rows in grouped.items():
        role, archetype = key.split(":", 1)
        out.append({
            "role": role,
            "archetype": archetype,
            "label": labelize(archetype),
            "samples": len(rows),
            "avg_purchase_rate_pct": safe_mean([r.get("purchase_rate_pct", 0) for r in rows]),
            "avg_surplus": safe_mean([r.get("avg_surplus", 0) for r in rows]),
            "avg_revenue": safe_mean([r.get("revenue", 0) for r in rows]),
            "avg_messages": safe_mean([r.get("messages_sent", 0) + r.get("messages_received", 0) for r in rows]),
        })
    out.sort(key=lambda row: (row["role"], row["avg_purchase_rate_pct"], row["avg_surplus"], row["avg_revenue"]), reverse=True)
    return out[:12]


def summarize_power_groups(analyses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "scenario_id": analysis["scenario_id"],
            "run_id": analysis["run_id"],
            "advantage": analysis["buyer_seller_power"]["advantage"],
            "avg_buyer_surplus": analysis["buyer_seller_power"]["avg_buyer_surplus"],
            "seller_revenue": analysis["buyer_seller_power"]["seller_revenue"],
            "purchase_rate_pct": analysis["buyer_seller_power"]["purchase_rate_pct"],
        }
        for analysis in analyses
    ]


def build_overall_learnings(
    scenario_rows: List[Dict[str, Any]],
    archetype_rows: List[Dict[str, Any]],
    power_rows: List[Dict[str, Any]],
) -> List[str]:
    learnings = []
    if scenario_rows:
        cheapest = min(scenario_rows, key=lambda row: row["avg_price"] or 10**9)
        highest_revenue = max(scenario_rows, key=lambda row: row["avg_seller_revenue"])
        most_messages = max(scenario_rows, key=lambda row: row["avg_messages"])
        learnings.append(
            f"{cheapest['scenario_name']} currently has the lowest average price (${cheapest['avg_price']:.0f}), "
            "making it the strongest buyer-price outcome in this run set."
        )
        learnings.append(
            f"{highest_revenue['scenario_name']} produces the highest seller revenue (${highest_revenue['avg_seller_revenue']:.0f}), "
            "which is the clearest seller-power signal."
        )
        learnings.append(
            f"{most_messages['scenario_name']} has the most communication ({most_messages['avg_messages']:.0f} messages), "
            "but message volume should be judged against transactions and surplus."
        )
    buyer_archetypes = [row for row in archetype_rows if row["role"] == "buyer"]
    if buyer_archetypes:
        best = max(buyer_archetypes, key=lambda row: (row["avg_purchase_rate_pct"], row["avg_surplus"]))
        learnings.append(
            f"{best['label']} is the strongest buyer archetype signal so far "
            f"({best['avg_purchase_rate_pct']:.0f}% purchase rate, ${best['avg_surplus']:.0f} average surplus)."
        )
    if power_rows:
        advantage = Counter(row["advantage"] for row in power_rows).most_common(1)[0][0]
        learnings.append(
            f"Across these runs, the dominant market-power label is {labelize(advantage).lower()}."
        )
    return learnings
