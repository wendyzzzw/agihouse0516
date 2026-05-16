"""Communication matrix generators for the 5 topologies the frontend supports."""
import random
import networkx as nx
from typing import List

TOPOLOGIES = ["isolated", "clustered", "small_world", "hub_spoke", "fully_connected"]


def generate_graph(topology: str, buyer_ids: List[str], seller_ids: List[str], seed: int = 42) -> nx.Graph:
    rng = random.Random(seed)
    G = nx.Graph()
    G.add_nodes_from(buyer_ids + seller_ids)

    # Every buyer can transact with every seller — sellers are always reachable.
    for b in buyer_ids:
        for s in seller_ids:
            G.add_edge(b, s)

    # Buyer-to-buyer edges depend on topology.
    if topology == "isolated":
        pass

    elif topology == "clustered":
        for start in range(0, len(buyer_ids), 5):
            cluster = buyer_ids[start:start + 5]
            for i in range(len(cluster)):
                for j in range(i + 1, len(cluster)):
                    G.add_edge(cluster[i], cluster[j])

    elif topology == "small_world":
        for start in range(0, len(buyer_ids), 5):
            cluster = buyer_ids[start:start + 5]
            for i in range(len(cluster)):
                for j in range(i + 1, len(cluster)):
                    G.add_edge(cluster[i], cluster[j])
        # 3 long-range ties bridging clusters
        for _ in range(3):
            a, b = rng.sample(buyer_ids, 2)
            G.add_edge(a, b)

    elif topology == "hub_spoke":
        hub = buyer_ids[0]
        for b in buyer_ids[1:]:
            G.add_edge(hub, b)

    elif topology == "fully_connected":
        for i in range(len(buyer_ids)):
            for j in range(i + 1, len(buyer_ids)):
                G.add_edge(buyer_ids[i], buyer_ids[j])

    else:
        raise ValueError(f"unknown topology: {topology}")

    return G


def graph_to_matrix(G: nx.Graph) -> dict:
    """Convert nx.Graph to a Dict[node][node] -> bool matrix."""
    nodes = sorted(G.nodes())
    matrix = {n: {m: False for m in nodes} for n in nodes}
    for a, b in G.edges():
        matrix[a][b] = True
        matrix[b][a] = True
    return matrix
