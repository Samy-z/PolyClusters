"""Cluster, member and bet-level metrics, plus the composite suspicion score.

Three tables come out of here:

* ``clusters``  - one row per detected group, answering "is this group worth
  copying, and does it look informed rather than lucky?"
* ``members``   - one row per wallet inside a cluster: who leads, who is
  biggest, who gets the best fills.
* ``bets``      - one row per (cluster, bet): the positions themselves.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .cluster import ClusterParams, SimilarityGraph
from .universe import PositionSet

EPS = 1e-9


def _safe_div(a, b):
    return np.divide(a, b, out=np.full_like(np.asarray(a, dtype=float), np.nan),
                     where=np.asarray(b, dtype=float) != 0)


def _robust_z(series: pd.Series) -> pd.Series:
    """Median/MAD z-score - a handful of extreme clusters must not flatten the rest."""
    s = pd.to_numeric(series, errors="coerce")
    med = s.median()
    mad = (s - med).abs().median()
    if not np.isfinite(mad) or mad < EPS:
        std = s.std()
        if not np.isfinite(std) or std < EPS:
            return pd.Series(np.zeros(len(s)), index=s.index)
        return ((s - med) / std).fillna(0.0)
    return ((s - med) / (1.4826 * mad)).fillna(0.0)


def attach_clusters(ps: PositionSet, membership: pd.DataFrame) -> pd.DataFrame:
    """Join cluster ids onto positions, keeping only clustered wallets."""
    if membership.empty or ps.positions.empty:
        return pd.DataFrame()
    return ps.positions.merge(membership, on="proxy_wallet", how="inner")


# ---------------------------------------------------------------------------
# Bet-level
# ---------------------------------------------------------------------------
def bet_metrics(
    cp: pd.DataFrame, ps: PositionSet, sg: SimilarityGraph, params: ClusterParams
) -> pd.DataFrame:
    """One row per (cluster, bet): who is in it, when, at what price, and how it paid."""
    if cp.empty:
        return pd.DataFrame()

    # Rarity is recomputed across every bet in the universe rather than reused
    # from the matrix, whose "stopword" bets were dropped and would show blank.
    n_pool = max(ps.positions.proxy_wallet.nunique(), 1)
    holders = ps.positions.groupby("bet_key").proxy_wallet.nunique()
    idf_map = (np.log((n_pool + 1.0) / (holders + 1.0)) + 1.0).to_dict()
    sizes = cp.groupby("cluster_id").proxy_wallet.nunique().rename("cluster_size")

    grouped = cp.groupby(["cluster_id", "bet_key"])
    rows = grouped.agg(
        condition_id=("condition_id", "first"),
        outcome_index=("outcome_index", "first"),
        outcome=("outcome", "first"),
        n_members=("proxy_wallet", "nunique"),
        total_usd=("buy_usd", "sum"),
        total_shares=("buy_shares", "sum"),
        avg_entry=("vwap_entry", "mean"),
        min_entry=("vwap_entry", "min"),
        max_entry=("vwap_entry", "max"),
        first_entry_ts=("first_buy_ts", "min"),
        last_entry_ts=("first_buy_ts", "max"),
        market_vwap=("market_vwap", "first"),
        market_traders=("market_traders", "first"),
        resolved=("resolved", "first"),
        won=("won", "first"),
        pnl=("pnl", "sum"),
        edge_per_share=("edge_per_share", "mean"),
        entry_pct_of_life=("entry_pct_of_life", "median"),
        hours_before_close=("hours_before_close", "median"),
    ).reset_index()

    rows = rows.merge(sizes, on="cluster_id", how="left")
    rows["member_frac"] = rows.n_members / rows.cluster_size
    rows["unanimous"] = rows.n_members >= rows.cluster_size
    rows["core"] = rows.member_frac >= params_core_pct(params)
    rows["entry_spread_h"] = (rows.last_entry_ts - rows.first_entry_ts) / 3600.0
    rows["price_spread"] = rows.max_entry - rows.min_entry
    rows["entry_vs_market"] = rows.avg_entry - rows.market_vwap
    rows["roi"] = _safe_div(rows.pnl, rows.total_usd)
    rows["rarity_idf"] = rows.bet_key.map(idf_map)

    # Identify the first wallet in and the largest wallet in, per bet.
    first_in = cp.loc[cp.groupby(["cluster_id", "bet_key"]).first_buy_ts.idxmin()]
    biggest = cp.loc[cp.groupby(["cluster_id", "bet_key"]).buy_usd.idxmax()]
    rows = rows.merge(
        first_in[["cluster_id", "bet_key", "proxy_wallet", "vwap_entry"]].rename(
            columns={"proxy_wallet": "first_entrant", "vwap_entry": "first_entry_price"}
        ),
        on=["cluster_id", "bet_key"], how="left",
    )
    rows = rows.merge(
        biggest[["cluster_id", "bet_key", "proxy_wallet", "buy_usd"]].rename(
            columns={"proxy_wallet": "biggest_bettor", "buy_usd": "biggest_usd"}
        ),
        on=["cluster_id", "bet_key"], how="left",
    )

    meta = ps.markets.set_index("condition_id")
    rows["question"] = rows.condition_id.map(meta["question"])
    rows["event_title"] = rows.condition_id.map(meta["event_title"])
    rows["sectors"] = rows.condition_id.map(meta["sectors"])
    rows["market_volume"] = rows.condition_id.map(meta["volume"])
    rows["market_slug"] = rows.condition_id.map(meta["slug"])
    return rows.sort_values(["cluster_id", "total_usd"], ascending=[True, False])


def params_core_pct(params: ClusterParams) -> float:
    return float(getattr(params, "core_pct", 0.75))


# ---------------------------------------------------------------------------
# Member-level
# ---------------------------------------------------------------------------
def member_metrics(
    cp: pd.DataFrame, bets: pd.DataFrame, edges: pd.DataFrame, db_users: pd.DataFrame
) -> pd.DataFrame:
    """Per-wallet behaviour inside its cluster: leadership, size, fill quality."""
    if cp.empty:
        return pd.DataFrame()

    base = cp.groupby(["cluster_id", "proxy_wallet"]).agg(
        n_bets=("bet_key", "nunique"),
        n_markets=("condition_id", "nunique"),
        n_trades=("n_trades", "sum"),
        total_usd=("buy_usd", "sum"),
        avg_usd=("buy_usd", "mean"),
        max_usd=("buy_usd", "max"),
        pnl=("pnl", "sum"),
        avg_entry=("vwap_entry", "mean"),
        avg_entry_vs_vwap=("entry_vs_vwap", "mean"),
        avg_edge_per_share=("edge_per_share", "mean"),
        median_entry_pct_of_life=("entry_pct_of_life", "median"),
        median_hours_before_close=("hours_before_close", "median"),
        first_ts=("first_buy_ts", "min"),
        last_ts=("last_buy_ts", "max"),
    ).reset_index()
    base["roi"] = _safe_div(base.pnl, base.total_usd)

    resolved = cp[cp.resolved.astype(bool)].copy()
    if not resolved.empty:
        resolved["won_usd"] = np.where(resolved.won == 1.0, resolved.buy_usd, 0.0)
        wr = resolved.groupby(["cluster_id", "proxy_wallet"]).agg(
            resolved_bets=("bet_key", "nunique"),
            wins=("won", "sum"),
            resolved_usd=("buy_usd", "sum"),
            won_usd=("won_usd", "sum"),
        ).reset_index()
        wr["winrate"] = _safe_div(wr.wins, wr.resolved_bets)
        wr["winrate_usd"] = _safe_div(wr.won_usd, wr.resolved_usd)
        base = base.merge(wr, on=["cluster_id", "proxy_wallet"], how="left")
    else:
        for col in ("resolved_bets", "wins", "resolved_usd", "won_usd", "winrate", "winrate_usd"):
            base[col] = np.nan

    # --- leadership: who moves first on bets the cluster shares ------------
    shared_keys = bets[bets.n_members >= 2][["cluster_id", "bet_key"]] if not bets.empty else pd.DataFrame()
    if not shared_keys.empty:
        shared_pos = cp.merge(shared_keys, on=["cluster_id", "bet_key"], how="inner")
        rank = shared_pos.groupby(["cluster_id", "bet_key"]).first_buy_ts.rank(method="min")
        shared_pos = shared_pos.assign(entry_rank=rank)
        med = shared_pos.groupby(["cluster_id", "bet_key"]).first_buy_ts.transform("median")
        shared_pos["lead_time_h"] = (shared_pos.first_buy_ts - med) / 3600.0
        bet_avg_entry = shared_pos.groupby(["cluster_id", "bet_key"]).vwap_entry.transform("mean")
        shared_pos["entry_vs_cluster"] = shared_pos.vwap_entry - bet_avg_entry

        lead = shared_pos.groupby(["cluster_id", "proxy_wallet"]).agg(
            n_shared_bets=("bet_key", "nunique"),
            first_mover_count=("entry_rank", lambda s: int((s == 1).sum())),
            avg_entry_rank=("entry_rank", "mean"),
            avg_lead_time_h=("lead_time_h", "mean"),
            median_lead_time_h=("lead_time_h", "median"),
            avg_entry_vs_cluster=("entry_vs_cluster", "mean"),
            shared_usd=("buy_usd", "sum"),
        ).reset_index()
        lead["first_mover_rate"] = _safe_div(lead.first_mover_count, lead.n_shared_bets)
        base = base.merge(lead, on=["cluster_id", "proxy_wallet"], how="left")
        # How much of this wallet's activity is spent alongside the cluster.
        # A wallet at 90% is effectively dedicated to the group; one at 10%
        # happens to overlap while mostly trading its own book.
        base["shared_pct"] = _safe_div(base.n_shared_bets, base.n_bets)
    else:
        for col in ("n_shared_bets", "first_mover_count", "avg_entry_rank", "avg_lead_time_h",
                    "median_lead_time_h", "avg_entry_vs_cluster", "shared_usd",
                    "first_mover_rate", "shared_pct"):
            base[col] = np.nan

    # --- how tightly each wallet sits inside its own cluster ---------------
    if not edges.empty:
        member_of = base.set_index("proxy_wallet").cluster_id.to_dict()
        e = edges[
            edges.u.map(member_of).notna()
            & (edges.u.map(member_of) == edges.v.map(member_of))
        ]
        if not e.empty:
            both = pd.concat([
                e.rename(columns={"u": "proxy_wallet", "v": "other"}),
                e.rename(columns={"v": "proxy_wallet", "u": "other"}),
            ])
            aff = both.groupby("proxy_wallet").agg(
                avg_sim_to_cluster=("sim", "mean"),
                max_sim_to_cluster=("sim", "max"),
                degree=("other", "nunique"),
            )
            if "sync_frac" in both.columns:
                aff["avg_sync_frac"] = both.groupby("proxy_wallet").sync_frac.mean()
            base = base.merge(aff, left_on="proxy_wallet", right_index=True, how="left")

    if not db_users.empty:
        base = base.merge(db_users, on="proxy_wallet", how="left")
    for col in ("name", "pseudonym"):
        if col not in base.columns:
            base[col] = ""
    base["display"] = base.name.fillna("").replace("", np.nan) \
        .fillna(base.pseudonym.fillna("")).replace("", np.nan) \
        .fillna(base.proxy_wallet.str[:10])

    # Flags the user explicitly asked to be able to see at a glance.
    base["is_biggest_bettor"] = base.groupby("cluster_id").total_usd.rank(
        ascending=False, method="min") == 1
    base["is_lead_mover"] = base.groupby("cluster_id").first_mover_count.rank(
        ascending=False, method="min") == 1
    base["usd_share_of_cluster"] = base.total_usd / base.groupby("cluster_id").total_usd.transform("sum")
    return base.sort_values(["cluster_id", "total_usd"], ascending=[True, False])


# ---------------------------------------------------------------------------
# Cluster-level
# ---------------------------------------------------------------------------
def cluster_metrics(
    cp: pd.DataFrame,
    bets: pd.DataFrame,
    members: pd.DataFrame,
    edges: pd.DataFrame,
    ps: PositionSet,
    params: ClusterParams,
) -> pd.DataFrame:
    if cp.empty:
        return pd.DataFrame()

    core_pct = params_core_pct(params)
    out = cp.groupby("cluster_id").agg(
        n_members=("proxy_wallet", "nunique"),
        n_positions=("bet_key", "size"),
        n_bets=("bet_key", "nunique"),
        n_markets=("condition_id", "nunique"),
        total_usd=("buy_usd", "sum"),
        median_position_usd=("buy_usd", "median"),
        max_position_usd=("buy_usd", "max"),
        pnl=("pnl", "sum"),
        first_ts=("first_buy_ts", "min"),
        last_ts=("last_buy_ts", "max"),
        median_entry_pct_of_life=("entry_pct_of_life", "median"),
        median_hours_before_close=("hours_before_close", "median"),
        mean_entry_vs_vwap=("entry_vs_vwap", "mean"),
        mean_edge_per_share=("edge_per_share", "mean"),
    ).reset_index()
    out["roi"] = _safe_div(out.pnl, out.total_usd)
    out["avg_usd_per_member"] = out.total_usd / out.n_members
    out["bets_per_member"] = out.n_bets / out.n_members

    # --- win rates ---------------------------------------------------------
    res = cp[cp.resolved.astype(bool)].copy()
    if not res.empty:
        res["won_usd"] = np.where(res.won == 1.0, res.buy_usd, 0.0)
        wr = res.groupby("cluster_id").agg(
            resolved_positions=("bet_key", "size"),
            wins=("won", "sum"),
            resolved_usd=("buy_usd", "sum"),
            won_usd=("won_usd", "sum"),
            resolved_pnl=("pnl", "sum"),
        ).reset_index()
        wr["winrate"] = _safe_div(wr.wins, wr.resolved_positions)
        wr["winrate_usd"] = _safe_div(wr.won_usd, wr.resolved_usd)
        wr["resolved_roi"] = _safe_div(wr.resolved_pnl, wr.resolved_usd)
        out = out.merge(wr, on="cluster_id", how="left")

        # Longshot performance separates informed traders from favourite-buyers:
        # both show a high headline win rate, only one of them earns anything.
        ls = res[res.is_longshot]
        if not ls.empty:
            lsg = ls.groupby("cluster_id").agg(
                longshot_bets=("bet_key", "size"),
                longshot_wins=("won", "sum"),
                longshot_usd=("buy_usd", "sum"),
                longshot_pnl=("pnl", "sum"),
                longshot_avg_entry=("vwap_entry", "mean"),
            ).reset_index()
            lsg["longshot_winrate"] = _safe_div(lsg.longshot_wins, lsg.longshot_bets)
            lsg["longshot_roi"] = _safe_div(lsg.longshot_pnl, lsg.longshot_usd)
            out = out.merge(lsg, on="cluster_id", how="left")
        else:
            for col in ("longshot_bets", "longshot_wins", "longshot_usd", "longshot_pnl",
                        "longshot_avg_entry", "longshot_winrate", "longshot_roi"):
                out[col] = np.nan
    else:
        for col in ("resolved_positions", "wins", "resolved_usd", "won_usd",
                    "resolved_pnl", "winrate", "winrate_usd", "resolved_roi",
                    "longshot_bets", "longshot_wins", "longshot_usd", "longshot_pnl",
                    "longshot_avg_entry", "longshot_winrate", "longshot_roi"):
            out[col] = np.nan

    # --- shared / unanimous bets ------------------------------------------
    if not bets.empty:
        multi = bets[bets.n_members >= 2]
        agg = {}
        for cid, grp in bets.groupby("cluster_id"):
            multi_g = grp[grp.n_members >= 2]
            unan = grp[grp.unanimous]
            core = grp[grp.member_frac >= core_pct]
            res_unan = unan[unan.resolved.astype(bool)]
            res_core = core[core.resolved.astype(bool)]
            res_multi = multi_g[multi_g.resolved.astype(bool)]
            agg[cid] = {
                "n_shared_bets": int(len(multi_g)),
                "n_unanimous_bets": int(len(unan)),
                "n_core_bets": int(len(core)),
                "unanimity_rate": float(len(unan) / len(multi_g)) if len(multi_g) else np.nan,
                "core_rate": float(len(core) / len(multi_g)) if len(multi_g) else np.nan,
                "shared_usd": float(multi_g.total_usd.sum()),
                "unanimous_usd": float(unan.total_usd.sum()),
                "shared_winrate": float(res_multi.won.mean()) if len(res_multi) else np.nan,
                "unanimous_winrate": float(res_unan.won.mean()) if len(res_unan) else np.nan,
                "core_winrate": float(res_core.won.mean()) if len(res_core) else np.nan,
                "shared_roi": float(_safe_div(res_multi.pnl.sum(), res_multi.total_usd.sum()))
                if len(res_multi) else np.nan,
                "unanimous_roi": float(_safe_div(res_unan.pnl.sum(), res_unan.total_usd.sum()))
                if len(res_unan) else np.nan,
                "median_entry_spread_h": float(multi_g.entry_spread_h.median())
                if len(multi_g) else np.nan,
                "sync_rate": float((multi_g.entry_spread_h <= params.timing_window_hours).mean())
                if len(multi_g) else np.nan,
                "mean_price_spread": float(multi_g.price_spread.mean()) if len(multi_g) else np.nan,
                "mean_rarity_idf": float(multi_g.rarity_idf.mean()) if len(multi_g) else np.nan,
                "median_market_volume": float(multi_g.market_volume.median())
                if len(multi_g) else np.nan,
            }
        out = out.merge(
            pd.DataFrame.from_dict(agg, orient="index").rename_axis("cluster_id").reset_index(),
            on="cluster_id", how="left",
        )
        out["shared_usd_frac"] = _safe_div(out.shared_usd, out.total_usd)

    # --- concordance: when two members touch a market, do they agree? ------
    conc = _concordance(cp)
    if not conc.empty:
        out = out.merge(conc, on="cluster_id", how="left")

    # --- graph cohesion ----------------------------------------------------
    if not edges.empty and not members.empty:
        member_of = members.set_index("proxy_wallet").cluster_id.to_dict()
        e = edges.copy()
        e["cu"] = e.u.map(member_of)
        e["cv"] = e.v.map(member_of)
        internal = e[(e.cu == e.cv) & e.cu.notna()]
        if not internal.empty:
            g = internal.groupby("cu").agg(
                n_edges=("sim", "size"),
                avg_pairwise_sim=("sim", "mean"),
                max_pairwise_sim=("sim", "max"),
                avg_shared_bets=("shared", "mean"),
            ).rename_axis("cluster_id").reset_index()
            if "sync_frac" in internal.columns:
                g["avg_pair_sync_frac"] = internal.groupby("cu").sync_frac.mean().values
            out = out.merge(g, on="cluster_id", how="left")
            possible = out.n_members * (out.n_members - 1) / 2
            out["density"] = _safe_div(out.n_edges, possible)

    # --- member-derived rollups -------------------------------------------
    if not members.empty:
        mm = members.groupby("cluster_id").agg(
            median_member_usd=("total_usd", "median"),
            min_member_usd=("total_usd", "min"),
            usd_concentration=("usd_share_of_cluster", "max"),
            median_member_winrate=("winrate", "median"),
        ).reset_index()
        out = out.merge(mm, on="cluster_id", how="left")

    out["window_days"] = (out.last_ts - out.first_ts) / 86400.0
    return compute_suspicion(out, params)


def _concordance(cp: pd.DataFrame) -> pd.DataFrame:
    """Fraction of contested markets on which all present members took the same side."""
    if cp.empty:
        return pd.DataFrame()
    per_market = cp.groupby(["cluster_id", "condition_id"]).agg(
        n_members=("proxy_wallet", "nunique"),
        n_sides=("outcome_index", "nunique"),
    ).reset_index()
    contested = per_market[per_market.n_members >= 2].copy()
    if contested.empty:
        return pd.DataFrame()
    contested["agreed"] = (contested.n_sides == 1).astype(float)
    return contested.groupby("cluster_id").agg(
        n_contested_markets=("agreed", "size"),
        concordance=("agreed", "mean"),
    ).reset_index()


# column -> (settings weight attribute, True if a HIGHER value is more suspicious)
#
# Win rate is deliberately split from - and outweighed by - realised edge.
# Ranking on win rate alone surfaces wallets that buy 98c favourites and win
# almost always for almost nothing, which is the opposite of what we want.
SUSPICION_COMPONENTS = {
    "winrate": ("weight_winrate", True),
    "shared_winrate": ("weight_winrate", True),
    "longshot_winrate": ("weight_winrate", True),
    "resolved_roi": ("weight_roi", True),
    "unanimous_roi": ("weight_roi", True),
    "mean_edge_per_share": ("weight_roi", True),
    "longshot_roi": ("weight_roi", True),
    "unanimity_rate": ("weight_unanimity", True),
    "concordance": ("weight_unanimity", True),
    "sync_rate": ("weight_sync", True),
    "median_entry_spread_h": ("weight_sync", False),
    "median_entry_pct_of_life": ("weight_earliness", False),
    "mean_rarity_idf": ("weight_rarity", True),
    "avg_usd_per_member": ("weight_wealth", True),
}


def compute_suspicion(
    clusters: pd.DataFrame, params: ClusterParams, weights: dict[str, float] | None = None
) -> pd.DataFrame:
    """Blend the individual signals into one ranked score.

    Everything is robust-z-scored first so a metric measured in dollars cannot
    swamp one measured as a fraction. Small clusters are damped by a sample
    factor so a 2-member pair with two lucky bets does not top the list.
    """
    if clusters.empty:
        return clusters
    w = weights or {}
    out = clusters.copy()
    total = np.zeros(len(out))
    used = 0.0
    for col, (weight_key, higher_bad) in SUSPICION_COMPONENTS.items():
        if col not in out.columns:
            continue
        weight = float(w.get(weight_key, getattr(params, weight_key, 1.0)) or 0.0)
        if weight == 0:
            continue
        z = _robust_z(out[col]).clip(-4, 4)
        total += weight * (z if higher_bad else -z)
        used += weight
    raw = total / used if used else total

    # Confidence damping: a cluster needs both members and resolved bets.
    n_res = out.get("resolved_positions", pd.Series(np.zeros(len(out)), index=out.index)).fillna(0)
    sample = np.tanh(n_res / 10.0) * np.tanh(out.n_members / 3.0)
    out["suspicion_raw"] = raw
    out["suspicion_score"] = raw * sample
    lo, hi = out.suspicion_score.min(), out.suspicion_score.max()
    out["suspicion_pct"] = (
        100.0 * (out.suspicion_score - lo) / (hi - lo) if hi > lo else 50.0
    )
    return out.sort_values("suspicion_score", ascending=False).reset_index(drop=True)
