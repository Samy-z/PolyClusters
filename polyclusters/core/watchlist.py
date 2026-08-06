"""Starred items, and the machinery that keeps an eye on them.

Four kinds of thing can be watched, each with an identity that survives a
re-run:

* ``member``   - a wallet. Stable forever.
* ``bet``      - ``(condition_id, outcome_index)``. Stable forever.
* ``position`` - one wallet in one bet.
* ``cluster``  - has no stable id at all. Cluster numbers are assigned per run,
  so a watched cluster is stored as its *member set* and re-identified on later
  runs by Jaccard overlap. That also gives us drift: who joined, who left.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .db import Database

KINDS = ("cluster", "member", "bet", "position")
KIND_LABELS = {
    "cluster": "Clusters",
    "member": "Traders",
    "bet": "Bets",
    "position": "Positions",
}
# Below this overlap a cluster from a new run is treated as a different group.
CLUSTER_MATCH_MIN_JACCARD = 0.34


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def make_item_id(kind: str, ref: dict[str, Any]) -> str:
    if kind == "member":
        return f"member:{ref['wallet']}"
    if kind == "bet":
        return f"bet:{ref['bet_key']}"
    if kind == "position":
        return f"position:{ref['wallet']}:{ref['bet_key']}"
    if kind == "cluster":
        wallets = sorted(ref.get("wallets") or [])
        return f"cluster:{_hash(*wallets)}"
    raise ValueError(f"unknown watch kind: {kind}")


@dataclass
class WatchItem:
    item_id: str
    kind: str
    label: str
    ref: dict[str, Any]
    note: str = ""
    added_at: int = 0
    last_checked: int = 0
    snapshot: dict[str, Any] = field(default_factory=dict)


class WatchlistStore:
    """CRUD plus the diffing that turns two snapshots into events."""

    def __init__(self, db: Database):
        self.db = db
        self._cache: set[str] | None = None

    # -- membership ---------------------------------------------------------
    def ids(self) -> set[str]:
        if self._cache is None:
            df = self.db.query("SELECT item_id FROM watchlist")
            self._cache = set(df.item_id.tolist()) if not df.empty else set()
        return self._cache

    def is_watched(self, kind: str, ref: dict[str, Any]) -> bool:
        try:
            return make_item_id(kind, ref) in self.ids()
        except (KeyError, ValueError):
            return False

    def add(self, kind: str, ref: dict[str, Any], label: str, note: str = "") -> str:
        item_id = make_item_id(kind, ref)
        now = int(time.time())
        self.db.execute(
            "INSERT INTO watchlist (item_id, kind, label, ref_json, note, added_at, "
            "last_checked, snapshot_json) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (item_id) DO UPDATE SET label = excluded.label",
            [item_id, kind, label, json.dumps(ref), note, now, 0, "{}"],
        )
        self._cache = None
        return item_id

    def remove(self, item_id: str) -> None:
        self.db.execute("DELETE FROM watchlist WHERE item_id = ?", [item_id])
        self.db.execute("DELETE FROM watch_events WHERE item_id = ?", [item_id])
        self._cache = None

    def toggle(self, kind: str, ref: dict[str, Any], label: str) -> bool:
        """Returns True if the item is watched after the call."""
        item_id = make_item_id(kind, ref)
        if item_id in self.ids():
            self.remove(item_id)
            return False
        self.add(kind, ref, label)
        return True

    def set_note(self, item_id: str, note: str) -> None:
        self.db.execute("UPDATE watchlist SET note = ? WHERE item_id = ?", [note, item_id])

    def items(self, kind: str | None = None) -> list[WatchItem]:
        sql = "SELECT * FROM watchlist"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY added_at DESC"
        df = self.db.query(sql, params)
        out = []
        for r in df.itertuples():
            out.append(
                WatchItem(
                    item_id=r.item_id, kind=r.kind, label=r.label,
                    ref=json.loads(r.ref_json or "{}"), note=r.note or "",
                    added_at=int(r.added_at or 0), last_checked=int(r.last_checked or 0),
                    snapshot=json.loads(r.snapshot_json or "{}"),
                )
            )
        return out

    def count_by_kind(self) -> dict[str, int]:
        df = self.db.query("SELECT kind, count(*) AS n FROM watchlist GROUP BY kind")
        return dict(zip(df.kind, df.n)) if not df.empty else {}

    # -- events -------------------------------------------------------------
    def record_event(
        self, item_id: str, kind: str, severity: str, summary: str,
        detail: dict[str, Any] | None = None, ts: int | None = None,
    ) -> None:
        ts = ts or int(time.time())
        eid = _hash(item_id, kind, summary, str(ts))
        self.db.execute(
            "INSERT INTO watch_events VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [eid, item_id, ts, kind, severity, summary, json.dumps(detail or {}), False],
        )

    def events(self, item_id: str | None = None, limit: int = 5000) -> pd.DataFrame:
        sql = """
            SELECT e.*, w.label, w.kind AS item_kind
            FROM watch_events e LEFT JOIN watchlist w USING (item_id)
        """
        params: list[Any] = []
        if item_id:
            sql += " WHERE e.item_id = ?"
            params.append(item_id)
        sql += " ORDER BY e.ts DESC LIMIT ?"
        params.append(limit)
        return self.db.query(sql, params)

    def mark_events_seen(self) -> None:
        self.db.execute("UPDATE watch_events SET seen = TRUE")

    def unseen_count(self) -> int:
        return int(self.db.scalar("SELECT count(*) FROM watch_events WHERE NOT seen") or 0)

    def save_snapshot(self, item_id: str, snapshot: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE watchlist SET snapshot_json = ?, last_checked = ? WHERE item_id = ?",
            [json.dumps(snapshot), int(time.time()), item_id],
        )


# ---------------------------------------------------------------------------
# Observation: build a current picture of each watched item from local data
# ---------------------------------------------------------------------------
def observe_wallet(db: Database, wallet: str) -> dict[str, Any]:
    """Everything the local store knows about one wallet's standing bets."""
    pos = db.query(
        """
        SELECT t.condition_id,
               t.outcome_index,
               any_value(m.question)         AS question,
               any_value(m.resolved)         AS resolved,
               any_value(m.winning_outcome)  AS winner,
               sum(CASE WHEN t.side='BUY'  THEN t.size ELSE 0 END) AS buy_shares,
               sum(CASE WHEN t.side='SELL' THEN t.size ELSE 0 END) AS sell_shares,
               sum(CASE WHEN t.side='BUY'  THEN t.usd  ELSE 0 END) AS buy_usd,
               min(CASE WHEN t.side='BUY'  THEN t.ts END)          AS first_buy_ts,
               max(t.ts)                                           AS last_ts
        FROM trades t LEFT JOIN markets m USING (condition_id)
        WHERE t.proxy_wallet = ?
        GROUP BY t.condition_id, t.outcome_index
        """,
        [wallet.lower()],
    )
    bets: dict[str, Any] = {}
    if not pos.empty:
        pos["net"] = pos.buy_shares - pos.sell_shares
        for r in pos.itertuples():
            key = f"{r.condition_id}:{int(r.outcome_index)}"
            bets[key] = {
                "question": r.question or "",
                "usd": float(r.buy_usd or 0.0),
                "net": float(r.net or 0.0),
                "entry": float(r.buy_usd / r.buy_shares) if r.buy_shares else None,
                "first_buy_ts": int(r.first_buy_ts or 0),
                "last_ts": int(r.last_ts or 0),
                "resolved": bool(r.resolved) if r.resolved is not None else False,
                "won": (
                    None if not r.resolved or r.winner is None
                    else bool(int(r.outcome_index) == int(r.winner))
                ),
            }
    return {"bets": bets, "total_usd": float(sum(b["usd"] for b in bets.values()))}


