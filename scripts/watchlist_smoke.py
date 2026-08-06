"""Exercise the watchlist end to end: star rows, verify identity survives a
re-run, generate events from a snapshot diff, and remove items.

Run:  .venv/Scripts/python.exe scripts/watchlist_smoke.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from PySide6.QtWidgets import QApplication  # noqa: E402

from polyclusters.analysis.cluster import ClusterParams  # noqa: E402
from polyclusters.analysis.engine import run_analysis  # noqa: E402
from polyclusters.config import AnalysisFilters, AppSettings  # noqa: E402
from polyclusters.core.db import Database  # noqa: E402
from polyclusters.core.watchlist import (  # noqa: E402
    WatchlistStore, diff_wallet, match_cluster, observe_wallet, wallet_signals,
)
from polyclusters.ui.main_window import MainWindow  # noqa: E402

SRC = ROOT / "scripts" / "_smoke.duckdb"
WORK = ROOT / "scripts" / "_watch.duckdb"


def main() -> int:
    if not SRC.exists():
        print("Run scripts/smoke_test.py first to build the sample database.")
        return 1
    for f in WORK.parent.glob("_watch.duckdb*"):
        f.unlink()
    shutil.copy(SRC, WORK)

    app = QApplication(sys.argv)
    db = Database(WORK)
    settings = AppSettings()
    settings.min_user_usd = 20_000
    settings.min_user_bets = 2
    win = MainWindow(db, settings)
    win.showMaximized()
    app.processEvents()

    params = ClusterParams(
        similarity_threshold=settings.similarity_threshold,
        louvain_resolution=settings.louvain_resolution,
        max_bet_user_frac=settings.max_bet_user_frac,
    )
    result = run_analysis(db, AnalysisFilters(), settings, params)
    win._on_analysis_done(result)
    app.processEvents()
    print(f"1. Analysis: {len(result.clusters)} clusters, {len(result.members)} members")

    store: WatchlistStore = win.watch
    cp = win.clusters_panel

    # Start from a known-empty watchlist. The source database is shared with the
    # other smoke tests, and a leftover row would make the first toggle below
    # remove an item instead of adding one - which fails several steps later,
    # a long way from the cause.
    for existing in store.items():
        store.remove(existing.item_id)
    assert not store.count_by_kind(), "watchlist did not start empty"

    # --- star one of each kind, through the same path a click takes --------
    print("\n2. Starring one row of each kind via the star column...")
    for table, kind in (
        (cp.clusters_table, "cluster"), (cp.members_table, "member"),
        (cp.bets_table, "bet"), (cp.positions_table, "position"),
    ):
        idx = table.proxy.index(0, 0)
        assert table.model.column_spec(0).fmt == "star", f"{kind}: no star column"
        table._maybe_toggle_star(idx)
        app.processEvents()
    counts = store.count_by_kind()
    print(f"   watchlist now: {counts}")
    assert set(counts) == {"cluster", "member", "bet", "position"}, counts

    # --- the star must render as filled afterwards -------------------------
    filled = cp.members_table.model.dataframe["_watched"].any()
    print(f"3. Star renders filled after click: {bool(filled)}")
    assert filled

    # --- identity survives a completely different run ----------------------
    print("\n4. Re-running analysis with different parameters...")
    watched_wallet = store.items("member")[0].ref["wallet"]
    watched_cluster = store.items("cluster")[0]
    result2 = run_analysis(
        db, AnalysisFilters(), settings,
        ClusterParams(similarity_threshold=0.25, louvain_resolution=2.5,
                      max_bet_user_frac=settings.max_bet_user_frac),
    )
    win._on_analysis_done(result2)
    app.processEvents()
    print(f"   run 2 produced {len(result2.clusters)} clusters "
          f"(run 1 had {len(result.clusters)})")
    assert store.is_watched("member", {"wallet": watched_wallet}), "wallet watch lost"
    print("   wallet still watched: yes")

    cid, drift = match_cluster(watched_cluster.ref["wallets"], result2)
    print(f"   watched cluster re-identified as cluster {cid} "
          f"(overlap {drift.get('jaccard', 0):.0%}, "
          f"+{len(drift.get('joined', []))} joined, -{len(drift.get('left', []))} left)")

    # --- signals -----------------------------------------------------------
    print("\n5. Trader signals computed from local data:")
    sig = wallet_signals(db, watched_wallet)
    for k in ("n_bets", "staked", "winrate", "longshot_winrate", "edge_per_share",
              "top_cotrader_share", "median_market_volume"):
        if k in sig:
            print(f"   {k:<22}: {sig[k]}")
    assert sig.get("n_bets", 0) > 0

    # --- event generation via snapshot diff --------------------------------
    print("\n6. Snapshot diff generates events...")
    item = store.items("member")[0]
    after = observe_wallet(db, watched_wallet)
    trimmed = {"bets": dict(list(after["bets"].items())[:-2]), "total_usd": 0.0}
    store.save_snapshot(item.item_id, trimmed)
    events = diff_wallet(trimmed, after)
    for kind, severity, summary, _detail in events:
        store.record_event(item.item_id, kind, severity, summary)
    print(f"   {len(events)} event(s) recorded, e.g.:")
    for _k, sev, summary, _d in events[:3]:
        print(f"     [{sev}] {summary[:90]}")
    assert events, "diff produced no events"

    # --- panel renders them ------------------------------------------------
    wp = win.watchlist_panel
    wp.reload()
    app.processEvents()
    print("\n7. Watchlist panel tables:")
    for name, table in (("traders", wp.traders_table), ("bets", wp.bets_table),
                        ("clusters", wp.clusters_table), ("positions", wp.positions_table),
                        ("events", wp.events_table)):
        print(f"   {name:<9}: {len(table.model.dataframe)} row(s)")
    assert len(wp.events_table.model.dataframe) > 0
    print(f"   unseen badge count: {store.unseen_count()}")

    # --- persistence across a restart --------------------------------------
    print("\n8. Reopening the database (simulating a restart)...")
    db.close()
    db2 = Database(WORK)
    store2 = WatchlistStore(db2)
    print(f"   watchlist after reopen: {store2.count_by_kind()}")
    print(f"   events after reopen   : {len(store2.events())}")
    assert store2.count_by_kind() == counts, "watchlist did not persist"

    # --- removal -----------------------------------------------------------
    store2.remove(item.item_id)
    print(f"\n9. After removing one item: {store2.count_by_kind()}")
    assert "member" not in store2.count_by_kind()
    assert len(store2.events(item.item_id)) == 0, "events not cleaned up with the item"
    print("   its events were cleaned up too")

    db2.close()
    print("\nWatchlist smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
