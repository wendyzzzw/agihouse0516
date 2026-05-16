"""Communication-matrix builder aligned with the teammate schema.

A simulation's `topology` block can be one of:

  topology:
    input_type: generated
    generator:
      seller_buyer_edges:  complete_bipartite | none
      seller_seller_edges: complete | none
      buyer_buyer_edges:
        type: isolated | clustered | small_world | hub_spoke | fully_connected | none | complete
        cluster_size: <int, optional>
        long_range_edges: <int, optional>
        clusters: [[id, id, ...], ...]          # explicit
        bridge_edges: [[id, id], ...]           # explicit

  topology:
    input_type: edge_list
    edges:
      seller_buyer: [[seller_id, buyer_id], ...]
      buyer_buyer: [[buyer_id, buyer_id], ...]
      seller_seller: [[seller_id, seller_id], ...]

`build_matrix(sim, override_topology=None)` returns `(graph, matrix)` where
`matrix[a][b] -> bool`. The optional override replaces the buyer-buyer
generator's `type` (so the demo.html buttons can flip topology at runtime).
"""
from __future__ import annotations
import random
import networkx as nx
from typing import Any, Dict, List, Optional, Tuple

# Frontend-visible names (also the names accepted by --topology override).
SUPPORTED_BUYER_BUYER = [
    "isolated", "clustered", "small_world", "hub_spoke", "fully_connected",
    "none", "complete",
]


def build_matrix(
    sim: Dict[str, Any],
    override_buyer_buyer: Optional[str] = None,
    seed: int = 42,
) -> Tuple[nx.Graph, Dict[str, Dict[str, bool]]]:
    rng = random.Random(seed)
    buyer_ids = [b["id"] for b in sim["buyers"]]
    seller_ids = [s["id"] for s in sim["sellers"]]

    G = nx.Graph()
    G.add_nodes_from(buyer_ids + seller_ids)

    topo = sim.get("topology") or {}
    input_type = topo.get("input_type", "generated")

    if input_type == "edge_list":
        edges = topo.get("edges") or {}
        for a, b in edges.get("seller_buyer", []):
            G.add_edge(a, b)
        for a, b in edges.get("buyer_buyer", []):
            G.add_edge(a, b)
        for a, b in edges.get("seller_seller", []):
            G.add_edge(a, b)
    else:
        gen = topo.get("generator") or {}
        _apply_seller_buyer(G, gen.get("seller_buyer_edges", "complete_bipartite"),
                            buyer_ids, seller_ids)
        _apply_seller_seller(G, gen.get("seller_seller_edges", "none"), seller_ids)
        bb = gen.get("buyer_buyer_edges")
        bb = _normalise_bb(bb, override_buyer_buyer)
        _apply_buyer_buyer(G, bb, buyer_ids, rng)

    matrix = _to_matrix(G)
    return G, matrix


def _normalise_bb(bb: Any, override: Optional[str]) -> Dict[str, Any]:
    """Coerce buyer_buyer spec into a dict form: {type, cluster_size, ...}."""
    if isinstance(bb, str):
        bb = {"type": bb}
    elif bb is None:
        bb = {"type": "none"}
    else:
        bb = dict(bb)
    if override:
        bb["type"] = override
    return bb


def _apply_seller_buyer(G, mode: str, buyer_ids: List[str], seller_ids: List[str]):
    if mode == "complete_bipartite":
        for s in seller_ids:
            for b in buyer_ids:
                G.add_edge(s, b)
    # "none" → no edges


def _apply_seller_seller(G, mode: str, seller_ids: List[str]):
    if mode == "complete":
        for i in range(len(seller_ids)):
            for j in range(i + 1, len(seller_ids)):
                G.add_edge(seller_ids[i], seller_ids[j])


def _apply_buyer_buyer(G, bb: Dict[str, Any], buyer_ids: List[str], rng: random.Random):
    t = bb.get("type", "none")
    if t in ("none", "isolated"):
        return

    if t in ("complete", "fully_connected"):
        for i in range(len(buyer_ids)):
            for j in range(i + 1, len(buyer_ids)):
                G.add_edge(buyer_ids[i], buyer_ids[j])
        return

    if t == "hub_spoke":
        hub = buyer_ids[0]
        for b in buyer_ids[1:]:
            G.add_edge(hub, b)
        return

    if t == "clustered":
        # Explicit clusters list, else partition by cluster_size.
        clusters = bb.get("clusters")
        if not clusters:
            size = int(bb.get("cluster_size") or 5)
            clusters = [buyer_ids[i:i + size] for i in range(0, len(buyer_ids), size)]
        for cl in clusters:
            for i in range(len(cl)):
                for j in range(i + 1, len(cl)):
                    G.add_edge(cl[i], cl[j])
        for a, b in bb.get("bridge_edges", []) or []:
            G.add_edge(a, b)
        return

    if t == "small_world":
        # Clusters + a few random long-range ties.
        clusters = bb.get("clusters")
        if not clusters:
            size = int(bb.get("cluster_size") or 5)
            clusters = [buyer_ids[i:i + size] for i in range(0, len(buyer_ids), size)]
        for cl in clusters:
            for i in range(len(cl)):
                for j in range(i + 1, len(cl)):
                    G.add_edge(cl[i], cl[j])
        lr = int(bb.get("long_range_edges") or 3)
        for _ in range(lr):
            a, b = rng.sample(buyer_ids, 2)
            G.add_edge(a, b)
        return

    raise ValueError(f"unknown buyer_buyer_edges type: {t}")


def _to_matrix(G: nx.Graph) -> Dict[str, Dict[str, bool]]:
    nodes = sorted(G.nodes())
    m = {n: {x: False for x in nodes} for n in nodes}
    for a, b in G.edges():
        m[a][b] = True
        m[b][a] = True
    return m