def observe_bet(db: Database, bet_key: str) -> dict[str, Any]:
    cid, _, idx = bet_key.rpartition(":")
    row = db.query(
        """
        SELECT any_value(m.question) AS question, any_value(m.resolved) AS resolved,
               any_value(m.winning_outcome) AS winner, any_value(m.volume) AS volume,
               count(DISTINCT t.proxy_wallet) AS traders,
               sum(CASE WHEN t.side='BUY' THEN t.usd ELSE 0 END) AS buy_usd,
               max(t.ts) AS last_ts
        FROM markets m LEFT JOIN trades t
          ON t.condition_id = m.condition_id AND t.outcome_index = ?
        WHERE m.condition_id = ?
        """,
        [int(idx or 0), cid],
    )
    if row.empty:
        return {}
    r = row.iloc[0]
    resolved = bool(r.resolved) if r.resolved is not None else False
    winner = None if r.winner is None or (isinstance(r.winner, float) and np.isnan(r.winner)) else int(r.winner)
    return {
        "question": r.question or "",
        "resolved": resolved,
        "won": None if not resolved or winner is None else bool(int(idx or 0) == winner),
        "traders": int(r.traders or 0),
        "buy_usd": float(r.buy_usd or 0.0),
        "volume": float(r.volume or 0.0),
        "last_ts": int(r.last_ts or 0),
    }


