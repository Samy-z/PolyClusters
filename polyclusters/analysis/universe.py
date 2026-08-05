"""Turn the UI's filters into a market universe and a user-by-bet position table.

A *bet* here is the pair ``(condition_id, outcome_index)`` - the same asset on
the same side. Two users "agree" only when they hold the same bet key, so a
head-to-head pair on one market never registers as concordance.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..config import AnalysisFilters
from ..core.db import Database


def market_universe(db: Database, f: AnalysisFilters) -> pd.DataFrame:
    """Markets matching the filters, joined with their sector labels."""
    where: list[str] = ["1=1"]
    params: list = []

    if f.condition_ids:
        placeholders = ",".join("?" * len(f.condition_ids))
        where.append(f"m.condition_id IN ({placeholders})")
        params.extend(f.condition_ids)
    if f.event_slugs:
        placeholders = ",".join("?" * len(f.event_slugs))
        where.append(f"m.event_slug IN ({placeholders})")
        params.extend(f.event_slugs)
    if f.tag_ids:
        placeholders = ",".join("?" * len(f.tag_ids))
        where.append(
            f"m.condition_id IN (SELECT condition_id FROM market_tags WHERE tag_id IN ({placeholders}))"
        )
        params.extend(f.tag_ids)
    if f.exclude_tag_ids:
        placeholders = ",".join("?" * len(f.exclude_tag_ids))
        where.append(
            f"m.condition_id NOT IN (SELECT condition_id FROM market_tags WHERE tag_id IN ({placeholders}))"
        )
        params.extend(f.exclude_tag_ids)
    if f.min_market_volume:
        where.append("m.volume >= ?")
        params.append(f.min_market_volume)
    if f.resolved_only:
        where.append("m.resolved")

    clause = " AND ".join(where)
    if f.seed_condition_ids:
        # Seed markets must survive the universe filters even if their volume or
        # sector would otherwise exclude them - they define the anchor set.
        placeholders = ",".join("?" * len(f.seed_condition_ids))
        clause = f"(({clause}) OR m.condition_id IN ({placeholders}))"
        params.extend(f.seed_condition_ids)

    sql = f"""
        SELECT m.*,
               (SELECT string_agg(DISTINCT mt.tag_label, ', ')
                  FROM market_tags mt WHERE mt.condition_id = m.condition_id) AS sectors
        FROM markets m
        WHERE {clause}
    """
    return db.query(sql, params)


@dataclass
class PositionSet:
    """User positions plus the market metadata they were derived from."""

    positions: pd.DataFrame  # one row per (wallet, bet)
    markets: pd.DataFrame
    market_stats: pd.DataFrame  # per-market VWAP / timing reference points
    window: tuple[int, int]

    @property
    def n_users(self) -> int:
        return int(self.positions.proxy_wallet.nunique()) if not self.positions.empty else 0

    @property
    def n_bets(self) -> int:
        return int(self.positions.bet_key.nunique()) if not self.positions.empty else 0


def build_positions(
    db: Database,
    f: AnalysisFilters,
    *,
    min_position_usd: float = 0.0,
    include_flat: bool = False,
    min_entry_price: float = 0.0,
    max_entry_price: float = 1.0,
) -> PositionSet:
    """Aggregate raw trades into net directional positions per user and bet.

    ``include_flat`` keeps positions the user fully exited inside the window;
    they are excluded by default because a round-trip is not a standing bet.

    The entry-price band is the single most important noise filter. Thousands
    of wallets buy the near-certain side at $0.98 and "win" ~100% of the time
    for a fraction of a percent - that agreement is arithmetic, not affiliation,
    and left unfiltered it collapses the whole population into one blob.
    """
    markets = market_universe(db, f)
    if markets.empty:
        return PositionSet(pd.DataFrame(), markets, pd.DataFrame(), (f.start_ts, f.end_ts))

    db.stage_temp("_universe", markets[["condition_id"]])

    t_lo = f.start_ts or 0
    t_hi = f.end_ts or 2_000_000_000

    positions = db.query(
        """
        WITH t AS (
            SELECT tr.* FROM trades tr
            JOIN _universe u USING (condition_id)
            WHERE tr.ts BETWEEN ? AND ?
        )
        SELECT
            proxy_wallet,
            condition_id,
            outcome_index,
            any_value(outcome)                                        AS outcome,
            sum(CASE WHEN side='BUY'  THEN size ELSE 0 END)           AS buy_shares,
            sum(CASE WHEN side='SELL' THEN size ELSE 0 END)           AS sell_shares,
            sum(CASE WHEN side='BUY'  THEN usd  ELSE 0 END)           AS buy_usd,
            sum(CASE WHEN side='SELL' THEN usd  ELSE 0 END)           AS sell_usd,
            min(CASE WHEN side='BUY'  THEN ts END)                    AS first_buy_ts,
            max(CASE WHEN side='BUY'  THEN ts END)                    AS last_buy_ts,
            min(CASE WHEN side='BUY'  THEN price END)                 AS min_buy_price,
            max(CASE WHEN side='BUY'  THEN price END)                 AS max_buy_price,
            min(ts)                                                   AS first_ts,
            max(ts)                                                   AS last_ts,
            count(*)                                                  AS n_trades
        FROM t
        GROUP BY proxy_wallet, condition_id, outcome_index
        """,
        [t_lo, t_hi],
    )

    market_stats = db.query(
        """
        WITH t AS (
            SELECT tr.* FROM trades tr
            JOIN _universe u USING (condition_id)
            WHERE tr.ts BETWEEN ? AND ?
        )
        SELECT condition_id, outcome_index,
               sum(usd) / nullif(sum(size), 0) AS market_vwap,
               min(ts) AS market_first_ts,
               max(ts) AS market_last_ts,
               count(*) AS market_trades,
               count(DISTINCT proxy_wallet) AS market_traders,
               sum(usd) AS market_usd
        FROM t GROUP BY condition_id, outcome_index
        """,
        [t_lo, t_hi],
    )
    db.drop_temp("_universe")

    if positions.empty:
        return PositionSet(positions, markets, market_stats, (t_lo, t_hi))

    positions["net_shares"] = positions.buy_shares - positions.sell_shares
    positions["vwap_entry"] = positions.buy_usd / positions.buy_shares.replace(0, np.nan)
    positions["bet_key"] = (
        positions.condition_id + ":" + positions.outcome_index.astype(str)
    )

    if not include_flat:
        positions = positions[positions.net_shares > 1e-6]
    positions = positions[positions.buy_usd >= max(min_position_usd, 0.0)]
    positions = positions[positions.buy_shares > 0]
    positions = positions[
        positions.vwap_entry.between(min_entry_price, max_entry_price)
    ]

    if f.seed_condition_ids:
        # Anchor on whoever took a position in the seed market(s), then keep
        # their positions everywhere so there is something to cluster on.
        seeded = set(
            positions[positions.condition_id.isin(f.seed_condition_ids)].proxy_wallet
        )
        positions = positions[positions.proxy_wallet.isin(seeded)]

    if positions.empty:
        return PositionSet(positions, markets, market_stats, (t_lo, t_hi))

    # --- settlement and P&L ------------------------------------------------
    win_map = markets.set_index("condition_id")["winning_outcome"]
    resolved_map = markets.set_index("condition_id")["resolved"]
    positions["winning_outcome"] = positions.condition_id.map(win_map)
    positions["resolved"] = positions.condition_id.map(resolved_map).fillna(False).astype(bool)
    positions["won"] = np.where(
        positions.resolved,
        positions.outcome_index == positions.winning_outcome,
        np.nan,
    )
    payout = np.where(positions.won == 1.0, 1.0, 0.0)
    positions["settle_value"] = np.where(
        positions.resolved, positions.net_shares * payout, np.nan
    )
    # Realised + settled P&L over the window. For unresolved markets the
    # position is marked at its last traded price instead.
    last_px = market_stats.set_index(["condition_id", "outcome_index"])["market_vwap"]
    mark = positions.set_index(["condition_id", "outcome_index"]).index.map(last_px)
    positions["mark_price"] = np.asarray(mark, dtype=float)
    positions["pnl"] = np.where(
        positions.resolved,
        positions.sell_usd + positions.settle_value - positions.buy_usd,
        positions.sell_usd + positions.net_shares * positions.mark_price - positions.buy_usd,
    )
    positions["roi"] = positions.pnl / positions.buy_usd.replace(0, np.nan)

    # --- entry quality -----------------------------------------------------
    positions = positions.merge(
        market_stats, on=["condition_id", "outcome_index"], how="left", suffixes=("", "_mkt")
    )
    # Negative = entered cheaper than the market's average clearing price.
    positions["entry_vs_vwap"] = positions.vwap_entry - positions.market_vwap
    # Edge actually captured on resolution: 1 - entry for winners, -entry for losers.
    positions["edge_per_share"] = np.where(
        positions.resolved, payout - positions.vwap_entry, np.nan
    )
    span = (positions.market_last_ts - positions.market_first_ts).replace(0, np.nan)
    positions["entry_pct_of_life"] = (
        (positions.first_buy_ts - positions.market_first_ts) / span
    ).clip(0, 1)
    positions["hours_before_close"] = (
        positions.market_last_ts - positions.first_buy_ts
    ) / 3600.0
    # A win bought at 0.97 is worth ~nothing; a win bought at 0.20 is where
    # private information actually shows up, so they are tracked separately.
    positions["is_longshot"] = positions.vwap_entry < 0.5
    positions["longshot_won"] = np.where(
        positions.resolved & positions.is_longshot, positions.won, np.nan
    )

    return PositionSet(
        positions.reset_index(drop=True), markets, market_stats, (t_lo, t_hi)
    )


def filter_users(
    ps: PositionSet,
    *,
    min_user_usd: float,
    min_user_bets: int,
    max_user_bets: int,
    max_users: int,
) -> PositionSet:
    """Keep only wallets large and selective enough to be worth clustering.

    The upper bet-count bound matters as much as the lower one: high-frequency
    market makers touch thousands of markets and would otherwise dominate every
    co-occurrence graph without carrying any directional signal.
    """
    if ps.positions.empty:
        return ps
    agg = ps.positions.groupby("proxy_wallet").agg(
        total_usd=("buy_usd", "sum"), n_bets=("bet_key", "nunique")
    )
    keep = agg[
        (agg.total_usd >= min_user_usd)
        & (agg.n_bets >= min_user_bets)
        & (agg.n_bets <= max_user_bets)
    ]
    if len(keep) > max_users:
        keep = keep.nlargest(max_users, "total_usd")
    kept = ps.positions[ps.positions.proxy_wallet.isin(keep.index)]
    return PositionSet(kept.reset_index(drop=True), ps.markets, ps.market_stats, ps.window)
