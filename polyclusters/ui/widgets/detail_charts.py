"""Charts for the watchlist detail panel.

Each one answers a question a table cannot answer at a glance:

* where a wallet puts its money on the price scale (favourite-buyer or longshot
  hunter, and whether the longshots actually land)
* whether its edge accumulates or comes from one lucky bet
* who shadows it
* what the market did around a watched entry
* how tightly a cluster's members actually overlap
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..theme import ACCENT, BAD, BG_ALT, BORDER, FG, FG_DIM, GOOD, WARN
from .charts import TimeAxis


class ChartCard(QWidget):
    """A titled chart with a one-line explanation of what it shows."""

    def __init__(self, title: str, subtitle: str = "", height: int = 170,
                 axis_items: dict | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        head = QLabel(title)
        head.setObjectName("metricLabel")
        layout.addWidget(head)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("dim")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)

        self.plot = pg.PlotWidget(axisItems=axis_items or {})
        self.plot.setMenuEnabled(False)
        self.plot.setMinimumHeight(height)
        self.plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        layout.addWidget(self.plot)

        self.empty = QLabel("No data yet.")
        self.empty.setObjectName("dim")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setVisible(False)
        layout.addWidget(self.empty)

    def _begin(self, has_data: Any) -> bool:
        # Callers build this from pandas comparisons, which yield numpy.bool_ -
        # and PySide refuses that where it wants a plain bool.
        has_data = bool(has_data)
        self.plot.clear()
        self.plot.setVisible(has_data)
        self.empty.setVisible(not has_data)
        return has_data

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)


class EntryPriceChart(ChartCard):
    """Dollars staked per entry price, coloured by how much of it won."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            "Where the money goes on the price scale",
            "Stake by entry price. Green = the money in that band won.",
            height=160, parent=parent,
        )
        self.plot.setLabel("bottom", "Entry price")
        self.plot.setLabel("left", "Staked")

    def show_histogram(self, hist: pd.DataFrame) -> None:
        if not self._begin(hist is not None and not hist.empty and hist.usd.sum() > 0):
            return
        width = float(hist.right.iloc[0] - hist.left.iloc[0]) * 0.92
        brushes = []
        for usd, win in zip(hist.usd, hist.win_share):
            if usd <= 0:
                brushes.append(pg.mkBrush(QColor(60, 66, 80, 120)))
            elif not np.isfinite(win):
                brushes.append(pg.mkBrush(QColor(ACCENT)))       # still open
            else:
                # Blend red -> green by the share of that band's money that won.
                brushes.append(pg.mkBrush(QColor(
                    int(240 - 177 * win), int(93 + 92 * win), int(93 - 13 * win), 230)))
        self.plot.addItem(pg.BarGraphItem(
            x=hist.centre.to_numpy(), height=hist.usd.to_numpy(), width=width, brushes=brushes,
            pen=pg.mkPen(None),
        ))
        # 0.5 divides longshots from favourites, which is the line that matters.
        line = pg.InfiniteLine(pos=0.5, angle=90,
                               pen=pg.mkPen(QColor(FG_DIM), width=1, style=Qt.DashLine))
        self.plot.addItem(line)
        self.plot.setXRange(0, 1, padding=0.02)

        staked = float(hist.usd.sum())
        longshot = float(hist[hist.centre < 0.5].usd.sum())
        self.set_subtitle(
            f"Stake by entry price · {longshot / staked:.0%} of the money went in "
            f"below 50c. Green = won, red = lost, blue = still open."
        )