def wallet_signals(db: Database, wallet: str, min_usd: float = 100.0) -> dict[str, Any]:
    """Standing profile of one wallet, independent of any clustering run.

    The interesting column is ``top_cotrader_share``: of everything this wallet
    holds, the largest fraction any single other wallet also holds. One wallet
    shadowing 80% of your positions is not a coincidence, and unlike the cluster
    view this survives across runs and filter changes.
    """
    wallet = wallet.lower()
    base = db.query(
        """
        WITH pos AS (
            SELECT t.condition_id, t.outcome_index,
                   sum(CASE WHEN t.side='BUY' THEN t.usd ELSE 0 END)  AS buy_usd,
                   sum(CASE WHEN t.side='BUY' THEN t.size ELSE 0 END) AS buy_shares,
                   min(CASE WHEN t.side='BUY' THEN t.ts END)          AS first_buy_ts
            FROM trades t WHERE t.proxy_wallet = ?
            GROUP BY t.condition_id, t.outcome_index
        )
        SELECT p.*, m.question, m.resolved, m.winning_outcome, m.volume,
               (SELECT max(ts) FROM trades x WHERE x.condition_id = p.condition_id) AS mkt_last_ts
        FROM pos p LEFT JOIN markets m USING (condition_id)
        WHERE p.buy_usd >= ?
        """,
        [wallet, min_usd],
    )
    if base.empty:
        return {"n_bets": 0}

    base["entry"] = base.buy_usd / base.buy_shares.replace(0, np.nan)
    resolved = base[base.resolved.fillna(False).astype(bool)].copy()
    won = (resolved.outcome_index == resolved.winning_outcome) if not resolved.empty else pd.Series(dtype=bool)
    longshot = resolved[resolved.entry < 0.5] if not resolved.empty else resolved

    out: dict[str, Any] = {
        "n_bets": int(len(base)),
        "n_markets": int(base.condition_id.nunique()),
        "staked": float(base.buy_usd.sum()),
        "median_bet": float(base.buy_usd.median()),
        "resolved_bets": int(len(resolved)),
        "winrate": float(won.mean()) if len(won) else np.nan,
        "longshot_winrate": (
            float((longshot.outcome_index == longshot.winning_outcome).mean())
            if len(longshot) else np.nan
        ),
        "median_entry": float(base.entry.median()),
        "median_market_volume": float(base.volume.median()) if base.volume.notna().any() else np.nan,
        "median_hours_before_close": float(
            ((base.mkt_last_ts - base.first_buy_ts) / 3600.0).median()
        ) if base.mkt_last_ts.notna().any() else np.nan,
        "first_seen": int(base.first_buy_ts.min() or 0),
        "last_seen": int(base.first_buy_ts.max() or 0),
    }
    if len(resolved):
        payout = np.where(won, 1.0, 0.0)
        out["edge_per_share"] = float(np.nanmean(payout - resolved.entry.to_numpy()))

    co = db.query(
        """
        WITH mine AS (
            SELECT DISTINCT condition_id, outcome_index FROM trades
            WHERE proxy_wallet = ? AND side = 'BUY'
        )
        SELECT t.proxy_wallet, count(DISTINCT t.condition_id || ':' || t.outcome_index) AS shared
        FROM trades t JOIN mine USING (condition_id, outcome_index)
        WHERE t.proxy_wallet <> ? AND t.side = 'BUY'
        GROUP BY t.proxy_wallet
        ORDER BY shared DESC LIMIT 8
        """,
        [wallet, wallet],
    )
    if not co.empty:
        names = db.query("SELECT proxy_wallet, name, pseudonym FROM users")
        lut = {}
        if not names.empty:
            for r in names.itertuples():
                lut[r.proxy_wallet] = (r.name or r.pseudonym or "")
        out["top_cotraders"] = [
            {"wallet": r.proxy_wallet, "display": lut.get(r.proxy_wallet, "") or r.proxy_wallet[:10],
             "shared": int(r.shared), "share": float(r.shared) / max(out["n_bets"], 1)}
            for r in co.itertuples()
        ]
        out["top_cotrader_share"] = out["top_cotraders"][0]["share"]
    return out


