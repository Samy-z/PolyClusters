"""Degenerate-input tests for the analysis pipeline.

These are the shapes that crash rather than the shapes that are interesting:
a run with only unresolved markets (so every win-rate column is missing), a run
that finds a single cluster (so there is no spread to z-score against), and the
combination of the two, which is what actually broke.

Run:  .venv/Scripts/python.exe scripts/edge_case_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from polyclusters.analysis.cluster import ClusterParams  # noqa: E402
from polyclusters.analysis.engine import run_analysis  # noqa: E402
from polyclusters.analysis.metrics import _robust_z, compute_suspicion  # noqa: E402
from polyclusters.config import AnalysisFilters, AppSettings  # noqa: E402
from polyclusters.core.db import Database  # noqa: E402

WORK = ROOT / "scripts" / "_edge.duckdb"


def market_row(cid: str, question: str, resolved: bool, winner: int | None) -> dict:
    return dict(
        condition_id=cid, market_id=cid, question=question, slug=cid,
        event_id="", event_slug="", event_title="", start_ts=1_700_000_000,
        end_ts=1_700_900_000, closed=resolved, resolved=resolved,
        volume=500_000.0, liquidity=1_000.0, n_outcomes=2, outcomes_json='["Yes","No"]',
        outcome_prices_json="[]", winning_outcome=winner, neg_risk=False,
        clob_token_ids_json="[]", ingested_at=0,
    )


def trade(uid: str, cid: str, wallet: str, outcome: int, usd: float, ts: int) -> dict:
    return dict(
        trade_uid=uid, tx_hash=uid, condition_id=cid, proxy_wallet=wallet,
        asset=f"{cid}-{outcome}", side="BUY", outcome_index=outcome, outcome="Yes",
        size=usd / 0.4, price=0.4, usd=usd, ts=ts,
    )


def build(resolved: bool) -> Database:
    for f in WORK.parent.glob("_edge.duckdb*"):
        f.unlink()
    db = Database(WORK)
    markets, trades = [], []
    # Two wallets betting identically across several obscure markets: exactly
    # one cluster comes out, which is the degenerate case for z-scoring.
    for i in range(6):
        cid = f"0xmarket{i:02d}"
        markets.append(market_row(cid, f"Question {i}?", resolved, (i % 2) if resolved else None))
        for w, wallet in enumerate(("0xaaa", "0xbbb")):
            trades.append(trade(f"t{i}{w}", cid, wallet, 0, 30_000.0, 1_700_100_000 + i * 3600))
    db.upsert_markets(pd.DataFrame(markets))
    db.upsert_trades(pd.DataFrame(trades))
    return db


def run(db: Database, label: str) -> None:
    settings = AppSettings()
    settings.min_user_usd = 10_000
    settings.min_user_bets = 2
    params = ClusterParams(similarity_threshold=0.5, min_shared_bets=3, min_cluster_size=2)
    result = run_analysis(db, AnalysisFilters(), settings, params)
    n = len(result.clusters)
    print(f"  {label:<34} -> {n} cluster(s)", end="")
    if n:
        row = result.clusters.iloc[0]
        print(f", suspicion={row.suspicion_score:.3f}, pct={row.suspicion_pct:.0f}, "
              f"winrate={row.get('winrate')}")
    else:
        print()


def main() -> int:
    print("1. _robust_z against degenerate input:")
    cases = {
        "all pandas NA (nullable dtype)": pd.Series([pd.NA, pd.NA, pd.NA], dtype="Float64"),
        "single value": pd.Series([1.0]),
        "single value, nullable": pd.Series([1.0], dtype="Float64"),
        "all identical": pd.Series([2.0, 2.0, 2.0]),
        "one real value among NA": pd.Series([pd.NA, 5.0, pd.NA], dtype="Float64"),
        "empty": pd.Series([], dtype="float64"),
        "normal spread": pd.Series([1.0, 2.0, 3.0, 10.0]),
    }
    for label, series in cases.items():
        z = _robust_z(series)
        assert not z.isna().any(), f"{label}: produced NaN"
        print(f"   [ok] {label:<32} -> {list(np.round(z.to_numpy(dtype=float), 2))}")

    print("\n2. compute_suspicion on a one-row, all-missing frame:")
    frame = pd.DataFrame({
        "cluster_id": [1], "n_members": [2],
        "winrate": pd.Series([pd.NA], dtype="Float64"),
        "resolved_roi": pd.Series([pd.NA], dtype="Float64"),
        "resolved_positions": pd.Series([pd.NA], dtype="Int64"),
        "mean_rarity_idf": pd.Series([pd.NA], dtype="Float64"),
        "avg_usd_per_member": [60000.0],
    })
    scored = compute_suspicion(frame, ClusterParams())
    print(f"   [ok] score={scored.suspicion_score.iloc[0]:.3f} "
          f"pct={scored.suspicion_pct.iloc[0]:.0f}")

    print("\n3. full pipeline on synthetic data:")
    db = build(resolved=True)
    run(db, "all markets resolved")
    db.close()
    db = build(resolved=False)
    run(db, "all markets UNRESOLVED")   # the reported crash
    db.close()

    for f in WORK.parent.glob("_edge.duckdb*"):
        f.unlink()
    print("\nEdge-case smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
