"""End-to-end analysis run: filters -> positions -> graph -> clusters -> metrics."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import networkx as nx
import pandas as pd

from ..config import AnalysisFilters, AppSettings
from ..core.db import Database
from .cluster import ClusterParams, SimilarityGraph, build_matrix, compute_edges, detect_communities
from .metrics import attach_clusters, bet_metrics, cluster_metrics, compute_suspicion, member_metrics
from .universe import PositionSet, build_positions, filter_users

ProgressFn = Callable[[str], None]


@dataclass
class AnalysisResult:
    clusters: pd.DataFrame = field(default_factory=pd.DataFrame)
    members: pd.DataFrame = field(default_factory=pd.DataFrame)
    bets: pd.DataFrame = field(default_factory=pd.DataFrame)
    edges: pd.DataFrame = field(default_factory=pd.DataFrame)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    markets: pd.DataFrame = field(default_factory=pd.DataFrame)
    graph: nx.Graph = field(default_factory=nx.Graph)
    params: ClusterParams = field(default_factory=ClusterParams)
    filters: AnalysisFilters = field(default_factory=AnalysisFilters)
    stats: dict[str, Any] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.clusters.empty

    def cluster_members(self, cluster_id: int) -> pd.DataFrame:
        if self.members.empty:
            return pd.DataFrame()
        return self.members[self.members.cluster_id == cluster_id]

    def cluster_bets(self, cluster_id: int) -> pd.DataFrame:
        if self.bets.empty:
            return pd.DataFrame()
        return self.bets[self.bets.cluster_id == cluster_id]

    def cluster_positions(self, cluster_id: int) -> pd.DataFrame:
        if self.positions.empty:
            return pd.DataFrame()
        return self.positions[self.positions.cluster_id == cluster_id]

    def cluster_subgraph(self, cluster_id: int) -> nx.Graph:
        wallets = set(self.cluster_members(cluster_id).proxy_wallet)
        return self.graph.subgraph(wallets).copy() if wallets else nx.Graph()

    def rescore(self, weights: dict[str, float]) -> None:
        """Recompute the suspicion ranking without re-running the pipeline."""
        self.clusters = compute_suspicion(self.clusters, self.params, weights)


def run_analysis(
    db: Database,
    filters: AnalysisFilters,
    settings: AppSettings,
    params: ClusterParams,
    *,
    progress: ProgressFn | None = None,
    min_position_usd: float | None = None,
) -> AnalysisResult:
    log: list[str] = []

    def say(msg: str) -> None:
        log.append(msg)
        if progress:
            progress(msg)

    t0 = time.perf_counter()
    result = AnalysisResult(params=params, filters=filters, log=log)

    say("Selecting market universe and building positions...")
    ps: PositionSet = build_positions(
        db,
        filters,
        min_position_usd=(
            settings.min_position_usd if min_position_usd is None else min_position_usd
        ),
        min_entry_price=settings.min_entry_price,
        max_entry_price=settings.max_entry_price,
    )
    result.markets = ps.markets
    if ps.positions.empty:
        say("No positions matched. Widen the date range, or ingest more markets first.")
        result.stats = {"markets": len(ps.markets), "users": 0, "bets": 0, "elapsed": 0.0}
        return result
    say(f"  {len(ps.markets):,} markets | {len(ps.positions):,} positions "
        f"| {ps.n_users:,} wallets | {ps.n_bets:,} distinct bets")

    say("Filtering to wallets that clear the size / selectivity thresholds...")
    ps = filter_users(
        ps,
        min_user_usd=settings.min_user_usd,
        min_user_bets=settings.min_user_bets,
        max_user_bets=settings.max_user_bets,
        max_users=settings.max_users,
    )
    if ps.positions.empty:
        say("Every wallet was filtered out. Lower 'min wallet USD' or 'min bets'.")
        result.stats = {"markets": len(result.markets), "users": 0, "bets": 0, "elapsed": 0.0}
        return result
    say(f"  {ps.n_users:,} wallets survive | {ps.n_bets:,} bets")

    say("Building the IDF-weighted user x bet matrix...")
    sg: SimilarityGraph = build_matrix(ps, params)
    say(f"  matrix {sg.matrix.shape[0]:,} x {sg.matrix.shape[1]:,} "
        f"({sg.matrix.nnz:,} non-zero)")

    say("Scoring pairwise co-betting similarity...")
    edges = compute_edges(sg, params, ps)
    result.edges = edges
    say(f"  {len(edges):,} edges above sim>={params.similarity_threshold} "
        f"and shared>={params.min_shared_bets}")
    if edges.empty:
        say("No pairs passed the similarity gate. Lower the threshold or min shared bets.")
        result.stats = {"markets": len(result.markets), "users": ps.n_users,
                        "bets": ps.n_bets, "elapsed": time.perf_counter() - t0}
        return result

    say(f"Detecting communities ({params.method})...")
    graph, membership = detect_communities(edges, params)
    result.graph = graph
    if membership.empty:
        say("No community met the minimum cluster size.")
        result.stats = {"markets": len(result.markets), "users": ps.n_users,
                        "bets": ps.n_bets, "elapsed": time.perf_counter() - t0}
        return result
    n_clusters = membership.cluster_id.nunique()
    say(f"  {n_clusters} clusters covering {len(membership):,} wallets")

    say("Computing cluster, member and bet metrics...")
    cp = attach_clusters(ps, membership)
    bets = bet_metrics(cp, ps, sg, params)
    result.bets = bets
    db_users = db.query("SELECT proxy_wallet, name, pseudonym FROM users")
    members = member_metrics(cp, bets, edges, db_users)

    # Raw positions are read row by row, so carry the human labels rather than
    # making the reader resolve a wallet hash and a condition id by eye.
    meta = ps.markets.set_index("condition_id")
    cp["question"] = cp.condition_id.map(meta["question"])
    cp["event_title"] = cp.condition_id.map(meta["event_title"])
    if not members.empty:
        cp["display"] = cp.proxy_wallet.map(
            members.drop_duplicates("proxy_wallet").set_index("proxy_wallet")["display"]
        )
    else:
        cp["display"] = cp.proxy_wallet.str[:10]
    result.positions = cp
    result.members = members
    result.clusters = cluster_metrics(cp, bets, members, edges, ps, params)

    elapsed = time.perf_counter() - t0
    result.stats = {
        "markets": len(result.markets),
        "users": ps.n_users,
        "bets": ps.n_bets,
        "clusters": int(n_clusters),
        "clustered_wallets": int(len(membership)),
        "edges": int(len(edges)),
        "elapsed": elapsed,
    }
    say(f"Done in {elapsed:.1f}s - {n_clusters} clusters ranked by suspicion score.")
    return result