def cluster_drift(watched_wallets: Iterable[str], current: Iterable[str]) -> dict[str, Any]:
    """Compare a stored cluster's membership with a freshly detected one."""
    a, b = set(watched_wallets), set(current)
    union = a | b
    return {
        "jaccard": len(a & b) / len(union) if union else 0.0,
        "stayed": sorted(a & b),
        "left": sorted(a - b),
        "joined": sorted(b - a),
    }


def match_cluster(watched_wallets: Iterable[str], result: Any) -> tuple[int | None, dict[str, Any]]:
    """Find which cluster of a fresh run best matches a watched member set."""
    a = set(watched_wallets)
    if not a or result is None or getattr(result, "members", None) is None or result.members.empty:
        return None, {}
    best_id, best = None, {"jaccard": 0.0}
    for cid, grp in result.members.groupby("cluster_id"):
        d = cluster_drift(a, set(grp.proxy_wallet))
        if d["jaccard"] > best["jaccard"]:
            best_id, best = int(cid), d
    if best["jaccard"] < CLUSTER_MATCH_MIN_JACCARD:
        return None, best
    return best_id, best


def diff_wallet(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str, str, dict]]:
    """(event kind, severity, summary, detail) for one wallet between snapshots."""
    out: list[tuple[str, str, str, dict]] = []
    old_bets = before.get("bets") or {}
    new_bets = after.get("bets") or {}

    for key, cur in new_bets.items():
        prev = old_bets.get(key)
        title = (cur.get("question") or key)[:70]
        if prev is None:
            if old_bets:  # first observation is a baseline, not a flurry of alerts
                out.append((
                    "new_position", "alert",
                    f"Entered “{title}” with ${cur['usd']:,.0f} at {cur.get('entry') or 0:.3f}",
                    {"bet_key": key, **cur},
                ))
            continue
        # A materially larger stake is worth knowing about; noise is not.
        if prev.get("usd") and cur["usd"] > prev["usd"] * 1.25 + 500:
            out.append((
                "added_to", "notable",
                f"Increased “{title}” from ${prev['usd']:,.0f} to ${cur['usd']:,.0f}",
                {"bet_key": key, "from": prev["usd"], "to": cur["usd"]},
            ))
        if prev.get("net", 0) > 0 and cur.get("net", 0) <= 0:
            out.append((
                "exited", "notable", f"Exited “{title}”",
                {"bet_key": key},
            ))
        if not prev.get("resolved") and cur.get("resolved"):
            verdict = "WON" if cur.get("won") else "LOST"
            out.append((
                "resolved", "alert" if cur.get("won") else "info",
                f"“{title}” resolved — {verdict} (${cur['usd']:,.0f} staked)",
                {"bet_key": key, "won": cur.get("won"), "usd": cur["usd"]},
            ))
    return out
