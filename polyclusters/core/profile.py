"""Everything the local store can say about one watched item.

These feed the Watchlist detail panel. All of it comes from trades already on
disk, so opening a profile costs nothing and works offline; the network is only
touched when the user asks for updates.

Each function returns plain frames and arrays rather than widgets, so the same
numbers can be exported, charted or asserted against in tests.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .db import Database
from .watchlist import _as_float, _as_int, _as_text

# Entry-price histogram resolution. Twenty buckets is enough to separate a
# favourite-buyer from a longshot hunter without turning into noise.
PRICE_BINS = 20


def wallet_positions(db: Database, wallet: str) -> pd.DataFrame:
    """Every bet a wallet holds or has held, with outcome and P&L."""
    df = db.query(
        """
        SELECT t.condition_id,
               t.outcome_index,
               any_value(t.outcome)          AS outcome,
               any_value(m.question)         AS question,
               any_value(m.resolved)         AS resolved,
               any_value(m.winning_outcome)  AS winner,
               any_value(m.volume)           AS market_volume,
               any_value(m.end_ts)           AS market_end_ts,
               sum(CASE WHEN t.side='BUY'  THEN t.size ELSE 0 END) AS buy_shares,
               sum(CASE WHEN t.side='SELL' THEN t.size ELSE 0 END) AS sell_shares,
               sum(CASE WHEN t.side='BUY'  THEN t.usd  ELSE 0 END) AS buy_usd,
               sum(CASE WHEN t.side='SELL' THEN t.usd  ELSE 0 END) AS sell_usd,
               min(CASE WHEN t.side='BUY'  THEN t.ts END)          AS first_buy_ts,
               max(t.ts)                                           AS last_ts,
               count(*)                                            AS n_trades
        FROM trades t LEFT JOIN markets m USING (condition_id)
        WHERE t.proxy_wallet = ?
        GROUP BY t.condition_id, t.outcome_index
        """,
        [wallet.lower()],
    )
    if df.empty:
        return df

    for col in ("buy_shares", "sell_shares", "buy_usd", "sell_usd", "market_volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["net_shares"] = df.buy_shares - df.sell_shares
    df["entry"] = df.buy_usd / df.buy_shares.replace(0, np.nan)

    winner = pd.to_numeric(df.winner, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    outcome = pd.to_numeric(df.outcome_index, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    resolved = df.resolved.fillna(False).astype(bool).to_numpy()
    decided = resolved & np.isfinite(winner)
    df["won"] = np.where(decided, (outcome == winner).astype(float), np.nan)

    payout = np.where(df.won.to_numpy() == 1.0, 1.0, 0.0)
    df["pnl"] = np.where(
        decided,
        df.sell_usd + df.net_shares * payout - df.buy_usd,
        np.nan,
    )
    df["roi"] = df.pnl / df.buy_usd.replace(0, np.nan)
    df["status"] = np.where(
        decided, np.where(df.won.to_numpy() == 1.0, "won", "lost"), "open"
    )
    df["bet_key"] = df.condition_id + ":" + df.outcome_index.astype(str)
    return df.sort_values("first_buy_ts", ascending=False)


def performance(db: Database, wallets: list[str]) -> dict[str, Any]:
    """Realised performance for one wallet or a whole group.

    Every ROI here is realised: settled P&L over the dollars staked on settled
    bets, so an open book cannot flatter the number. The longshot / favourite
    split is the discriminating one - a high overall ROI earned entirely above
    50c is favourite-grinding, the same figure below 50c is information.
    """
    wallets = [w.lower() for w in wallets if w]
    if not wallets:
        return {}
    marks = ",".join("?" * len(wallets))
    df = db.query(
        f"""
        SELECT t.proxy_wallet, t.condition_id, t.outcome_index,
               any_value(m.resolved)         AS resolved,
               any_value(m.winning_outcome)  AS winner,
               sum(CASE WHEN t.side='BUY'  THEN t.size ELSE 0 END) AS buy_shares,
               sum(CASE WHEN t.side='BUY'  THEN t.usd  ELSE 0 END) AS buy_usd,
               sum(CASE WHEN t.side='SELL' THEN t.size ELSE 0 END) AS sell_shares,
               sum(CASE WHEN t.side='SELL' THEN t.usd  ELSE 0 END) AS sell_usd
        FROM trades t LEFT JOIN markets m USING (condition_id)
        WHERE t.proxy_wallet IN ({marks})
        GROUP BY t.proxy_wallet, t.condition_id, t.outcome_index
        """,
        wallets,
    )
    if df.empty:
        return {}
    for col in ("buy_shares", "buy_usd", "sell_shares", "sell_usd"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df = df[df.buy_shares > 0].copy()
    if df.empty:
        return {}
    df["entry"] = df.buy_usd / df.buy_shares

    winner = pd.to_numeric(df.winner, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    outcome = pd.to_numeric(df.outcome_index, errors="coerce").to_numpy(dtype="float64", na_value=np.nan)
    decided = df.resolved.fillna(False).astype(bool).to_numpy() & np.isfinite(winner)
    won = np.where(decided, (outcome == winner).astype(float), np.nan)
    payout = np.where(won == 1.0, 1.0, 0.0)
    net = (df.buy_shares - df.sell_shares).to_numpy()
    pnl = np.where(decided, df.sell_usd.to_numpy() + net * payout - df.buy_usd.to_numpy(), np.nan)

    def _roi(mask: np.ndarray) -> tuple[float, float, float]:
        staked = float(df.buy_usd.to_numpy()[mask].sum())
        gained = float(np.nansum(pnl[mask]))
        return (gained / staked if staked > 0 else np.nan), gained, staked

    settled = decided
    longshot = settled & (df.entry.to_numpy() < 0.5)
    favourite = settled & (df.entry.to_numpy() >= 0.5)

    roi, pnl_total, staked_settled = _roi(settled)
    longshot_roi, longshot_pnl, longshot_staked = _roi(longshot)
    favourite_roi, favourite_pnl, favourite_staked = _roi(favourite)
    return {
        "staked_total": float(df.buy_usd.sum()),
        "staked_settled": staked_settled,
        "realised_pnl": pnl_total,
        "roi": roi,
        "longshot_roi": longshot_roi,
        "longshot_pnl": longshot_pnl,
        "longshot_staked": longshot_staked,
        "favourite_roi": favourite_roi,
        "favourite_pnl": favourite_pnl,
        "favourite_staked": favourite_staked,
        "settled_bets": int(settled.sum()),
        "winrate": float(np.nanmean(won)) if settled.any() else np.nan,
    }


def entry_price_histogram(positions: pd.DataFrame, bins: int = PRICE_BINS) -> pd.DataFrame:
    """Dollars staked per entry-price bucket.

    Weighted by money rather than bet count: ten $200 dabbles at 0.9 should not
    outweigh one $50k conviction bet at 0.15.
    """
    edges = np.linspace(0.0, 1.0, bins + 1)
    out = pd.DataFrame({
        "left": edges[:-1], "right": edges[1:],
        "centre": (edges[:-1] + edges[1:]) / 2.0,
        "usd": np.zeros(bins), "won_usd": np.zeros(bins), "n": np.zeros(bins, dtype=int),
    })
    if positions is None or positions.empty or "entry" not in positions:
        return out
    entry = pd.to_numeric(positions.entry, errors="coerce").to_numpy(dtype=float)
    usd = pd.to_numeric(positions.buy_usd, errors="coerce").to_numpy(dtype=float)
    won = pd.to_numeric(positions.get("won"), errors="coerce").to_numpy(dtype=float) \
        if "won" in positions else np.full(len(entry), np.nan)
    ok = np.isfinite(entry) & np.isfinite(usd)
    idx = np.clip(np.digitize(entry[ok], edges) - 1, 0, bins - 1)
    for i, u, w in zip(idx, usd[ok], won[ok]):
        out.loc[i, "usd"] += u
        out.loc[i, "n"] += 1
        if w == 1.0:
            out.loc[i, "won_usd"] += u
    out["win_share"] = np.divide(
        out.won_usd, out.usd, out=np.full(bins, np.nan), where=out.usd > 0
    )
    return out


def pnl_curve(positions: pd.DataFrame) -> pd.DataFrame:
    """Cumulative realised P&L and stake, ordered by when each bet was entered."""
    if positions is None or positions.empty:
        return pd.DataFrame(columns=["ts", "cum_pnl", "cum_staked", "question", "pnl"])
    done = positions[positions.status.isin(("won", "lost"))].copy()
    if done.empty:
        return pd.DataFrame(columns=["ts", "cum_pnl", "cum_staked", "question", "pnl"])
    done["ts"] = pd.to_numeric(done.first_buy_ts, errors="coerce").fillna(0).astype("int64")
    done = done.sort_values("ts")
    done["cum_pnl"] = done.pnl.fillna(0.0).cumsum()
    done["cum_staked"] = done.buy_usd.fillna(0.0).cumsum()
    return done[["ts", "cum_pnl", "cum_staked", "question", "pnl", "buy_usd", "status"]]


def wallet_cotraders(db: Database, wallet: str, limit: int = 10) -> pd.DataFrame:
    """Who else keeps holding the same bets, and how much of the book they cover."""
    wallet = wallet.lower()
    df = db.query(
        """
        WITH mine AS (
            SELECT DISTINCT condition_id, outcome_index FROM trades
            WHERE proxy_wallet = ? AND side = 'BUY'
        ),
        mine_n AS (SELECT count(*) AS n FROM mine)
        SELECT t.proxy_wallet,
               count(DISTINCT t.condition_id || ':' || t.outcome_index) AS shared,
               (SELECT n FROM mine_n) AS mine_total,
               sum(CASE WHEN t.side='BUY' THEN t.usd ELSE 0 END) AS usd
        FROM trades t JOIN mine USING (condition_id, outcome_index)
        WHERE t.proxy_wallet <> ? AND t.side = 'BUY'
        GROUP BY t.proxy_wallet
        ORDER BY shared DESC
        LIMIT ?
        """,
        [wallet, wallet, limit],
    )
    if df.empty:
        return df
    df["share"] = df.shared / df.mine_total.replace(0, np.nan)
    names = db.query("SELECT proxy_wallet, name, pseudonym FROM users")
    lut = {}
    if not names.empty:
        for r in names.itertuples():
            lut[r.proxy_wallet] = _as_text(r.name) or _as_text(r.pseudonym)
    df["display"] = [lut.get(w) or w[:10] for w in df.proxy_wallet]
    return df


def bet_price_series(db: Database, condition_id: str, outcome_index: int,
                     buckets: int = 140) -> pd.DataFrame:
    """Traded price over time for one outcome, as volume-weighted buckets.

    This is the market's own record rather than a quoted price feed: it is what
    people actually paid, which is the thing a watched entry should be judged
    against.
    """
    df = db.query(
        """
        SELECT ts, price, size, usd FROM trades
        WHERE condition_id = ? AND outcome_index = ?
        ORDER BY ts
        """,
        [condition_id, int(outcome_index)],
    )
    if df.empty:
        return pd.DataFrame(columns=["ts", "vwap", "usd"])
    lo, hi = int(df.ts.min()), int(df.ts.max())
    if hi <= lo:
        return pd.DataFrame({"ts": [lo], "vwap": [float(df.usd.sum() / max(df["size"].sum(), 1e-9))],
                             "usd": [float(df.usd.sum())]})
    width = max((hi - lo) // buckets, 60)
    df["bucket"] = ((df.ts - lo) // width) * width + lo
    grouped = df.groupby("bucket").agg(usd=("usd", "sum"), shares=("size", "sum")).reset_index()
    grouped["vwap"] = grouped.usd / grouped.shares.replace(0, np.nan)
    return grouped.rename(columns={"bucket": "ts"})[["ts", "vwap", "usd"]].dropna(subset=["vwap"])


def bet_entrants(db: Database, condition_id: str, outcome_index: int,
                 limit: int = 400) -> pd.DataFrame:
    """Every wallet that bought this side, when they got in and at what price."""
    df = db.query(
        """
        SELECT t.proxy_wallet,
               sum(t.usd)                    AS usd,
               sum(t.usd) / nullif(sum(t.size), 0) AS entry,
               min(t.ts)                     AS first_ts
        FROM trades t
        WHERE t.condition_id = ? AND t.outcome_index = ? AND t.side = 'BUY'
        GROUP BY t.proxy_wallet
        ORDER BY usd DESC
        LIMIT ?
        """,
        [condition_id, int(outcome_index), limit],
    )
    if df.empty:
        return df
    names = db.query("SELECT proxy_wallet, name, pseudonym FROM users")
    lut = {}
    if not names.empty:
        for r in names.itertuples():
            lut[r.proxy_wallet] = _as_text(r.name) or _as_text(r.pseudonym)
    df["display"] = [lut.get(w) or w[:10] for w in df.proxy_wallet]
    df["rank"] = np.arange(1, len(df) + 1)
    return df


def cluster_members(db: Database, wallets: list[str]) -> pd.DataFrame:
    """Per-member totals for an arbitrary set of wallets."""
    if not wallets:
        return pd.DataFrame()
    marks = ",".join("?" * len(wallets))
    df = db.query(
        f"""
        SELECT t.proxy_wallet,
               count(DISTINCT t.condition_id || ':' || t.outcome_index) AS bets,
               sum(CASE WHEN t.side='BUY' THEN t.usd ELSE 0 END) AS staked,
               min(t.ts) AS first_ts, max(t.ts) AS last_ts
        FROM trades t WHERE t.proxy_wallet IN ({marks})
        GROUP BY t.proxy_wallet ORDER BY staked DESC
        """,
        list(wallets),
    )
    if df.empty:
        return df
    names = db.query("SELECT proxy_wallet, name, pseudonym FROM users")
    lut = {}
    if not names.empty:
        for r in names.itertuples():
            lut[r.proxy_wallet] = _as_text(r.name) or _as_text(r.pseudonym)
    df["display"] = [lut.get(w) or w[:10] for w in df.proxy_wallet]
    df["share"] = df.staked / max(df.staked.sum(), 1e-9)
    return df


def cluster_overlap(db: Database, wallets: list[str]) -> tuple[list[str], np.ndarray]:
    """Member-by-member count of bets held in common.

    The diagonal is each member's own bet count, so a row reads as "of the N
    bets this wallet holds, here is how many each of the others also holds" -
    which is the shape of the question the cluster exists to answer.
    """
    wallets = [w for w in wallets if w]
    if len(wallets) < 2:
        return wallets, np.zeros((len(wallets), len(wallets)))
    marks = ",".join("?" * len(wallets))
    df = db.query(
        f"""
        SELECT proxy_wallet, condition_id || ':' || outcome_index AS bet_key
        FROM trades WHERE proxy_wallet IN ({marks}) AND side = 'BUY'
        GROUP BY proxy_wallet, bet_key
        """,
        list(wallets),
    )
    sets = {w: set(df[df.proxy_wallet == w].bet_key) for w in wallets}
    n = len(wallets)
    matrix = np.zeros((n, n))
    for i, a in enumerate(wallets):
        for j, b in enumerate(wallets):
            matrix[i, j] = len(sets[a] & sets[b]) if i != j else len(sets[a])
    return wallets, matrix


def recent_trades(db: Database, wallet: str, limit: int = 200) -> pd.DataFrame:
    """Raw trade tape for a wallet, newest first."""
    return db.query(
        """
        SELECT t.ts, t.side, t.price, t.size, t.usd, t.outcome,
               m.question, t.condition_id
        FROM trades t LEFT JOIN markets m USING (condition_id)
        WHERE t.proxy_wallet = ?
        ORDER BY t.ts DESC LIMIT ?
        """,
        [wallet.lower(), limit],
    )
