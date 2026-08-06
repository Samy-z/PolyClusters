"""The right-hand profile panel of the Watchlist tab.

Clicking a watched item opens everything the local store knows about it. What
gets built depends on the kind: a trader gets a price-scale histogram, a P&L
curve and its shadowers; a bet gets the traded price with every entry marked on
it; a cluster gets its funding split and an overlap matrix.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ...core import profile
from ...core.db import Database
from ...core.watchlist import WatchlistStore, wallet_signals
from ..models import Col, fmt_value
from ..theme import ACCENT, BAD, FG, FG_DIM, GOOD, WARN
from ..widgets.detail_charts import (
    BetPriceChart, CoTraderChart, EntryPriceChart, OverlapMatrixChart,
    PnlCurveChart, StakeShareChart,
)
from ..widgets.table_view import MetricTable

POSITION_COLS = [
    Col("status", "State", "text", 58),
    Col("question", "Market", "text", 250),
    Col("outcome", "Side", "text", 54),
    Col("buy_usd", "Staked", "usd", 82, heat=True),
    Col("entry", "Entry", "ratio", 66),
    Col("pnl", "P&L", "usd", 82, heat=True),
    Col("roi", "ROI", "pct", 64, heat=True),
    Col("first_buy_ts", "Entered", "ts", 116),
    Col("market_volume", "Mkt vol", "usd", 82),
]

TAPE_COLS = [
    Col("ts", "When", "ts", 118),
    Col("side", "Side", "text", 52),
    Col("question", "Market", "text", 240),
    Col("outcome", "Outcome", "text", 66),
    Col("price", "Price", "ratio", 62),
    Col("usd", "USD", "usd", 78, heat=True),
]

ENTRANT_COLS = [
    Col("rank", "#", "int", 38),
    Col("display", "Trader", "text", 150),
    Col("usd", "Staked", "usd", 88, heat=True),
    Col("entry", "Entry", "ratio", 66),
    Col("first_ts", "First in", "ts", 116),
]

MEMBER_COLS = [
    Col("display", "Member", "text", 150),
    Col("staked", "Staked", "usd", 88, heat=True),
    Col("share", "Share", "pct", 66, heat=True),
    Col("bets", "Bets", "int", 56),
    Col("last_ts", "Last seen", "ts", 116),
]

EVENT_COLS = [
    Col("ts", "When", "ts", 118),
    Col("severity", "Level", "text", 60),
    Col("summary", "What happened", "text", 460),
]


def _clear_layout(layout: Any) -> None:
    """Empty a layout so nothing from the previous item survives the swap.

    deleteLater() alone is not enough: it defers destruction to the next event
    loop pass, and until then the old widget stays parented and keeps painting -
    which shows up as the previous item's badges and figures rendered on top of
    the new ones. Reparenting to None removes them from the display now.
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


