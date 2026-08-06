"""Data tab: local database overview, ingested markets, and the run log."""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

from ...core.db import Database
from ..columns import MARKET_COLUMNS
from ..widgets.table_view import MetricTable
from .common import StatRow

DB_STATS = [
    ("markets", "Markets", "Markets stored locally."),
    ("resolved_markets", "Resolved", "Markets with a known winning outcome."),
    ("trades", "Trades", "Individual trade rows crawled."),
    ("users", "Wallets", "Distinct wallets seen in those trades."),
    ("usd_volume", "Volume", "Total USD notional across stored trades."),
    ("tags", "Sectors", "Tags in the local catalogue."),
]


class DataPanel(QWidget):
    """Shows what has been ingested and streams progress from the workers."""

    reingest_requested = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Local database")
        title.setObjectName("h1")
        header.addWidget(title)
        header.addStretch(1)
        self.path_label = QLabel(str(db.path))
        self.path_label.setObjectName("dim")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self.path_label)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        header.addWidget(QLabel("Rows:"))
        self.row_limit = QComboBox()
        # 0 means no LIMIT clause at all.
        for label, value in (("1,000", 1_000), ("5,000", 5_000), ("25,000", 25_000),
                             ("100,000", 100_000), ("All", 0)):
            self.row_limit.addItem(label, value)
        self.row_limit.setCurrentIndex(1)
        self.row_limit.setToolTip(
            "How many rows to load into the tables below.\n"
            "'All' can be slow once the database holds millions of trades."
        )
        self.row_limit.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.row_limit)
        header.addWidget(refresh)
        purge = QPushButton("Clear trades")
        purge.setObjectName("danger")
        purge.clicked.connect(self._clear_trades)
        header.addWidget(purge)
        root.addLayout(header)

        self.stats = StatRow(DB_STATS)
        root.addWidget(self.stats)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        splitter = QSplitter(Qt.Vertical)
        root.addWidget(splitter, 1)

        tabs = QTabWidget()
        self.markets_table = MetricTable(MARKET_COLUMNS, title="Ingested markets")
        tabs.addTab(self.markets_table, "Markets")
        self.coverage_table = MetricTable([], title="Ingest coverage")
        tabs.addTab(self.coverage_table, "Coverage")
        splitter.addWidget(tabs)

        log_wrap = QWidget()
        log_lay = QVBoxLayout(log_wrap)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(4)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Run log"))
        bar.addStretch(1)
        clear = QPushButton("Clear log")
        clear.clicked.connect(lambda: self.log.clear())
        bar.addWidget(clear)
        log_lay.addLayout(bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        log_lay.addWidget(self.log, 1)
        splitter.addWidget(log_wrap)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.refresh()

    # -- log ----------------------------------------------------------------
    def append_log(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.log.moveCursor(QTextCursor.End)

    def set_progress(self, done: int, total: int, label: str) -> None:
        self.progress.setVisible(True)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"%v / %m  ·  {label[:70]}")

    def end_progress(self) -> None:
        self.progress.setVisible(False)
        self.progress.reset()

    # -- data ---------------------------------------------------------------
    def refresh(self) -> None:
        stats = self.db.stats()
        self.stats.set("markets", stats["markets"], "int")
        self.stats.set("resolved_markets", stats["resolved_markets"], "int")
        self.stats.set("trades", stats["trades"], "int")
        self.stats.set("users", stats["users"], "int")
        self.stats.set("usd_volume", stats["usd_volume"], "usd")
        self.stats.set("tags", stats["tags"], "int")

        limit = int(self.row_limit.currentData() or 0)
        clause = f"LIMIT {limit}" if limit else ""

        markets = self.db.query(
            f"""
            SELECT m.*,
                   (SELECT string_agg(DISTINCT mt.tag_label, ', ')
                      FROM market_tags mt WHERE mt.condition_id = m.condition_id) AS sectors
            FROM markets m ORDER BY m.volume DESC {clause}
            """
        )
        self.markets_table.set_dataframe(markets, MARKET_COLUMNS)

        coverage = self.db.query(
            f"""
            SELECT m.question, l.condition_id,
                   count(*) AS windows,
                   sum(l.n_trades) AS trades_fetched,
                   min(l.window_start) AS covered_from,
                   max(l.window_end) AS covered_to,
                   max(l.fetched_at) AS last_fetch
            FROM ingest_log l LEFT JOIN markets m USING (condition_id)
            GROUP BY m.question, l.condition_id
            ORDER BY trades_fetched DESC {clause}
            """
        )
        from ..models import Col

        self.coverage_table.set_dataframe(
            coverage,
            [
                Col("question", "Market", "text", 340),
                Col("trades_fetched", "Trades", "int", 84),
                Col("windows", "Windows", "int", 78),
                Col("covered_from", "Covered from", "ts", 128),
                Col("covered_to", "Covered to", "ts", 128),
                Col("last_fetch", "Last fetch", "ts", 128),
                Col("condition_id", "Condition id", "text", 150),
            ],
        )

    def _clear_trades(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear trades",
            "Delete every stored trade and the ingest coverage log?\n\n"
            "Markets and sectors are kept, so re-ingesting will re-crawl trades "
            "from the API. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.db.clear_trades()
        self.append_log("Cleared all stored trades and ingest coverage.")
        self.refresh()
