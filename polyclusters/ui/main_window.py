"""Main window: control dock on the left, result tabs on the right."""

from __future__ import annotations

from typing import Any

import pandas as pd
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QDockWidget, QLabel, QMainWindow, QMenu, QMessageBox, QStatusBar, QTabWidget,
    QToolBar, QWidget,
)

# Rendered height of the logo strip, in logical pixels.
BRAND_LOGO_HEIGHT = 68

from ..analysis.cluster import ClusterParams
from ..analysis.engine import AnalysisResult
from ..config import (
    APP_ICO, AnalysisFilters, AppSettings, LOGO_DISPLAY, LOGO_DISPLAY_DARK, LOGO_ICON,
)
from ..core.db import Database
from ..ingest.pipeline import IngestReport
from .panels.clusters_panel import ClustersPanel
from .panels.compare_panel import ComparePanel
from .panels.control_panel import ControlPanel
from .panels.data_panel import DataPanel
from .theme import STYLESHEET
from .workers import AnalysisWorker, IngestWorker, MarketSearchWorker, TagBootstrapWorker


class MainWindow(QMainWindow):
    def __init__(self, db: Database, settings: AppSettings):
        super().__init__()
        self.db = db
        self.settings = settings
        self.result: AnalysisResult | None = None
        self._worker: Any = None

        self.setWindowTitle("PolyClusters")
        self.resize(1680, 980)
        self.setStyleSheet(STYLESHEET)

        # -- left dock ------------------------------------------------------
        self.controls = ControlPanel(db, settings)
        dock = QDockWidget("Controls", self)
        dock.setObjectName("controlsDock")
        dock.setWidget(self.controls)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setMinimumWidth(330)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.controls_dock = dock

        # -- centre ---------------------------------------------------------
        self.tabs = QTabWidget()
        self.clusters_panel = ClustersPanel()
        self.compare_panel = ComparePanel()
        self.data_panel = DataPanel(db)
        self.tabs.addTab(self.clusters_panel, "Clusters")
        self.tabs.addTab(self.compare_panel, "Compare")
        self.tabs.addTab(self.data_panel, "Data && log")
        self.setCentralWidget(self.tabs)

        # -- status bar -----------------------------------------------------
        self.setStatusBar(QStatusBar())
        self.status_label = QLabel("Ready")
        self.statusBar().addWidget(self.status_label, 1)
        self.db_label = QLabel("")
        self.statusBar().addPermanentWidget(self.db_label)

        self._wire()
        self._build_menu()
        self._build_header()
        self._apply_icon()
        self._refresh_db_label()
        self._first_run_hint()

    # -- branding -----------------------------------------------------------
    def _build_header(self) -> None:
        """Logo strip directly beneath the menu bar.

        A top toolbar spans the full window width above the dock area, which is
        what puts the mark in the true top-left corner; a header inside the
        central widget would sit to the right of the Controls dock instead.
        """
        source = LOGO_DISPLAY_DARK if LOGO_DISPLAY_DARK.exists() else LOGO_DISPLAY
        bar = QToolBar("Branding", self)
        bar.setObjectName("brandBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(QSize(1, 1))

        logo = QLabel()
        logo.setObjectName("brandLogo")
        if source.exists():
            pix = QPixmap(str(source))
            if not pix.isNull():
                ratio = self.devicePixelRatioF() or 1.0
                pix.setDevicePixelRatio(ratio)
                logo.setPixmap(
                    pix.scaledToHeight(int(BRAND_LOGO_HEIGHT * ratio), Qt.SmoothTransformation)
                )
        else:  # asset missing: fall back to a plain wordmark
            logo.setText("PolyClusters")
            logo.setObjectName("h1")
        logo.setContentsMargins(10, 4, 12, 4)
        logo.setToolTip("PolyClusters — Polymarket co-betting cluster analysis")
        bar.addWidget(logo)

        # The logo is part of the app's identity, not an optional panel, so its
        # toggle is kept out of the toolbar/dock context menu entirely.
        bar.toggleViewAction().setVisible(False)
        bar.setContextMenuPolicy(Qt.PreventContextMenu)

        self.addToolBar(Qt.TopToolBarArea, bar)
        self.brand_bar = bar

    def createPopupMenu(self) -> QMenu | None:  # noqa: N802
        """Drop the branding bar from the right-click show/hide menu."""
        menu = super().createPopupMenu()
        if menu is None:
            return None
        brand_bar = getattr(self, "brand_bar", None)
        if brand_bar is not None:
            toggle = brand_bar.toggleViewAction()
            for action in menu.actions():
                if action is toggle or action.text() == toggle.text():
                    menu.removeAction(action)
        # Removing the only toolbar entry leaves Qt's dock/toolbar separator
        # dangling at the end of the menu.
        while menu.actions() and menu.actions()[-1].isSeparator():
            menu.removeAction(menu.actions()[-1])
        return menu

    def _apply_icon(self) -> None:
        if APP_ICO.exists():
            self.setWindowIcon(QIcon(str(APP_ICO)))
        elif LOGO_ICON.exists():
            self.setWindowIcon(QIcon(str(LOGO_ICON)))

    # -- setup --------------------------------------------------------------
    def _wire(self) -> None:
        self.controls.ingest_requested.connect(self.start_ingest)
        self.controls.analyse_requested.connect(self.start_analysis)
        self.controls.cancel_requested.connect(self.cancel_job)
        self.controls.market_search_requested.connect(self.search_markets)
        self.controls.weights_changed.connect(self.rescore)
        self.clusters_panel.status.connect(self.set_status)

    def _build_menu(self) -> None:
        run_menu = self.menuBar().addMenu("&Run")
        act_ingest = QAction("&Fetch data", self)
        act_ingest.setShortcut(QKeySequence("Ctrl+D"))
        act_ingest.triggered.connect(
            lambda: self.start_ingest(self.controls.filters(),
                                      self.controls.refresh_tags_check.isChecked())
        )
        run_menu.addAction(act_ingest)

        act_analyse = QAction("&Run analysis", self)
        act_analyse.setShortcut(QKeySequence("Ctrl+R"))
        act_analyse.triggered.connect(
            lambda: self.start_analysis(self.controls.filters(),
                                        self.controls.cluster_params())
        )
        run_menu.addAction(act_analyse)

        act_cancel = QAction("&Cancel", self)
        act_cancel.setShortcut(QKeySequence("Esc"))
        act_cancel.triggered.connect(self.cancel_job)
        run_menu.addAction(act_cancel)

        view_menu = self.menuBar().addMenu("&View")
        act_dock = self.controls_dock.toggleViewAction()
        act_dock.setText("Show &controls")
        view_menu.addAction(act_dock)

        help_menu = self.menuBar().addMenu("&Help")
        act_about = QAction("&How this works", self)
        act_about.triggered.connect(self._show_help)
        help_menu.addAction(act_about)

    def _first_run_hint(self) -> None:
        stats = self.db.stats()
        if not stats["tags"]:
            # The picker reads from the local catalogue, so fill it before the
            # user can reach the fetch button with nothing to tick.
            QTimer.singleShot(100, self.bootstrap_tags)
        if stats["trades"]:
            self.set_status(
                f"{stats['trades']:,} trades across {stats['markets']:,} markets "
                "already stored — press Ctrl+R to analyse."
            )
            return
        self.tabs.setCurrentWidget(self.data_panel)
        self.data_panel.append_log(
            "No data yet.\n"
            "  1. Pick a time window on the left.\n"
            "  2. TICK the sectors you want (e.g. Politics, Geopolitics) — typing\n"
            "     in the search box alone does not scope the run. Press Enter or\n"
            "     'Select matching' to tick everything the search finds.\n"
            "  3. Press '1 · Fetch data from Polymarket'.\n"
            "  4. Then press '2 · Run cluster analysis'.\n\n"
            "Start narrow — one or two sectors and a 30-day window — to see how "
            "long a crawl takes before widening it."
        )
        self.set_status("No local data yet — pick sectors, then fetch (Ctrl+D).")

    # -- jobs ---------------------------------------------------------------
    def _busy(self, busy: bool) -> None:
        self.controls.set_busy(busy)
        if not busy:
            self.data_panel.end_progress()

    def start_ingest(self, filters: AnalysisFilters, refresh_tags: bool) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "Busy", "A job is already running.")
            return
        if not self._confirm_scope(filters):
            return
        settings = self.controls.apply_to_settings()
        settings.save()
        self.tabs.setCurrentWidget(self.data_panel)
        self.data_panel.append_log(f"\n=== FETCH · {filters.describe()} ===")
        self.set_status("Fetching from Polymarket…")

        worker = IngestWorker(self.db, settings, filters, refresh_tags, self)
        worker.message.connect(self.data_panel.append_log)
        worker.progress.connect(self.data_panel.set_progress)
        worker.failed.connect(self._on_failed)
        worker.finished_ok.connect(self._on_ingest_done)
        self._start(worker)

    def start_analysis(self, filters: AnalysisFilters, params: ClusterParams) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "Busy", "A job is already running.")
            return
        if not self.db.stats()["trades"]:
            QMessageBox.information(
                self, "No data",
                "There are no trades in the local database yet.\n"
                "Run 'Fetch data from Polymarket' first.",
            )
            return
        settings = self.controls.apply_to_settings()
        settings.save()
        self.data_panel.append_log(f"\n=== ANALYSE · {filters.describe()} ===")
        self.set_status("Clustering…")

        worker = AnalysisWorker(self.db, settings, filters, params, self)
        worker.message.connect(self.data_panel.append_log)
        worker.failed.connect(self._on_failed)
        worker.finished_ok.connect(self._on_analysis_done)
        self._start(worker)

    def _confirm_scope(self, filters: AnalysisFilters) -> bool:
        """Make an unscoped sweep an explicit choice rather than an accident.

        Typing a sector into the search box without ticking it leaves the run
        unscoped, which silently turns a targeted fetch into an all-sector one.
        """
        if filters.tag_ids or filters.discovery_condition_ids():
            return True
        typed = self.controls.tag_search.text().strip()
        extra = (
            f"<br><br>You typed <b>“{typed}”</b> in the sector search but did not "
            "tick anything. Press <b>Select matching</b> (or Enter in the search "
            "box) to scope the run to it."
            if typed else ""
        )
        reply = QMessageBox.question(
            self,
            "Fetch every sector?",
            "<b>No sectors or markets are selected.</b><br><br>"
            "This fetches across <i>all</i> sectors — an unfiltered 30-day window "
            f"matches around 28,000 markets, capped at "
            f"{self.controls.max_markets.value():,} by volume for this run."
            f"{extra}<br><br>Continue with every sector?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return reply == QMessageBox.Yes

    def bootstrap_tags(self) -> None:
        """Fill the sector picker on first launch so sectors are selectable."""
        existing = getattr(self, "_tag_worker", None)
        if existing is not None and existing.isRunning():
            return
        worker = TagBootstrapWorker(self.db, self.settings, self)
        worker.message.connect(self.data_panel.append_log)
        worker.message.connect(self.set_status)
        worker.failed.connect(self._on_failed)
        worker.finished_ok.connect(self._on_tags_ready)
        self._tag_worker = worker  # keep a reference alive
        worker.start()

    def _on_tags_ready(self, n: int) -> None:
        self.controls.reload_tags()
        self.data_panel.refresh()
        self.set_status(
            f"{n:,} sectors loaded — tick the ones you want, then fetch (Ctrl+D)."
        )

    def search_markets(self, term: str) -> None:
        worker = MarketSearchWorker(self.settings, term, self)
        worker.results.connect(self.controls.show_market_results)
        worker.failed.connect(self._on_failed)
        worker.start()
        self._search_worker = worker  # keep a reference alive

    def _start(self, worker: Any) -> None:
        self._worker = worker
        self._busy(True)
        worker.finished.connect(lambda: self._busy(False))
        worker.start()

    def cancel_job(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.set_status("Cancelling…")

    # -- completion ---------------------------------------------------------
    def _on_ingest_done(self, report: IngestReport) -> None:
        self.data_panel.append_log(report.summary())
        if report.errors:
            for err in report.errors[:20]:
                self.data_panel.append_log(f"  error: {err}")
        self.data_panel.refresh()
        self.controls.reload_tags()
        self._refresh_db_label()
        self.set_status("Fetch complete — " + report.summary())
        if report.trades_added and not report.cancelled:
            QTimer.singleShot(
                200,
                lambda: self.start_analysis(
                    self.controls.filters(), self.controls.cluster_params()
                ),
            )

    def _on_analysis_done(self, result: AnalysisResult) -> None:
        self.result = result
        self.clusters_panel.set_result(result)
        self.compare_panel.set_result(result)
        if result.ok:
            self.tabs.setCurrentWidget(self.clusters_panel)
            s = result.stats
            self.set_status(
                f"{s.get('clusters', 0)} clusters · "
                f"{s.get('clustered_wallets', 0):,} wallets clustered of "
                f"{s.get('users', 0):,} eligible · {s.get('edges', 0):,} edges · "
                f"{s.get('elapsed', 0):.1f}s"
            )
        else:
            self.tabs.setCurrentWidget(self.data_panel)
            self.set_status("No clusters found — see the log for where it stopped.")

    def _on_failed(self, trace: str) -> None:
        self.data_panel.append_log("FAILED:\n" + trace)
        self.tabs.setCurrentWidget(self.data_panel)
        self.set_status("Job failed — see the log.")
        QMessageBox.critical(
            self, "Job failed", trace.strip().splitlines()[-1][:400]
        )

    def rescore(self, weights: dict[str, float]) -> None:
        """Re-rank clusters live as the weight spinners move."""
        if self.result is None or not self.result.ok:
            return
        self.result.rescore(weights)
        self.clusters_panel.refresh_ranking()
        self.compare_panel.set_result(self.result)

    # -- misc ---------------------------------------------------------------
    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _refresh_db_label(self) -> None:
        s = self.db.stats()
        self.db_label.setText(
            f"{s['markets']:,} markets · {s['trades']:,} trades · {s['users']:,} wallets"
        )

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "How this works",
            "<b>1 · Fetch</b> pulls markets for your window and sectors from the public "
            "Gamma API, then crawls every trade for each one. Trade history is "
            "time-sliced to get around the API's 10,000-row offset cap, and already "
            "covered windows are skipped on re-runs.<br><br>"
            "<b>2 · Analyse</b> turns trades into net positions per "
            "(wallet, market, outcome), keeps wallets that clear your size and "
            "selectivity gates, and scores every pair on IDF-weighted cosine "
            "similarity — so agreeing on an obscure market counts for far more than "
            "agreeing on a crowd favourite. Communities come from Louvain over that "
            "graph.<br><br>"
            "<b>Reading the results:</b> a high win rate alone means little — buying "
            "the 97c favourite wins almost always for almost nothing. Look for high "
            "win rate <i>together with</i> ROI, longshot win rate, tight entry spread, "
            "early entry and high bet rarity. That combination is what the suspicion "
            "score is built from, and the weights on the left re-rank it live.<br><br>"
            "<i>Clusters are statistical associations, not proof of wrongdoing.</i>",
        )

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        try:
            self.controls.apply_to_settings().save()
        except OSError:
            pass
        self.db.close()
        super().closeEvent(event)