class Badge(QLabel):
    """A small coloured pill for a headline fact."""

    def __init__(self, text: str, tone: str = FG_DIM, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("badge")
        self.setStyleSheet(
            f"color: {tone}; border: 1px solid {tone}; border-radius: 8px;"
            f"padding: 2px 8px; font-size: 10px; font-weight: 600; background: transparent;"
        )


class MetricGrid(QFrame):
    """A compact label/value grid."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(10, 8, 10, 8)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(5)

    def set_items(self, items: list[tuple[str, Any, str]], columns: int = 3) -> None:
        _clear_layout(self.grid)
        for i, (label, value, fmt) in enumerate(items):
            row, col = divmod(i, columns)
            caption = QLabel(label.upper())
            caption.setObjectName("metricLabel")
            text = fmt_value(value, fmt)
            display = QLabel(text)
            display.setStyleSheet("font-size: 14px; font-weight: 700;")
            if fmt in ("pct", "usd", "ratio", "num") and isinstance(
                value, (int, float, np.integer, np.floating)
            ) and np.isfinite(value):
                if label.lower() in ("p&l", "roi", "edge/share",
                                     "longshot roi", "favourite roi"):
                    display.setStyleSheet(
                        f"font-size: 14px; font-weight: 700; "
                        f"color: {GOOD if value > 0 else (BAD if value < 0 else FG)};"
                    )
            box = QVBoxLayout()
            box.setSpacing(0)
            box.addWidget(display)
            box.addWidget(caption)
            holder = QWidget()
            holder.setLayout(box)
            self.grid.addWidget(holder, row, col)


class WatchDetailPanel(QScrollArea):
    """Profile of one watched item, rebuilt on each selection."""

    def __init__(self, db: Database, store: WatchlistStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.store = store
        self._item_id: str | None = None

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.setWidget(body)
        self.root = QVBoxLayout(body)
        self.root.setContentsMargins(12, 10, 12, 12)
        self.root.setSpacing(10)

        header = QHBoxLayout()
        self.title = QLabel("Nothing selected")
        self.title.setObjectName("h1")
        self.title.setWordWrap(True)
        header.addWidget(self.title, 1)
        self.collapse_button = QPushButton("››")
        self.collapse_button.setObjectName("dockCollapse")
        self.collapse_button.setFixedSize(26, 20)
        self.collapse_button.setCursor(Qt.PointingHandCursor)
        self.collapse_button.setToolTip("Collapse this panel")
        header.addWidget(self.collapse_button, 0, Qt.AlignTop)
        self.root.addLayout(header)

        self.subtitle = QLabel("Select a row on the left to see everything known about it.")
        self.subtitle.setObjectName("dim")
        self.subtitle.setWordWrap(True)
        self.root.addWidget(self.subtitle)

        self.badges = QHBoxLayout()
        self.badges.setSpacing(6)
        self.badges.addStretch(1)
        self.root.addLayout(self.badges)

        self.metrics = MetricGrid()
        self.root.addWidget(self.metrics)

        # Every chart and table is built once and shown or hidden per kind;
        # rebuilding pyqtgraph widgets on every click is visibly slow.
        self.entry_chart = EntryPriceChart()
        self.pnl_chart = PnlCurveChart()
        self.cotrader_chart = CoTraderChart()
        self.price_chart = BetPriceChart()
        self.overlap_chart = OverlapMatrixChart()
        self.stake_chart = StakeShareChart()
        for chart in (self.entry_chart, self.pnl_chart, self.cotrader_chart,
                      self.price_chart, self.overlap_chart, self.stake_chart):
            self.root.addWidget(chart)

        self.positions_table = self._table("Positions", POSITION_COLS, 200)
        self.entrants_table = self._table("Everyone in this bet", ENTRANT_COLS, 190)
        self.members_table = self._table("Members", MEMBER_COLS, 170)
        self.tape_table = self._table("Recent trades", TAPE_COLS, 170)
        self.events_table = self._table("Recorded events", EVENT_COLS, 140)
        self.root.addStretch(1)
        self._show_only([])

    def _table(self, title: str, cols: list[Col], height: int) -> MetricTable:
        label = QLabel(title)
        label.setObjectName("metricLabel")
        table = MetricTable(cols, show_toolbar=False)
        table.setMinimumHeight(height)
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        table._section_label = label  # kept so visibility can follow the table
        self.root.addWidget(label)
        self.root.addWidget(table)
        return table

    def _show_only(self, widgets: list[QWidget]) -> None:
        for widget in (self.entry_chart, self.pnl_chart, self.cotrader_chart,
                       self.price_chart, self.overlap_chart, self.stake_chart,
                       self.positions_table, self.entrants_table,
                       self.members_table, self.tape_table, self.events_table):
            on = widget in widgets
            widget.setVisible(on)
            label = getattr(widget, "_section_label", None)
            if label is not None:
                label.setVisible(on)

    def _set_badges(self, badges: list[tuple[str, str]]) -> None:
        _clear_layout(self.badges)
        for text, tone in badges:
            self.badges.addWidget(Badge(text, tone))
        self.badges.addStretch(1)

    # -- entry point --------------------------------------------------------
    def show_item(self, kind: str, row: pd.Series | None) -> None:
        if row is None:
            self.clear()
            return
        self._item_id = str(row.get("item_id") or "") or None
        try:
            if kind == "member":
                self._show_trader(row)
            elif kind == "bet":
                self._show_bet(row)
            elif kind == "cluster":
                self._show_cluster(row)
            elif kind == "position":
                self._show_position(row)
        except Exception as exc:  # noqa: BLE001 - a bad row must not blank the tab
            self.title.setText("Could not build this profile")
            self.subtitle.setText(str(exc))
            self._show_only([])
            return
        self._fill_events()

    def clear(self) -> None:
        self._item_id = None
        self.title.setText("Nothing selected")
        self.subtitle.setText("Select a row on the left to see everything known about it.")
        self._set_badges([])
        self.metrics.set_items([])
        self._show_only([])

    def _fill_events(self) -> None:
        if not self._item_id:
            return
        events = self.store.events(self._item_id, limit=300)
        if events.empty:
            return
        self.events_table.setVisible(True)
        label = getattr(self.events_table, "_section_label", None)
        if label is not None:
            label.setVisible(True)
        self.events_table.set_dataframe(events, EVENT_COLS)

    # -- per-kind profiles ---------------------------------------------------
    def _show_trader(self, row: pd.Series) -> None:
        wallet = str(row.get("wallet") or "")
        name = str(row.get("label") or wallet[:12])
        self.title.setText(name)
        self.subtitle.setText(wallet)

        positions = profile.wallet_positions(self.db, wallet)
        signals = wallet_signals(self.db, wallet)
        cotraders = profile.wallet_cotraders(self.db, wallet)
        perf = profile.performance(self.db, [wallet])
        open_now = positions[positions.status == "open"] if not positions.empty else positions

        badges: list[tuple[str, str]] = []
        shadow = signals.get("top_cotrader_share")
        if shadow is not None and np.isfinite(shadow) and shadow >= 0.5:
            badges.append((f"shadowed {shadow:.0%}", WARN))
        ls = signals.get("longshot_winrate")
        if ls is not None and np.isfinite(ls) and ls >= 0.6:
            badges.append((f"longshot WR {ls:.0%}", GOOD))
        if not open_now.empty:
            badges.append((f"{len(open_now)} open", ACCENT))
        med_vol = signals.get("median_market_volume")
        if med_vol is not None and np.isfinite(med_vol) and med_vol < 250_000:
            badges.append(("thin markets", WARN))
        self._set_badges(badges)

        self.metrics.set_items([
            ("Staked", signals.get("staked"), "usd"),
            ("P&L", perf.get("realised_pnl"), "usd"),
            ("ROI", perf.get("roi"), "pct"),
            ("Longshot ROI", perf.get("longshot_roi"), "pct"),
            ("Favourite ROI", perf.get("favourite_roi"), "pct"),
            ("Edge/share", signals.get("edge_per_share"), "ratio"),
            ("Winrate", signals.get("winrate"), "pct"),
            ("Longshot WR", signals.get("longshot_winrate"), "pct"),
            ("Bets", signals.get("n_bets"), "int"),
            ("Markets", signals.get("n_markets"), "int"),
            ("Median entry", signals.get("median_entry"), "ratio"),
            ("Median mkt vol", signals.get("median_market_volume"), "usd"),
            ("Open now", float(open_now.buy_usd.sum()) if not open_now.empty else 0.0, "usd"),
            ("Lead time", signals.get("median_hours_before_close"), "hours"),
            ("Last trade", signals.get("last_seen"), "ts"),
        ])

        self.entry_chart.show_histogram(profile.entry_price_histogram(positions))
        self.pnl_chart.show_curve(profile.pnl_curve(positions))
        self.cotrader_chart.show_cotraders(cotraders)
        self.positions_table.set_dataframe(positions, POSITION_COLS)
        self.tape_table.set_dataframe(profile.recent_trades(self.db, wallet), TAPE_COLS)
        self._show_only([self.entry_chart, self.pnl_chart, self.cotrader_chart,
                         self.positions_table, self.tape_table])

    def _show_bet(self, row: pd.Series) -> None:
        item = self._lookup_item()
        bet_key = str((item.ref if item else {}).get("bet_key") or "")
        cid, _, idx = bet_key.rpartition(":")
        outcome_index = int(idx or 0)
        question = str(row.get("label") or bet_key)
        self.title.setText(question)
        self.subtitle.setText(f"Outcome index {outcome_index} · {cid[:18]}…")

        series = profile.bet_price_series(self.db, cid, outcome_index)
        entrants = profile.bet_entrants(self.db, cid, outcome_index)
        watched = {i.ref.get("wallet") for i in self.store.items("member")}

        resolved = bool(row.get("resolved"))
        won = row.get("won")
        badges = []
        if resolved:
            badges.append(("WON" if won else "LOST", GOOD if won else BAD))
        else:
            badges.append(("still open", ACCENT))
        n_watched = int(row.get("watched_in") or 0)
        if n_watched:
            badges.append((f"{n_watched} watched trader(s) in it", WARN))
        self._set_badges(badges)

        first_px = float(series.vwap.iloc[0]) if not series.empty else np.nan
        last_px = float(series.vwap.iloc[-1]) if not series.empty else np.nan
        self.metrics.set_items([
            ("Traders", row.get("traders"), "int"),
            ("Bought", row.get("buy_usd"), "usd"),
            ("Market volume", row.get("volume"), "usd"),
            ("First price", first_px, "ratio"),
            ("Last price", last_px, "ratio"),
            ("Move", (last_px - first_px) if np.isfinite(first_px) else np.nan, "ratio"),
            ("Watched in it", n_watched, "int"),
            ("Last trade", row.get("last_ts"), "ts"),
        ])

        self.price_chart.show_series(series, entrants, watched)
        self.entrants_table.set_dataframe(entrants, ENTRANT_COLS)
        self._show_only([self.price_chart, self.entrants_table])

    def _show_cluster(self, row: pd.Series) -> None:
        item = self._lookup_item()
        wallets = [w for w in ((item.ref if item else {}).get("wallets") or []) if w]
        self.title.setText(str(row.get("label") or "Cluster"))
        self.subtitle.setText(f"{len(wallets)} member wallets, tracked by membership")

        members = profile.cluster_members(self.db, wallets)
        labels, matrix = profile.cluster_overlap(self.db, wallets)
        names = dict(zip(members.proxy_wallet, members.display)) if not members.empty else {}
        perf = profile.performance(self.db, wallets)

        badges = []
        matched = str(row.get("matched") or "—")
        if matched != "—":
            jac = row.get("jaccard")
            tone = GOOD if (jac is not None and np.isfinite(jac) and jac >= 0.7) else WARN
            badges.append((f"{matched} · {jac:.0%} overlap" if np.isfinite(jac or np.nan)
                           else matched, tone))
        else:
            badges.append(("not found in the latest run", FG_DIM))
        cohesion = row.get("cohesion")
        if cohesion is not None and np.isfinite(cohesion) and cohesion >= 0.5:
            badges.append((f"cohesion {cohesion:.0%}", WARN))
        self._set_badges(badges)

        self.metrics.set_items([
            ("Members", len(wallets), "int"),
            ("Staked", row.get("staked"), "usd"),
            ("P&L", perf.get("realised_pnl"), "usd"),
            ("ROI", perf.get("roi"), "pct"),
            ("Longshot ROI", perf.get("longshot_roi"), "pct"),
            ("Favourite ROI", perf.get("favourite_roi"), "pct"),
            ("Winrate", row.get("winrate"), "pct"),
            ("Cohesion", cohesion, "pct"),
            ("Joined since", row.get("joined"), "int"),
            ("Left since", row.get("left"), "int"),
        ])

        self.stake_chart.show_members(members)
        self.overlap_chart.show_matrix(labels, matrix, names)
        self.members_table.set_dataframe(members, MEMBER_COLS)
        self._show_only([self.stake_chart, self.overlap_chart, self.members_table])

    def _show_position(self, row: pd.Series) -> None:
        item = self._lookup_item()
        ref = item.ref if item else {}
        wallet = str(ref.get("wallet") or row.get("wallet") or "")
        bet_key = str(ref.get("bet_key") or "")
        cid, _, idx = bet_key.rpartition(":")
        outcome_index = int(idx or 0)

        self.title.setText(str(row.get("label") or bet_key))
        self.subtitle.setText(f"{wallet} in outcome {outcome_index}")

        series = profile.bet_price_series(self.db, cid, outcome_index)
        entrants = profile.bet_entrants(self.db, cid, outcome_index)
        entry = row.get("entry")
        resolved, won = bool(row.get("resolved")), row.get("won")

        badges = []
        if resolved:
            badges.append(("WON" if won else "LOST", GOOD if won else BAD))
        else:
            badges.append(("still open", ACCENT))
        if entry is not None and np.isfinite(entry) and not series.empty:
            market_avg = float(np.average(series.vwap, weights=series.usd.clip(lower=1e-9)))
            delta = entry - market_avg
            badges.append((
                f"{'above' if delta > 0 else 'below'} market by {abs(delta):.3f}",
                BAD if delta > 0 else GOOD,
            ))
        self._set_badges(badges)

        self.metrics.set_items([
            ("Staked", row.get("usd"), "usd"),
            ("Entry", entry, "ratio"),
            ("Net shares", row.get("net"), "num"),
            ("Resolved", resolved, "bool"),
        ])
        self.price_chart.show_series(series, entrants, {wallet})
        self.entrants_table.set_dataframe(entrants, ENTRANT_COLS)
        self._show_only([self.price_chart, self.entrants_table])

    def _lookup_item(self) -> Any:
        if not self._item_id:
            return None
        for item in self.store.items():
            if item.item_id == self._item_id:
                return item
        return None
