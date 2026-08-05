"""Co-betting similarity and community detection.

The graph is built from an IDF-weighted user x bet matrix. Rarity weighting is
the point: thousands of wallets bought Trump-2024-Yes, so that bet says nothing
about affiliation, while a shared position in an obscure low-volume market is
strong evidence two wallets are coordinated or share a source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
import numpy as np
import pandas as pd
from scipy import sparse

from .universe import PositionSet


@dataclass
class ClusterParams:
    min_shared_bets: int = 2
    similarity_threshold: float = 0.35
    louvain_resolution: float = 1.0
    min_cluster_size: int = 2
    max_bet_user_frac: float = 0.25  # drop "stopword" bets held by most of the pool
    use_idf: bool = True
    size_weighting: str = "log_usd"  # log_usd | usd | binary
    method: str = "louvain"  # louvain | components | greedy_modularity
    timing_bonus: bool = True
    timing_window_hours: float = 6.0
    core_pct: float = 0.75  # membership fraction that counts a bet as "core"


@dataclass
class SimilarityGraph:
    users: list[str]
    matrix: sparse.csr_matrix          # users x bets, L2-normalised rows
    binary: sparse.csr_matrix          # users x bets, 0/1
    bet_keys: list[str]
    idf: np.ndarray
    edges: pd.DataFrame = field(default_factory=pd.DataFrame)  # u, v, sim, shared

    @property
    def n_users(self) -> int:
        return len(self.users)


def build_matrix(ps: PositionSet, p: ClusterParams) -> SimilarityGraph:
    """Assemble the sparse user x bet matrix with rarity and size weighting."""
    pos = ps.positions
    if pos.empty:
        return SimilarityGraph([], sparse.csr_matrix((0, 0)), sparse.csr_matrix((0, 0)), [], np.array([]))

    users = sorted(pos.proxy_wallet.unique())
    u_index = {u: i for i, u in enumerate(users)}

    # Collapse duplicate (user, bet) rows before pivoting.
    grouped = pos.groupby(["proxy_wallet", "bet_key"], as_index=False).agg(
        buy_usd=("buy_usd", "sum")
    )
    holders = grouped.groupby("bet_key").proxy_wallet.nunique()
    n_users = len(users)

    # Bets held by almost everyone carry no discriminating information and turn
    # the similarity product into a dense block, so drop them outright.
    max_holders = max(2, int(p.max_bet_user_frac * n_users))
    usable = holders[(holders >= 2) & (holders <= max_holders)].index
    grouped = grouped[grouped.bet_key.isin(usable)]
    if grouped.empty:
        return SimilarityGraph(users, sparse.csr_matrix((n_users, 0)),
                               sparse.csr_matrix((n_users, 0)), [], np.array([]))

    bet_keys = sorted(grouped.bet_key.unique())
    b_index = {b: i for i, b in enumerate(bet_keys)}
    rows = grouped.proxy_wallet.map(u_index).to_numpy()
    cols = grouped.bet_key.map(b_index).to_numpy()

    if p.size_weighting == "binary":
        vals = np.ones(len(grouped))
    elif p.size_weighting == "usd":
        vals = grouped.buy_usd.to_numpy(dtype=float)
    else:
        vals = np.log1p(grouped.buy_usd.to_numpy(dtype=float))
    vals = np.maximum(vals, 1e-6)

    binary = sparse.csr_matrix(
        (np.ones(len(grouped)), (rows, cols)), shape=(n_users, len(bet_keys))
    )
    n_holders = np.asarray(binary.sum(axis=0)).ravel()
    idf = np.log((n_users + 1.0) / (n_holders + 1.0)) + 1.0 if p.use_idf else np.ones(len(bet_keys))

    weighted = sparse.csr_matrix((vals * idf[cols], (rows, cols)), shape=binary.shape)
    norms = np.sqrt(np.asarray(weighted.multiply(weighted).sum(axis=1)).ravel())
    norms[norms == 0] = 1.0
    normed = sparse.diags(1.0 / norms) @ weighted

    return SimilarityGraph(users, normed.tocsr(), binary.tocsr(), bet_keys, idf)


def compute_edges(sg: SimilarityGraph, p: ClusterParams, ps: PositionSet | None = None) -> pd.DataFrame:
    """Pairwise cosine similarity, pruned by shared-bet count and threshold."""
    if sg.n_users < 2 or sg.matrix.shape[1] == 0:
        return pd.DataFrame(columns=["u", "v", "sim", "shared", "weight"])

    shared = (sg.binary @ sg.binary.T).tocoo()
    mask = (shared.row < shared.col) & (shared.data >= p.min_shared_bets)
    if not mask.any():
        return pd.DataFrame(columns=["u", "v", "sim", "shared", "weight"])
    rows = shared.row[mask]
    cols = shared.col[mask]
    shared_counts = shared.data[mask]

    # Only the surviving pairs need a similarity value, so score them directly
    # instead of materialising the full product.
    m = sg.matrix
    sims = np.asarray(m[rows].multiply(m[cols]).sum(axis=1)).ravel()

    keep = sims >= p.similarity_threshold
    rows, cols, shared_counts, sims = rows[keep], cols[keep], shared_counts[keep], sims[keep]
    if len(rows) == 0:
        return pd.DataFrame(columns=["u", "v", "sim", "shared", "weight"])

    edges = pd.DataFrame(
        {
            "u": [sg.users[i] for i in rows],
            "v": [sg.users[i] for i in cols],
            "sim": sims,
            "shared": shared_counts.astype(int),
        }
    )
    edges["weight"] = edges.sim

    if p.timing_bonus and ps is not None and not ps.positions.empty:
        edges = _apply_timing_bonus(edges, ps, p)
    return edges.sort_values("weight", ascending=False).reset_index(drop=True)


def _apply_timing_bonus(edges: pd.DataFrame, ps: PositionSet, p: ClusterParams) -> pd.DataFrame:
    """Boost pairs that repeatedly enter the same bet within a short window.

    Two wallets holding the same view is ordinary. Two wallets acting on it
    within minutes of each other, repeatedly, is not.
    """
    entry = (
        ps.positions.groupby(["proxy_wallet", "bet_key"]).first_buy_ts.min().reset_index()
    )
    lookup = entry.set_index(["proxy_wallet", "bet_key"]).first_buy_ts

    by_user: dict[str, dict[str, float]] = {}
    for (wallet, bet), ts in lookup.items():
        by_user.setdefault(wallet, {})[bet] = float(ts)

    window = p.timing_window_hours * 3600.0
    sync_frac = np.zeros(len(edges))
    median_gap = np.full(len(edges), np.nan)
    for i, (u, v) in enumerate(zip(edges.u.to_numpy(), edges.v.to_numpy())):
        a, b = by_user.get(u, {}), by_user.get(v, {})
        common = a.keys() & b.keys()
        if not common:
            continue
        gaps = np.array([abs(a[k] - b[k]) for k in common])
        sync_frac[i] = float((gaps <= window).mean())
        median_gap[i] = float(np.median(gaps))
    edges["sync_frac"] = sync_frac
    edges["median_gap_s"] = median_gap
    edges["weight"] = edges.sim * (1.0 + 0.5 * edges.sync_frac)
    return edges


def detect_communities(edges: pd.DataFrame, p: ClusterParams) -> tuple[nx.Graph, pd.DataFrame]:
    """Partition the similarity graph and return (graph, wallet -> cluster_id)."""
    graph = nx.Graph()
    if edges.empty:
        return graph, pd.DataFrame(columns=["proxy_wallet", "cluster_id"])

    for row in edges.itertuples():
        graph.add_edge(row.u, row.v, weight=float(row.weight), sim=float(row.sim),
                       shared=int(row.shared))

    if p.method == "components":
        groups = list(nx.connected_components(graph))
    elif p.method == "greedy_modularity":
        groups = list(
            nx.community.greedy_modularity_communities(graph, weight="weight",
                                                       resolution=p.louvain_resolution)
        )
    else:
        groups = nx.community.louvain_communities(
            graph, weight="weight", resolution=p.louvain_resolution, seed=42
        )

    groups = [g for g in groups if len(g) >= p.min_cluster_size]
    groups.sort(key=len, reverse=True)
    rows = [
        {"proxy_wallet": w, "cluster_id": cid}
        for cid, members in enumerate(groups, start=1)
        for w in sorted(members)
    ]
    return graph, pd.DataFrame(rows)
