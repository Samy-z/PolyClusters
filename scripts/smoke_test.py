"""End-to-end backend smoke test: ingest a small slice, then cluster it.

Run:  .venv/Scripts/python.exe scripts/smoke_test.py
Uses a throwaway database so it never touches the app's real store.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Market titles carry non-cp1252 characters; the Windows console default would
# otherwise abort the run on a print().
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import pandas as pd

from polyclusters.analysis.cluster import ClusterParams
from polyclusters.analysis.engine import run_analysis
from polyclusters.config import AnalysisFilters, AppSettings
from polyclusters.core.db import Database
from polyclusters.ingest.pipeline import run_ingest, subtract_covered

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)


def test_interval_math() -> None:
    assert subtract_covered((0, 100), []) == [(0, 100)]
    assert subtract_covered((0, 100), [(0, 100)]) == []
    assert subtract_covered((0, 100), [(20, 40)]) == [(0, 19), (41, 100)]
    assert subtract_covered((0, 100), [(0, 40), (60, 100)]) == [(41, 59)]
    assert subtract_covered((0, 100), [(-50, 150)]) == []
    print("[ok] interval coverage math")


def main() -> int:
    test_interval_math()

    db_file = Path(__file__).parent / "_smoke.duckdb"
    reuse = db_file.exists() and "--fresh" not in sys.argv
    if db_file.exists() and not reuse:
        db_file.unlink()
    db = Database(db_file)

    settings = AppSettings()
    settings.max_concurrency = 6
    settings.requests_per_second = 10
    # Whale-only slice keeps the smoke test fast and mirrors real usage.
    settings.min_trade_usd = 2_000
    settings.min_user_usd = 20_000
    settings.min_user_bets = 2
    settings.min_shared_bets = 2

    # A one-month window over a mid-sized sector.
    end_ts = 1_762_000_000       # 2025-11-01
    start_ts = end_ts - 30 * 86_400
    filters = AnalysisFilters(
        start_ts=start_ts,
        end_ts=end_ts,
        tag_ids=[2],             # "Politics"
        min_market_volume=2_000_000,
    )

    if reuse:
        print("\n=== INGEST (skipped, reusing cached DB; pass --fresh to redo) ===")
    else:
        print("\n=== INGEST ===")
        t0 = time.time()
        report = run_ingest(
            db, settings, filters, refresh_tags=True,
            progress=lambda m: print(m),
            status=lambda d, t, label: (
                print(f"  [{d}/{t}] {label}") if d % 25 == 0 or d == t else None
            ),
        )
        print(f"\n{report.summary()}  ({time.time() - t0:.0f}s)")
    print("DB stats:", db.stats())

    print("\n=== ANALYSIS ===")
    params = ClusterParams(
        min_shared_bets=settings.min_shared_bets,
        similarity_threshold=settings.similarity_threshold,
        louvain_resolution=settings.louvain_resolution,
        min_cluster_size=settings.min_cluster_size,
        max_bet_user_frac=settings.max_bet_user_frac,
        timing_window_hours=settings.timing_window_hours,
        core_pct=settings.unanimity_core_pct,
    )
    result = run_analysis(db, filters, settings, params, progress=lambda m: print(m))

    if not result.ok:
        print("\nNo clusters produced. stats:", result.stats)
        return 1

    print("\n--- TOP CLUSTERS ---")
    cols = [c for c in [
        "cluster_id", "n_members", "n_bets", "n_shared_bets", "n_unanimous_bets",
        "total_usd", "winrate", "longshot_winrate", "resolved_roi",
        "mean_edge_per_share", "unanimity_rate", "sync_rate",
        "median_entry_spread_h", "mean_rarity_idf", "concordance",
        "avg_pairwise_sim", "density", "suspicion_score",
    ] if c in result.clusters.columns]
    print(result.clusters[cols].head(12).to_string(index=False))
    print("\ncluster size distribution:",
          result.clusters.n_members.describe()[["count", "mean", "min", "50%", "max"]].to_dict())

    top = int(result.clusters.iloc[0].cluster_id)
    print(f"\n--- MEMBERS OF CLUSTER {top} ---")
    mcols = [c for c in [
        "display", "proxy_wallet", "n_bets", "n_shared_bets", "total_usd",
        "winrate", "roi", "first_mover_count", "first_mover_rate",
        "avg_lead_time_h", "avg_entry_vs_cluster", "is_biggest_bettor",
    ] if c in result.members.columns]
    print(result.cluster_members(top)[mcols].to_string(index=False))

    print(f"\n--- BETS OF CLUSTER {top} ---")
    bcols = [c for c in [
        "question", "outcome", "n_members", "unanimous", "total_usd", "avg_entry",
        "entry_spread_h", "price_spread", "won", "roi", "rarity_idf",
    ] if c in result.bets.columns]
    print(result.cluster_bets(top)[bcols].head(15).to_string(index=False))

    print("\nstats:", result.stats)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
