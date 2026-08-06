"""Watchlist tab: starred clusters, traders, bets and positions, plus what
has happened to them since you last looked.

The point of this tab is continuity. A cluster run is a snapshot of one window
under one set of filters; change either and the cluster numbering is gone. What
survives is the wallets, the bets, and whether they keep behaving the way that
made them interesting. Everything here is keyed to something stable and is
recomputed from the local database, so a watched trader stays watched across
runs, filter changes and restarts.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QTabWidget, QVBoxLayout, QWidget,
)

from ...analysis.engine import AnalysisResult
from ...core.db import Database
from ...core.watchlist import WatchlistStore, match_cluster, wallet_signals
from ..models import Col
from ..widgets.collapse import CollapsedControlsStrip
from ..widgets.table_view import MetricTable
from .common import StatRow
from .watch_detail import WatchDetailPanel

WATCH_STATS = [
    ("clusters", "Clusters", "Watched clusters."),
    ("members", "Traders", "Watched wallets."),
    ("bets", "Bets", "Watched markets and sides."),
    ("positions", "Positions", "Watched wallet-in-a-bet pairs."),
    ("events", "Events", "Recorded changes across all watched items."),
    ("unseen", "New", "Events since you last opened this tab."),
]

TRADER_COLUMNS = [
    Col("label", "Trader", "text", 160),
    Col("wallet", "Wallet", "wallet", 118),
    Col("staked", "Staked", "usd", 90, heat=True),
    Col("n_bets", "Bets", "int", 60),
    Col("n_markets", "Markets", "int", 70),
    Col("median_bet", "Median bet", "usd", 88),
    Col("winrate", "Winrate", "pct", 74, heat=True),
    Col("longshot_winrate", "Longshot WR", "pct", 92, heat=True,
        tip="Win rate on entries below 50c, where private information shows."),
    Col("edge_per_share", "Edge/share", "ratio", 88, heat=True,
        tip="Mean (payout − entry). How mispriced the market was where they got in."),
    Col("top_cotrader_share", "Shadow %", "pct", 82, heat=True,
        tip="Largest share of this wallet's bets that any single other wallet\n"
            "also holds. A wallet shadowing most of your book is not coincidence."),
    Col("top_cotrader", "Shadowed by", "text", 150,
        tip="The wallet with the highest overlap."),
    Col("median_market_volume", "Med mkt vol", "usd", 96, invert=True, heat=True,
        tip="Typical market size they trade. Low = they live in thin markets."),
    Col("median_hours_before_close", "Lead time", "hours", 84, heat=True),
    Col("median_entry", "Med entry", "ratio", 80),
    Col("new_events", "New", "int", 52, heat=True,
        tip="Events recorded for this trader that you have not seen."),
    Col("last_seen", "Last trade", "ts", 122),
    Col("note", "Note", "text", 200),
]

BET_COLUMNS_W = [
    Col("label", "Market", "text", 320),
    Col("outcome_index", "Side idx", "int", 70),
    Col("resolved", "Resolved", "bool", 78),
    Col("won", "Won", "bool", 60),
    Col("traders", "Traders", "int", 74, heat=True),
    Col("buy_usd", "Bought", "usd", 92, heat=True),
    Col("volume", "Mkt volume", "usd", 96),
    Col("watched_in", "Watched in it", "int", 100, heat=True,
        tip="How many of your watched traders hold this bet."),
    Col("new_events", "New", "int", 52, heat=True),
    Col("last_ts", "Last trade", "ts", 122),
    Col("note", "Note", "text", 180),
]

CLUSTER_COLUMNS_W = [
    Col("label", "Cluster", "text", 220),
    Col("n_wallets", "Members", "int", 76),
    Col("matched", "In current run", "text", 110,
        tip="Which cluster of the latest analysis this group maps onto."),
    Col("jaccard", "Overlap", "pct", 80, heat=True,
        tip="Membership overlap with that cluster. Low = the group has drifted."),
    Col("joined", "Joined", "int", 62,
        tip="Wallets in the current cluster that were not in the watched set."),
    Col("left", "Left", "int", 56, invert=True),
    Col("staked", "Staked", "usd", 92, heat=True),
    Col("winrate", "Winrate", "pct", 76, heat=True),
    Col("cohesion", "Cohesion", "pct", 82, heat=True,
        tip="Share of the group's bets held by at least half of its members."),
    Col("new_events", "New", "int", 52, heat=True),
    Col("note", "Note", "text", 200),
]

POSITION_COLUMNS_W = [
    Col("label", "Position", "text", 300),
    Col("wallet", "Wallet", "wallet", 118),
    Col("usd", "Staked", "usd", 90, heat=True),
    Col("entry", "Entry", "ratio", 74),
    Col("net", "Net shares", "num", 92),
    Col("resolved", "Resolved", "bool", 78),
    Col("won", "Won", "bool", 60),
    Col("new_events", "New", "int", 52, heat=True),
    Col("note", "Note", "text", 200),
]

EVENT_COLUMNS = [
    Col("ts", "When", "ts", 128),
    Col("severity", "Level", "text", 66),
    Col("item_kind", "Type", "text", 78),
    Col("label", "Item", "text", 190),
    Col("kind", "Event", "text", 100),
    Col("summary", "What happened", "text", 620),
]


class WatchlistPanel(QWidget):
    """Everything starred, and what changed while you were away."""

    status = Signal(str)
    refresh_requested = Signal(int)  # lookback days

    def __init__(self, db: Database, store: WatchlistStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.store = store
        self._result: AnalysisResult | None = None

        # Tables on the left, the profile of whatever is selected on the right.
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.body_splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(self.body_splitter, 1)

        main = QWidget()
        self.body_splitter.addWidget(main)

        self.detail = WatchDetailPanel(db, store)
        self.detail.collapse_button.clicked.connect(self.collapse_detail)
        self.detail.setMinimumWidth(420)
        self.body_splitter.addWidget(self.detail)
        self.body_splitter.setStretchFactor(0, 3)
        self.body_splitter.setStretchFactor(1, 2)

        self.detail_strip = CollapsedControlsStrip("DETAILS", direction="left")
        self.detail_strip.setToolTip("Show the detail panel")
        self.detail_strip.clicked.connect(self.expand_detail)
        self.detail_strip.hide()
        outer.addWidget(self.detail_strip)

        root = QVBoxLayout(main)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        bar = QHBoxLayout()
        title = QLabel("Watchlist")
        title.setObjectName("h1")
        bar.addWidget(title)
        bar.addStretch(1)
        bar.addWidget(QLabel("Look back"))
        self.lookback = QSpinBox()
        self.lookback.setRange(1, 365)
        self.lookback.setValue(30)
        self.lookback.setSuffix(" d")
        self.lookback.setToolTip(
            "How far back to pull activity for watched wallets when checking "
            "for updates."
        )
        bar.addWidget(self.lookback)
        self.btn_refresh = QPushButton("Check for updates")
        self.btn_refresh.setObjectName("primary")
        self.btn_refresh.setToolTip(
            "Follow every watched wallet across all of Polymarket, refresh the\n"
            "status of watched markets, and record what changed."
        )
        self.btn_refresh.clicked.connect(
            lambda: self.refresh_requested.emit(self.lookback.value())
        )
        bar.addWidget(self.btn_refresh)
        btn_reload = QPushButton("Recompute")
        btn_reload.setToolTip("Rebuild the tables from local data, without fetching.")
        btn_reload.clicked.connect(self.reload)
        bar.addWidget(btn_reload)
        btn_remove = QPushButton("Remove selected")
        btn_remove.setObjectName("danger")
        btn_remove.clicked.connect(self._remove_selected)
        bar.addWidget(btn_remove)
        root.addLayout(bar)

        self.stats = StatRow(WATCH_STATS)
        root.addWidget(self.stats)

        splitter = QSplitter(Qt.Vertical)
        root.addWidget(splitter, 1)

        self.tabs = QTabWidget()
        self.traders_table = MetricTable(TRADER_COLUMNS, title="")
        self.bets_table = MetricTable(BET_COLUMNS_W, title="")
        self.clusters_table = MetricTable(CLUSTER_COLUMNS_W, title="")
        self.positions_table = MetricTable(POSITION_COLUMNS_W, title="")
        self.tabs.addTab(self.traders_table, "Traders")
        self.tabs.addTab(self.bets_table, "Bets")
        self.tabs.addTab(self.clusters_table, "Clusters")
        self.tabs.addTab(self.positions_table, "Positions")
        splitter.addWidget(self.tabs)

        events_wrap = QWidget()
        ev_lay = QVBoxLayout(events_wrap)
        ev_lay.setContentsMargins(0, 0, 0, 0)
        ev_lay.setSpacing(4)
        self.events_table = MetricTable(EVENT_COLUMNS, title="Activity")
        ev_lay.addWidget(self.events_table, 1)
        splitter.addWidget(events_wrap)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([460, 300])

        # Selecting anywhere opens that item's profile on the right.
        for table, kind in (
            (self.traders_table, "member"), (self.bets_table, "bet"),
            (self.clusters_table, "cluster"), (self.positions_table, "position"),
        ):
            table.row_selected.connect(
                lambda row, k=kind: self.detail.show_item(k, row)
            )
        self.tabs.currentChanged.connect(self._sync_detail_to_tab)

    # -- detail panel -------------------------------------------------------
    def collapse_detail(self) -> None:
        self.detail.hide()
        self.detail_strip.show()

    def expand_detail(self) -> None:
        self.detail_strip.hide()
        self.detail.show()

    def _sync_detail_to_tab(self, *_a: object) -> None:
        """Follow the selection of whichever table just came to the front."""
        table = self.tabs.currentWidget()
        kinds = {
            id(self.traders_table): "member", id(self.bets_table): "bet",
            id(self.clusters_table): "cluster", id(self.positions_table): "position",
        }
        kind = kinds.get(id(table))
        if kind is None:
            return
        self.detail.show_item(kind, table.current_row())

    # -- population ---------------------------------------------------------
    def set_result(self, result: AnalysisResult) -> None:
        """A fresh run lets watched clusters be re-identified by overlap."""
        self._result = result
        self.reload()

    def reload(self) -> None:
        counts = self.store.count_by_kind()
        events = self.store.events()
        unseen = self.store.unseen_count()

        self.stats.set("clusters", counts.get("cluster", 0), "int")
        self.stats.set("members", counts.get("member", 0), "int")
        self.stats.set("bets", counts.get("bet", 0), "int")
        self.stats.set("positions", counts.get("position", 0), "int")
        self.stats.set("events", len(events), "int")
        self.stats.set("unseen", unseen, "int")

        per_item = (
            events[~events.seen.astype(bool)].groupby("item_id").size()
            if not events.empty else pd.Series(dtype=int)
        )

        self.traders_table.set_dataframe(self._traders_frame(per_item), TRADER_COLUMNS)
        self.bets_table.set_dataframe(self._bets_frame(per_item), BET_COLUMNS_W)
        self.clusters_table.set_dataframe(self._clusters_frame(per_item), CLUSTER_COLUMNS_W)
        self.positions_table.set_dataframe(self._positions_frame(per_item), POSITION_COLUMNS_W)
        self.events_table.set_dataframe(events, EVENT_COLUMNS)

        self._sync_detail_to_tab()

        total = sum(counts.values())
        self.status.emit(
            f"Watchlist: {total} item(s), {len(events)} event(s), {unseen} unseen"
            if total else "Watchlist is empty — click a ★ in any Clusters table to add rows."
        )

    def mark_seen(self) -> None:
        if self.store.unseen_count():
            self.store.mark_events_seen()
            self.reload()

    # -- frame builders -----------------------------------------------------
    def _traders_frame(self, per_item: pd.Series) -> pd.DataFrame:
        rows = []
        for item in self.store.items("member"):
            wallet = item.ref.get("wallet", "")
            sig = wallet_signals(self.db, wallet)
            top = (sig.get("top_cotraders") or [{}])[0]
            rows.append({
                "item_id": item.item_id,
                "label": item.label,
                "wallet": wallet,
                "note": item.note,
                "new_events": int(per_item.get(item.item_id, 0)),
                "top_cotrader": top.get("display", ""),
                **{k: v for k, v in sig.items() if k != "top_cotraders"},
            })
        return pd.DataFrame(rows)

    def _bets_frame(self, per_item: pd.Series) -> pd.DataFrame:
        from ...core.watchlist import observe_bet

        watched_wallets = {i.ref.get("wallet") for i in self.store.items("member")}
        rows = []
        for item in self.store.items("bet"):
            key = item.ref.get("bet_key", "")
            obs = observe_bet(self.db, key)
            cid, _, idx = key.rpartition(":")
            watched_in = 0
            if watched_wallets:
                df = self.db.query(
                    "SELECT count(DISTINCT proxy_wallet) AS n FROM trades "
                    "WHERE condition_id = ? AND outcome_index = ? AND side = 'BUY' "
                    f"AND proxy_wallet IN ({','.join('?' * len(watched_wallets))})",
                    [cid, int(idx or 0), *sorted(w for w in watched_wallets if w)],
                )
                watched_in = int(df.n.iloc[0]) if not df.empty else 0
            rows.append({
                "item_id": item.item_id,
                "label": obs.get("question") or item.label,
                "outcome_index": int(idx or 0),
                "note": item.note,
                "new_events": int(per_item.get(item.item_id, 0)),
                "watched_in": watched_in,
                **{k: v for k, v in obs.items() if k != "question"},
            })
        return pd.DataFrame(rows)

    def _clusters_frame(self, per_item: pd.Series) -> pd.DataFrame:
        rows = []
        for item in self.store.items("cluster"):
            wallets = [w for w in (item.ref.get("wallets") or []) if w]
            row: dict[str, Any] = {
                "item_id": item.item_id,
                "label": item.label,
                "n_wallets": len(wallets),
                "note": item.note,
                "new_events": int(per_item.get(item.item_id, 0)),
                "matched": "—",
                "jaccard": np.nan,
                "joined": np.nan,
                "left": np.nan,
            }
            cid, drift = match_cluster(wallets, self._result)
            if cid is not None:
                row.update({
                    "matched": f"Cluster {cid}",
                    "jaccard": drift.get("jaccard"),
                    "joined": len(drift.get("joined", [])),
                    "left": len(drift.get("left", [])),
                })
            row.update(self._group_stats(wallets))
            rows.append(row)
        return pd.DataFrame(rows)

    def _group_stats(self, wallets: list[str]) -> dict[str, Any]:
        """Stake, win rate and cohesion for an arbitrary set of wallets."""
        if not wallets:
            return {"staked": np.nan, "winrate": np.nan, "cohesion": np.nan}
        marks = ",".join("?" * len(wallets))
        df = self.db.query(
            f"""
            SELECT t.proxy_wallet, t.condition_id, t.outcome_index,
                   sum(CASE WHEN t.side='BUY' THEN t.usd ELSE 0 END) AS usd,
                   any_value(m.resolved) AS resolved,
                   any_value(m.winning_outcome) AS winner
            FROM trades t LEFT JOIN markets m USING (condition_id)
            WHERE t.proxy_wallet IN ({marks})
            GROUP BY t.proxy_wallet, t.condition_id, t.outcome_index
            """,
            list(wallets),
        )
        if df.empty:
            return {"staked": np.nan, "winrate": np.nan, "cohesion": np.nan}
        df["bet_key"] = df.condition_id + ":" + df.outcome_index.astype(str)
        res = df[df.resolved.fillna(False).astype(bool)]
        holders = df.groupby("bet_key").proxy_wallet.nunique()
        return {
            "staked": float(df.usd.sum()),
            "winrate": (
                float((res.outcome_index == res.winner).mean()) if len(res) else np.nan
            ),
            "cohesion": float((holders >= max(len(wallets) / 2, 2)).mean()) if len(holders) else np.nan,
        }

    def _positions_frame(self, per_item: pd.Series) -> pd.DataFrame:
        from ...core.watchlist import observe_wallet

        rows = []
        cache: dict[str, dict] = {}
        for item in self.store.items("position"):
            wallet = item.ref.get("wallet", "")
            key = item.ref.get("bet_key", "")
            if wallet not in cache:
                cache[wallet] = observe_wallet(self.db, wallet)
            bet = (cache[wallet].get("bets") or {}).get(key, {})
            rows.append({
                "item_id": item.item_id,
                "label": bet.get("question") or item.label,
                "wallet": wallet,
                "note": item.note,
                "new_events": int(per_item.get(item.item_id, 0)),
                "usd": bet.get("usd"),
                "entry": bet.get("entry"),
                "net": bet.get("net"),
                "resolved": bet.get("resolved"),
                "won": bet.get("won"),
            })
        return pd.DataFrame(rows)

    # -- actions ------------------------------------------------------------
    def _remove_selected(self) -> None:
        table = self.tabs.currentWidget()
        if not isinstance(table, MetricTable):
            return
        df = table.selected_rows()
        if df.empty or "item_id" not in df.columns:
            QMessageBox.information(self, "Remove", "Select one or more rows first.")
            return
        if QMessageBox.question(
            self, "Remove from watchlist",
            f"Remove {len(df)} item(s) and their recorded events?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        for item_id in df.item_id:
            self.store.remove(str(item_id))
        self.reload()