class PnlCurveChart(ChartCard):
    """Cumulative realised P&L, so one lucky bet cannot pass for an edge."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            "Realised P&L over time",
            "Cumulative, by entry date. A staircase is an edge; a single step is luck.",
            height=170, axis_items={"bottom": TimeAxis(orientation="bottom")}, parent=parent,
        )
        self.plot.setLabel("left", "Cumulative P&L")

    def show_curve(self, curve: pd.DataFrame) -> None:
        if not self._begin(curve is not None and not curve.empty):
            return
        ts = curve.ts.to_numpy(dtype=float)
        pnl = curve.cum_pnl.to_numpy(dtype=float)
        final = float(pnl[-1]) if len(pnl) else 0.0
        colour = QColor(GOOD if final >= 0 else BAD)

        self.plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(QColor(FG_DIM), width=1)))
        curve_item = pg.PlotDataItem(ts, pnl, pen=pg.mkPen(colour, width=2))
        fill = pg.FillBetweenItem(
            curve_item, pg.PlotDataItem(ts, np.zeros_like(pnl)),
            brush=pg.mkBrush(QColor(colour.red(), colour.green(), colour.blue(), 45)),
        )
        self.plot.addItem(fill)
        self.plot.addItem(curve_item)

        # Mark the single biggest win and loss - the concentration question.
        step = curve.pnl.to_numpy(dtype=float)
        if len(step):
            for idx, tint in ((int(np.nanargmax(step)), GOOD), (int(np.nanargmin(step)), BAD)):
                self.plot.addItem(pg.ScatterPlotItem(
                    [ts[idx]], [pnl[idx]], size=9, brush=pg.mkBrush(QColor(tint)),
                    pen=pg.mkPen(QColor(BG_ALT), width=1.5),
                    tip=lambda **k: "",
                ))
            biggest = float(np.nanmax(np.abs(step)))
            share = biggest / max(abs(final), 1e-9) if final else float("inf")
            self.set_subtitle(
                f"Cumulative realised P&L over {len(curve)} settled bets · "
                f"largest single bet is {min(share, 9.99):.0%} of the total"
                + (" — the result rests on it" if share > 0.6 else "")
            )


class CoTraderChart(ChartCard):
    """Who holds the same bets, as a share of this wallet's own book."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            "Who shadows this wallet",
            "Share of its bets that another wallet also holds.",
            height=170, parent=parent,
        )
        self.plot.setLabel("bottom", "Share of this wallet's bets")

    def show_cotraders(self, df: pd.DataFrame) -> None:
        if not self._begin(df is not None and not df.empty):
            return
        df = df.head(8).iloc[::-1]
        y = np.arange(len(df))
        share = df.share.to_numpy(dtype=float)
        brushes = [
            pg.mkBrush(QColor(WARN) if s >= 0.5 else QColor(ACCENT)) for s in share
        ]
        self.plot.addItem(pg.BarGraphItem(x0=0, y=y, height=0.62, width=share,
                                          brushes=brushes, pen=pg.mkPen(None)))
        axis = self.plot.getAxis("left")
        axis.setTicks([[(i, str(d)[:18]) for i, d in enumerate(df.display)]])
        axis.setWidth(130)
        self.plot.setXRange(0, max(float(share.max()) * 1.15, 0.1), padding=0)
        top = float(share.max())
        self.set_subtitle(
            f"Top overlap is {top:.0%} of this wallet's book"
            + (" — that is not coincidence" if top >= 0.5 else "")
        )


