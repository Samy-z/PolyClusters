"""Clusters tab: ranked cluster table on top, drill-down for the selection below."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from ...analysis.engine import AnalysisResult
from ..columns import BET_COLUMNS, CLUSTER_COLUMNS, MEMBER_COLUMNS, POSITION_COLUMNS
from ..widgets.charts import ClusterNetworkView, EntryTimelineView
from ..widgets.table_view import MetricTable
from .common import StatRow

CLUSTER_STATS = [
    ("suspicion", "Suspicion", "Composite ranking score for the selected cluster."),
    ("members", "Members", "Wallets in the cluster."),
    ("positions", "Bets made", "Total member-bet pairs (every bet by every member)."),
    ("shared", "Shared bets", "Bets held by 2+ members on the same side."),
    ("unanimous", "Unanimous", "Bets EVERY member participated in."),
    ("staked", "Staked", "Total USD bought."),
    ("winrate", "Winrate", "Resolved positions won / resolved positions."),
    ("shared_wr", "Shared WR", "Win rate on bets 2+ members shared."),
    ("unan_wr", "Unanim WR", "Win rate on unanimous bets."),
    ("roi", "ROI", "P&L / staked."),
    ("pnl", "P&L", "Realised + settled profit."),
    ("sync", "Sync rate", "Shared bets entered inside the sync window."),
    ("early", "Earliness", "Median entry position within market lifetime (lower = earlier)."),
    ("rarity", "Rarity", "Mean IDF of shared bets (higher = more obscure)."),
]


class ClustersPanel(QWidget):
    """Top-level view over a completed analysis run."""

    status = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._result: AnalysisResult | None = None
        self._cluster_id: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Vertical)
        layout.addWidget(splitter, 1)

        # --- top: ranked clusters ---
        top = QWidget()
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(6)
        self.clusters_table = MetricTable(CLUSTER_COLUMNS, title="Clusters")
        self.clusters_table.row_selected.connect(self._on_cluster_selected)
        top_lay.addWidget(self.clusters_table, 1)
        splitter.addWidget(top)

        # --- bottom: detail for the selected cluster ---
        bottom = QWidget()
        bot_lay = QVBoxLayout(bottom)
        bot_lay.setContentsMargins(0, 0, 0, 0)
        bot_lay.setSpacing(6)

        self.header = QLabel("Select a cluster above")
        self.header.setObjectName("h1")
        bot_lay.addWidget(self.header)

        self.stats = StatRow(CLUSTER_STATS)
        bot_lay.addWidget(self.stats)

        self.tabs = QTabWidget()
        self.members_table = MetricTable(MEMBER_COLUMNS, title="")
        self.members_table.row_selected.connect(self._on_member_selected)
        self.bets_table = MetricTable(BET_COLUMNS, title="")
        self.bets_table.row_selected.connect(self._on_bet_selected)
        self.positions_table = MetricTable(POSITION_COLUMNS, title="")
        self.timeline = EntryTimelineView()
        self.network = ClusterNetworkView()
        self.network.node_clicked.connect(self._focus_wallet)

        self.tabs.addTab(self.members_table, "Members")
        self.tabs.addTab(self.bets_table, "Cluster bets")
        self.tabs.addTab(self.positions_table, "Raw positions")
        self.tabs.addTab(self.timeline, "Entry timeline")
        self.tabs.addTab(self.network, "Network")
        bot_lay.addWidget(self.tabs, 1)
        splitter.addWidget(bottom)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([340, 460])

    # -- population ---------------------------------------------------------
    def set_result(self, result: AnalysisResult) -> None:
        self._result = result
        self.clusters_table.set_dataframe(result.clusters, CLUSTER_COLUMNS)
        self.clear_detail()
        if not result.clusters.empty:
            self.clusters_table.select_first()

    def refresh_ranking(self) -> None:
        """Re-render the cluster table after a weight change."""
        if self._result is None:
            return
        keep = self._cluster_id
        self.clusters_table.set_dataframe(self._result.clusters, CLUSTER_COLUMNS)
        if keep is not None:
            self._select_cluster_id(keep)
        elif not self._result.clusters.empty:
            self.clusters_table.select_first()

    def clear_detail(self) -> None:
        self._cluster_id = None
        self.header.setText("Select a cluster above")
        self.stats.clear()
        for table in (self.members_table, self.bets_table, self.positions_table):
            table.set_dataframe(pd.DataFrame())
        self.timeline.clear()
        self.network.clear()

    def _select_cluster_id(self, cluster_id: int) -> None:
        df = self.clusters_table.model.dataframe
        if df.empty or "cluster_id" not in df.columns:
            return
        matches = np.flatnonzero(df.cluster_id.to_numpy() == cluster_id)
        if len(matches) == 0:
            return
        proxy = self.clusters_table.proxy
        src_index = self.clusters_table.model.index(int(matches[0]), 0)
        self.clusters_table.table.selectRow(proxy.mapFromSource(src_index).row())

    # -- selection handlers -------------------------------------------------
    def _on_cluster_selected(self, row: pd.Series | None) -> None:
        if row is None or self._result is None:
            return
        cid = int(row.cluster_id)
        self._cluster_id = cid
        res = self._result

        members = res.cluster_members(cid)
        bets = res.cluster_bets(cid)
        positions = res.cluster_positions(cid)

        self.header.setText(
            f"Cluster {cid} — {int(row.get('n_members', 0))} wallets, "
            f"{int(row.get('n_bets', 0))} distinct bets"
        )
        self._fill_stats(row)

        self.members_table.set_dataframe(members, MEMBER_COLUMNS)
        self.bets_table.set_dataframe(bets, BET_COLUMNS)
        self.positions_table.set_dataframe(
            positions.sort_values("buy_usd", ascending=False), POSITION_COLUMNS
        )
        self.timeline.show_cluster(positions, bets)
        self.network.show_cluster(res.cluster_subgraph(cid), members)
        self.status.emit(
            f"Cluster {cid}: {len(members)} members, {len(bets)} bets, "
            f"{len(positions):,} positions"
        )

    def _fill_stats(self, row: pd.Series) -> None:
        get = lambda k: row.get(k, np.nan)  # noqa: E731
        self.stats.set("suspicion", get("suspicion_score"), "num")
        self.stats.set("members", get("n_members"), "int")
        self.stats.set("positions", get("n_positions"), "int")
        self.stats.set("shared", get("n_shared_bets"), "int")
        self.stats.set("unanimous", get("n_unanimous_bets"), "int")
        self.stats.set("staked", get("total_usd"), "usd")
        self.stats.set("winrate", get("winrate"), "pct")
        self.stats.set("shared_wr", get("shared_winrate"), "pct")
        self.stats.set("unan_wr", get("unanimous_winrate"), "pct")
        self.stats.set("roi", get("roi"), "pct", signed=True)
        self.stats.set("pnl", get("pnl"), "usd", signed=True)
        self.stats.set("sync", get("sync_rate"), "pct")
        self.stats.set("early", get("median_entry_pct_of_life"), "pct")
        self.stats.set("rarity", get("mean_rarity_idf"), "num")

    def _on_member_selected(self, row: pd.Series | None) -> None:
        """Narrow the raw-positions table to the chosen wallet."""
        if row is None or self._result is None or self._cluster_id is None:
            return
        positions = self._result.cluster_positions(self._cluster_id)
        wallet = str(row.proxy_wallet)
        subset = positions[positions.proxy_wallet == wallet]
        self.positions_table.set_dataframe(
            subset.sort_values("buy_usd", ascending=False), POSITION_COLUMNS
        )
        self.status.emit(
            f"{row.get('display', wallet)} — {len(subset)} positions, "
            f"${row.get('total_usd', 0):,.0f} staked, "
            f"first-in on {int(row.get('first_mover_count') or 0)} shared bets"
        )

    def _on_bet_selected(self, row: pd.Series | None) -> None:
        """Show every member's entry into the chosen bet, earliest first."""
        if row is None or self._result is None or self._cluster_id is None:
            return
        positions = self._result.cluster_positions(self._cluster_id)
        subset = positions[positions.bet_key == row.bet_key]
        self.positions_table.set_dataframe(
            subset.sort_values("first_buy_ts"), POSITION_COLUMNS
        )
        self.tabs.setCurrentWidget(self.positions_table)
        self.status.emit(
            f"{str(row.get('question', ''))[:70]} — {len(subset)} members in, "
            f"entry spread {row.get('entry_spread_h', float('nan')):.1f}h"
        )

    def _focus_wallet(self, wallet: str) -> None:
        if self.members_table.search is not None:
            self.members_table.search.setText(wallet[:10])
        self.tabs.setCurrentWidget(self.members_table)
