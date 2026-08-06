"""Charts for the watchlist detail panel.

Each one answers a question a table cannot answer at a glance. They are reading
instruments, not workspaces: the mouse cannot pan or zoom them, every axis is
capped to the data with a little padding, and hovering any bar, point or cell
shows its value in a label that follows the data itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..theme import ACCENT, BAD, BG_ALT, BG_RAISED, BORDER, FG, FG_DIM, GOOD, WARN
from .charts import TimeAxis

# resolver(x, y) -> (anchor_x, anchor_y, text) in data coordinates, or None.
HoverResolver = Callable[[float, float], "tuple[float, float, str] | None"]


def _when(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


class HoverProbe:
    """A value label that follows the hovered data point.

    One instance per plot. The chart supplies a resolver mapping the cursor's
    data coordinates to the nearest interesting thing; the probe renders its
    text just above that thing, flipping below when the point sits near the top
    of the view so the label never leaves the frame.
    """

    def __init__(self, plot: pg.PlotWidget):
        self.plot = plot
        self.resolver: HoverResolver | None = None
        self.label = pg.TextItem(
            color=FG,
            fill=pg.mkBrush(QColor(26, 29, 37, 238)),
            border=pg.mkPen(QColor(ACCENT), width=1),
            anchor=(0.5, 1.25),
        )
        font = QFont()
        font.setPointSizeF(8.2)
        self.label.setFont(font)
        self.label.setZValue(100)
        self.label.hide()
        plot.addItem(self.label, ignoreBounds=True)

        self.marker = pg.ScatterPlotItem(
            size=8, brush=pg.mkBrush(QColor(FG)), pen=pg.mkPen(QColor(ACCENT), width=1.6)
        )
        self.marker.setZValue(99)
        self.marker.hide()
        plot.addItem(self.marker, ignoreBounds=True)

        self._proxy = pg.SignalProxy(
            plot.scene().sigMouseMoved, rateLimit=45, slot=self._moved
        )

    def _moved(self, event: Any) -> None:
        pos = event[0]
        vb = self.plot.getViewBox()
        if self.resolver is None or not self.plot.sceneBoundingRect().contains(pos):
            self.label.hide()
            self.marker.hide()
            return
        point = vb.mapSceneToView(pos)
        hit = self.resolver(point.x(), point.y())
        if hit is None:
            self.label.hide()
            self.marker.hide()
            return
        x, y, text = hit
        (_, _), (y_lo, y_hi) = vb.viewRange()[0], vb.viewRange()[1]
        # Flip under the point when it is close to the ceiling of the view.
        near_top = (y - y_lo) / max(y_hi - y_lo, 1e-12) > 0.78
        self.label.setAnchor((0.5, -0.25) if near_top else (0.5, 1.25))
        self.label.setText(text)
        self.label.setPos(x, y)
        self.label.show()
        self.marker.setData([x], [y])
        self.marker.show()


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
        # Reading instrument, not a workspace: no panning, no zooming, no
        # autorange button. Every range is set explicitly from the data.
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setMinimumHeight(height)
        self.plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        layout.addWidget(self.plot)

        self.empty = QLabel("No data yet.")
        self.empty.setObjectName("dim")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setVisible(False)
        layout.addWidget(self.empty)

        self.hover = HoverProbe(self.plot)

    def _begin(self, has_data: Any) -> bool:
        # Callers build this from pandas comparisons, which yield numpy.bool_ -
        # and PySide refuses that where it wants a plain bool.
        has_data = bool(has_data)
        self.plot.clear()
        # clear() removes the probe's items; put them back.
        self.plot.addItem(self.hover.label, ignoreBounds=True)
        self.plot.addItem(self.hover.marker, ignoreBounds=True)
        self.hover.label.hide()
        self.hover.marker.hide()
        self.hover.resolver = None
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
        self.plot.addItem(pg.InfiniteLine(
            pos=0.5, angle=90, pen=pg.mkPen(QColor(FG_DIM), width=1, style=Qt.DashLine)))

        top = float(hist.usd.max())
        self.plot.setXRange(0, 1, padding=0.02)
        self.plot.setYRange(0, top * 1.12, padding=0)   # money only goes up from zero

        rows = hist.reset_index(drop=True)

        def resolve(x: float, _y: float):
            i = int(np.clip(np.floor(x * len(rows)), 0, len(rows) - 1))
            r = rows.iloc[i]
            if r.usd <= 0:
                return None
            outcome = (
                "still open" if not np.isfinite(r.win_share)
                else f"{r.win_share:.0%} of it won"
            )
            return (
                float(r.centre), float(r.usd),
                f"{r.left:.2f}–{r.right:.2f}\n${r.usd:,.0f} across {int(r.n)} bet(s)\n{outcome}",
            )

        self.hover.resolver = resolve

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
        curve = curve.reset_index(drop=True)
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

        # The curve can dip negative; zero stays in frame either way, and the
        # padding is proportional so the line never touches the border.
        lo = min(0.0, float(np.nanmin(pnl)))
        hi = max(0.0, float(np.nanmax(pnl)))
        span = max(hi - lo, 1.0)
        self.plot.setYRange(lo - span * 0.08, hi + span * 0.10, padding=0)
        if len(ts) > 1:
            self.plot.setXRange(float(ts[0]), float(ts[-1]), padding=0.04)

        step = curve.pnl.to_numpy(dtype=float)
        if len(step):
            for idx, tint in ((int(np.nanargmax(step)), GOOD), (int(np.nanargmin(step)), BAD)):
                self.plot.addItem(pg.ScatterPlotItem(
                    [ts[idx]], [pnl[idx]], size=9, brush=pg.mkBrush(QColor(tint)),
                    pen=pg.mkPen(QColor(BG_ALT), width=1.5),
                ))
            biggest = float(np.nanmax(np.abs(step)))
            share = biggest / max(abs(final), 1e-9) if final else float("inf")
            self.set_subtitle(
                f"Cumulative realised P&L over {len(curve)} settled bets · "
                f"largest single bet is {min(share, 9.99):.0%} of the total"
                + (" — the result rests on it" if share > 0.6 else "")
            )

        def resolve(x: float, _y: float):
            i = int(np.clip(np.searchsorted(ts, x), 0, len(ts) - 1))
            if i > 0 and abs(ts[i - 1] - x) < abs(ts[i] - x):
                i -= 1
            r = curve.iloc[i]
            verdict = "won" if r.status == "won" else "lost"
            return (
                float(ts[i]), float(pnl[i]),
                f"{_when(ts[i])}\ncumulative ${pnl[i]:+,.0f}\n"
                f"{verdict} {r.pnl:+,.0f} on “{str(r.question)[:34]}”",
            )

        self.hover.resolver = resolve


class _HBarChart(ChartCard):
    """Shared machinery for the horizontal bar charts."""

    def _show_bars(self, labels: list[str], values: np.ndarray,
                   brushes: list, texts: list[str]) -> None:
        y = np.arange(len(values))
        self.plot.addItem(pg.BarGraphItem(
            x0=0, y=y, height=0.62, width=values, brushes=brushes, pen=pg.mkPen(None)))
        axis = self.plot.getAxis("left")
        axis.setTicks([[(i, label[:18]) for i, label in enumerate(labels)]])
        axis.setWidth(130)
        x_hi = max(float(values.max()) * 1.15, 0.1)
        self.plot.setXRange(0, x_hi, padding=0)          # bars start at zero
        self.plot.setYRange(-0.6, len(values) - 0.4, padding=0)

        def resolve(x: float, y_pos: float):
            i = int(round(y_pos))
            if not (0 <= i < len(values)) or abs(y_pos - i) > 0.45:
                return None
            if x < -x_hi * 0.02 or x > x_hi:
                return None
            return float(values[i]), float(i), texts[i]

        self.hover.resolver = resolve


class CoTraderChart(_HBarChart):
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
        df = df.head(8).iloc[::-1].reset_index(drop=True)
        share = df.share.to_numpy(dtype=float)
        self._show_bars(
            [str(d) for d in df.display],
            share,
            [pg.mkBrush(QColor(WARN) if s >= 0.5 else QColor(ACCENT)) for s in share],
            [
                f"{r.display}\nholds {int(r.shared)} of this wallet's bets ({r.share:.0%})\n"
                f"${r.usd:,.0f} on the shared side"
                for r in df.itertuples()
            ],
        )
        top = float(share.max())
        self.set_subtitle(
            f"Top overlap is {top:.0%} of this wallet's book"
            + (" — that is not coincidence" if top >= 0.5 else "")
        )


class StakeShareChart(_HBarChart):
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
        df = df.head(10).iloc[::-1].reset_index(drop=True)
        share = df.share.to_numpy(dtype=float)
        self._show_bars(
            [str(d) for d in df.display],
            share,
            [pg.mkBrush(QColor(WARN) if s >= 0.5 else QColor(ACCENT)) for s in share],
            [
                f"{r.display}\n${r.staked:,.0f} staked ({r.share:.0%} of the group)\n"
                f"{int(r.bets)} bet(s)"
                for r in df.itertuples()
            ],
        )
        top = float(share.max())
        self.set_subtitle(
            f"Largest member carries {top:.0%} of the stake"
            + (" — one whale and followers, not a peer group" if top >= 0.6 else "")
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
        series = series.reset_index(drop=True)
        ts = series.ts.to_numpy(dtype=float)
        vwap = series.vwap.to_numpy(dtype=float)
        self.plot.addItem(pg.PlotDataItem(ts, vwap, pen=pg.mkPen(QColor(ACCENT), width=2)))
        self.plot.setYRange(0, 1.0, padding=0.03)        # prices live in [0, 1]
        if len(ts) > 1:
            self.plot.setXRange(float(ts[0]), float(ts[-1]), padding=0.04)

        entry_points: list[tuple[float, float, str]] = []
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
                })
                entry_points.append((
                    float(row.first_ts), float(row.entry),
                    f"{row.display}{' ★' if watched else ''}\n"
                    f"${row.usd:,.0f} @ {row.entry:.3f}\n{_when(row.first_ts)}",
                ))
            self.plot.addItem(pg.ScatterPlotItem(spots=spots))
            self.set_subtitle(
                f"Volume-weighted price from actual fills · {len(entries)} buyers plotted, "
                "gold = on your watchlist, dot size = stake"
            )

        x_span = max(float(ts[-1] - ts[0]), 1.0) if len(ts) > 1 else 1.0

        def resolve(x: float, y: float):
            # A nearby buyer dot beats the curve; the curve is the fallback.
            best = None
            for ex, ey, text in entry_points:
                dx = abs(ex - x) / x_span
                dy = abs(ey - y)
                score = dx * 1.8 + dy
                if dx < 0.03 and dy < 0.09 and (best is None or score < best[0]):
                    best = (score, ex, ey, text)
            if best is not None:
                return best[1], best[2], best[3]
            i = int(np.clip(np.searchsorted(ts, x), 0, len(ts) - 1))
            if i > 0 and abs(ts[i - 1] - x) < abs(ts[i] - x):
                i -= 1
            return (
                float(ts[i]), float(vwap[i]),
                f"{_when(ts[i])}\nprice {vwap[i]:.3f}\n${series.usd.iloc[i]:,.0f} traded",
            )

        self.hover.resolver = resolve


class OverlapMatrixChart(ChartCard):
    """Member-by-member shared-bet counts as a heatmap."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            "How much the members actually overlap",
            "Bets held in common. The diagonal is each member's own total.",
            height=220, parent=parent,
        )
        self.plot.getViewBox().setAspectLocked(True)

    def show_matrix(self, labels: list[str], matrix: np.ndarray,
                    names: dict[str, str] | None = None) -> None:
        if not self._begin(len(labels) >= 2 and matrix.size > 0):
            return
        names = names or {}
        shown = [str(names.get(w, w))[:14] for w in labels]
        off = matrix.copy()
        np.fill_diagonal(off, 0)
        top = float(off.max()) if off.size else 0.0

        image = pg.ImageItem(matrix.T)
        image.setLookupTable(np.array([
            [int(26 + 53 * t), int(30 + 126 * t), int(38 + 211 * t)] for t in np.linspace(0, 1, 256)
        ], dtype=np.ubyte))
        image.setLevels((0, max(top, 1)))
        self.plot.addItem(image)

        ticks = [(i + 0.5, name) for i, name in enumerate(shown)]
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

        n = len(labels)
        self.plot.setXRange(0, n, padding=0.02)
        self.plot.setYRange(0, n, padding=0.02)

        def resolve(x: float, y: float):
            i, j = int(np.floor(x)), int(np.floor(y))
            if not (0 <= i < n and 0 <= j < n):
                return None
            value = int(matrix[i, j])
            if i == j:
                text = f"{shown[i]}\n{value} bet(s) of their own"
            else:
                text = f"{shown[i]} ∩ {shown[j]}\n{value} bet(s) in common"
            return i + 0.5, j + 0.5, text

        self.hover.resolver = resolve
        self.set_subtitle(
            f"Bets held in common · highest pair shares {int(top)}; "
            "the diagonal is each member's own total"
        )