class BetPriceChart(ChartCard):
    """What the market paid over time, with watched entries marked on it."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            "Traded price, and where they got in",
            "Volume-weighted price from actual fills.",
            height=180, axis_items={"bottom": TimeAxis(orientation="bottom")}, parent=parent,
        )
        self.plot.setLabel("left", "Price")

    def show_series(self, series: pd.DataFrame, entries: pd.DataFrame | None = None,
                    highlight: set[str] | None = None) -> None:
        if not self._begin(series is not None and not series.empty):
            return
        ts = series.ts.to_numpy(dtype=float)
        vwap = series.vwap.to_numpy(dtype=float)
        self.plot.addItem(pg.PlotDataItem(ts, vwap, pen=pg.mkPen(QColor(ACCENT), width=2)))
        self.plot.setYRange(0, 1, padding=0.02)

        if entries is not None and not entries.empty:
            highlight = highlight or set()
            usd = entries.usd.to_numpy(dtype=float)
            sizes = 6 + 16 * np.sqrt(usd / max(usd.max(), 1e-9))
            spots = []
            for row, size in zip(entries.itertuples(), sizes):
                watched = row.proxy_wallet in highlight
                spots.append({
                    "pos": (float(row.first_ts), float(row.entry)),
                    "size": float(size) * (1.25 if watched else 1.0),
                    "brush": pg.mkBrush(QColor(WARN) if watched else QColor(120, 130, 150, 130)),
                    "pen": pg.mkPen(QColor(FG), width=1.4) if watched else pg.mkPen(None),
                    "data": f"{row.display}  ${row.usd:,.0f} @ {row.entry:.3f}",
                })
            self.plot.addItem(pg.ScatterPlotItem(
                spots=spots, hoverable=True, tip=lambda **k: k.get("data", "")))
            self.set_subtitle(
                f"Volume-weighted price from actual fills · {len(entries)} buyers plotted, "
                "gold = on your watchlist, dot size = stake"
            )


class OverlapMatrixChart(ChartCard):
    """Member-by-member shared-bet counts as a heatmap."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            "How much the members actually overlap",
            "Bets held in common. The diagonal is each member's own total.",
            height=220, parent=parent,
        )
        self.plot.setMenuEnabled(False)
        self.plot.getViewBox().setAspectLocked(True)

    def show_matrix(self, labels: list[str], matrix: np.ndarray,
                    names: dict[str, str] | None = None) -> None:
        if not self._begin(len(labels) >= 2 and matrix.size > 0):
            return
        names = names or {}
        off = matrix.copy()
        np.fill_diagonal(off, 0)
        top = float(off.max()) if off.size else 0.0

        image = pg.ImageItem(matrix.T)
        # Blue ramp: pale where nothing is shared, saturated where a lot is.
        image.setLookupTable(np.array([
            [int(26 + 53 * t), int(30 + 126 * t), int(38 + 211 * t)] for t in np.linspace(0, 1, 256)
        ], dtype=np.ubyte))
        image.setLevels((0, max(top, 1)))
        self.plot.addItem(image)

        ticks = [(i + 0.5, str(names.get(w, w))[:14]) for i, w in enumerate(labels)]
        self.plot.getAxis("left").setTicks([ticks])
        self.plot.getAxis("bottom").setTicks([ticks])
        self.plot.getAxis("left").setWidth(110)
        self.plot.getAxis("bottom").setHeight(60)

        font = QFont(); font.setPointSizeF(7.5)
        for i in range(len(labels)):
            for j in range(len(labels)):
                value = int(matrix[i, j])
                if not value:
                    continue
                text = pg.TextItem(str(value), color=FG if i != j else FG_DIM, anchor=(0.5, 0.5))
                text.setFont(font)
                text.setPos(i + 0.5, j + 0.5)
                self.plot.addItem(text)
        self.plot.getViewBox().autoRange(padding=0.02)
        self.set_subtitle(
            f"Bets held in common · highest pair shares {int(top)}; "
            "the diagonal is each member's own total"
        )


class StakeShareChart(ChartCard):
    """Who actually funds a cluster."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            "Who funds this group",
            "Share of the group's total stake.",
            height=160, parent=parent,
        )
        self.plot.setLabel("bottom", "Share of group stake")

    def show_members(self, df: pd.DataFrame) -> None:
        if not self._begin(df is not None and not df.empty):
            return
        df = df.head(10).iloc[::-1]
        y = np.arange(len(df))
        share = df.share.to_numpy(dtype=float)
        brushes = [pg.mkBrush(QColor(WARN) if s >= 0.5 else QColor(ACCENT)) for s in share]
        self.plot.addItem(pg.BarGraphItem(x0=0, y=y, height=0.6, width=share,
                                          brushes=brushes, pen=pg.mkPen(None)))
        axis = self.plot.getAxis("left")
        axis.setTicks([[(i, str(d)[:18]) for i, d in enumerate(df.display)]])
        axis.setWidth(130)
        self.plot.setXRange(0, max(float(share.max()) * 1.15, 0.1), padding=0)
        top = float(share.max())
        self.set_subtitle(
            f"Largest member carries {top:.0%} of the stake"
            + (" — one whale and followers, not a peer group" if top >= 0.6 else "")
        )
