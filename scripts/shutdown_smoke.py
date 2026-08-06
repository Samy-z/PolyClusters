"""Prove the app leaves nothing running.

Starts a real background job, closes the window while it is still in flight,
and checks that every worker thread is gone and the database handle released.
The interesting case is not a clean idle exit - it is closing mid-fetch.

Run:  .venv/Scripts/python.exe scripts/shutdown_smoke.py
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
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

from polyclusters.config import AnalysisFilters, AppSettings  # noqa: E402
from polyclusters.core.db import Database  # noqa: E402
from polyclusters.ui.main_window import MainWindow  # noqa: E402

SRC = ROOT / "scripts" / "_smoke.duckdb"
WORK = ROOT / "scripts" / "_shutdown.duckdb"


def qthreads_alive(win: MainWindow) -> list[str]:
    return [label for w, label in win._workers if w is not None and w.isRunning()]


def main() -> int:
    if not SRC.exists():
        print("Run scripts/smoke_test.py first to build the sample database.")
        return 1
    for f in WORK.parent.glob("_shutdown.duckdb*"):
        f.unlink()
    shutil.copy(SRC, WORK)

    app = QApplication(sys.argv)
    settings = AppSettings()
    db = Database(WORK)
    win = MainWindow(db, settings)
    win.showMaximized()
    app.processEvents()

    print(f"1. tray created: {win.tray is not None}")
    if win.tray is not None:
        actions = [a.text() for a in win.tray._menu.actions() if a.text()]
        print(f"   menu: {actions}")
        assert "Terminate all tasks" in actions

    # A real network job, deliberately wide so it is still running when we close.
    print("\n2. starting a real fetch, then closing the window mid-flight...")
    settings.max_markets_per_fetch = 400
    settings.min_market_volume = 5_000
    win.start_ingest(AnalysisFilters(tag_ids=[2], min_market_volume=5_000), False)
    app.processEvents()

    deadline = time.time() + 20
    while not qthreads_alive(win) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
    running = qthreads_alive(win)
    print(f"   in flight when we pull the plug: {running}")
    assert running, "the job never started; nothing was proven"

    threads_before = threading.active_count()
    t0 = time.time()
    win.close()
    app.processEvents()
    elapsed = time.time() - t0

    left = qthreads_alive(win)
    print(f"\n3. after close ({elapsed:.1f}s):")
    print(f"   QThreads still running : {left or 'none'}")
    print(f"   python threads         : {threads_before} -> {threading.active_count()}")
    assert not left, f"workers survived the close: {left}"

    # The database handle must be released, or the next launch cannot open it.
    try:
        probe = Database(WORK)
        probe.close()
        print("   database handle        : released (reopened cleanly)")
    except Exception as exc:  # noqa: BLE001
        print(f"   database handle        : STILL HELD — {exc}")
        return 1

    print("\n4. terminate_all_tasks on an idle app is a no-op:")
    print(f"   returned {win.terminate_all_tasks()} survivors")

    for f in WORK.parent.glob("_shutdown.duckdb*"):
        f.unlink()
    print("\nShutdown smoke test passed — nothing left running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
