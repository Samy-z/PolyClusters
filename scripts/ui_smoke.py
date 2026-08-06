"""Headless UI test: build the window against the smoke DB, run an analysis,
populate every panel, and save screenshots.

Run:  .venv/Scripts/python.exe scripts/ui_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from polyclusters.analysis.cluster import ClusterParams  # noqa: E402
from polyclusters.analysis.engine import run_analysis  # noqa: E402
from polyclusters.config import AnalysisFilters, AppSettings  # noqa: E402
from polyclusters.core.db import Database  # noqa: E402
from polyclusters.ui.main_window import MainWindow  # noqa: E402

SHOTS = ROOT / "screenshots"


def grab(widget, name: str) -> None:
    SHOTS.mkdir(exist_ok=True)
    path = SHOTS / f"{name}.png"
    widget.grab().save(str(path))
    print(f"  saved {path.relative_to(ROOT)}")


def main() -> int:
    db_file = ROOT / "scripts" / "_smoke.duckdb"
    if not db_file.exists():
        print("Run scripts/smoke_test.py first to build the sample database.")
        return 1

    app = QApplication(sys.argv)
    settings = AppSettings()
    settings.min_user_usd = 20_000
    settings.min_user_bets = 2
    db = Database(db_file)
    win = MainWindow(db, settings)
    # Maximised on the primary screen, exactly as the app launches, so the
    # screenshots show the layout a user actually gets.
    win.showMaximized()

    filters = AnalysisFilters(start_ts=0, end_ts=0, min_market_volume=0)
    params = ClusterParams(
        similarity_threshold=settings.similarity_threshold,
        louvain_resolution=settings.louvain_resolution,
        max_bet_user_frac=settings.max_bet_user_frac,
    )
    print("Running analysis for the UI...")
    result = run_analysis(db, filters, settings, params, progress=lambda m: print("  " + m))
    if not result.ok:
        print("Analysis produced no clusters; the UI test needs data.")
        return 1

    win._on_analysis_done(result)
    app.processEvents()

    def shoot() -> None:
        print("Capturing panels...")
        win.tabs.setCurrentWidget(win.clusters_panel)
        app.processEvents()
        grab(win, "01_clusters_members")

        win.clusters_panel.tabs.setCurrentWidget(win.clusters_panel.bets_table)
        app.processEvents()
        grab(win, "02_cluster_bets")

        win.clusters_panel.tabs.setCurrentWidget(win.clusters_panel.timeline)
        app.processEvents()
        grab(win, "03_entry_timeline")

        win.clusters_panel.tabs.setCurrentWidget(win.clusters_panel.network)
        app.processEvents()
        grab(win, "04_network")

        win.tabs.setCurrentWidget(win.compare_panel)
        app.processEvents()
        grab(win, "05_compare")

        # Star one row of each kind so the watchlist has something to show.
        cp = win.clusters_panel
        starred = [t for t in (cp.clusters_table, cp.members_table, cp.bets_table,
                               cp.positions_table) if t.proxy.rowCount()]
        for table in starred:
            table._maybe_toggle_star(table.proxy.index(0, 0))
        win.tabs.setCurrentWidget(win.watchlist_panel)
        win.watchlist_panel.reload()
        app.processEvents()
        grab(win, "07_watchlist")

        # Un-star them again. This script shares _smoke.duckdb with the other
        # smoke tests, and leaving watchlist rows behind makes the next one
        # start from a state it did not create.
        for table in starred:
            table._maybe_toggle_star(table.proxy.index(0, 0))
        app.processEvents()

        win.tabs.setCurrentWidget(win.data_panel)
        app.processEvents()
        grab(win, "06_data")

        # Exercise the live re-ranking path.
        win.rescore({"weight_roi": 2.0, "weight_winrate": 0.25, "weight_sync": 2.0})
        app.processEvents()
        print("  rescore ok — top cluster now:",
              int(win.result.clusters.iloc[0].cluster_id))

        print("\nUI smoke test passed.")
        app.quit()

    QTimer.singleShot(600, shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
