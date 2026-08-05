"""pyqtgraph views: the cluster network and the shared-bet entry timeline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..theme import ACCENT, BAD, BG_ALT, FG, FG_DIM, GOOD, WARN

pg.setConfigOption("background", BG_ALT)
pg.setConfigOption("foreground", FG_DIM)
pg.setConfigOption("antialias", True)


class TimeAxis(pg.AxisItem):
    """Epoch-seconds axis rendered as calendar dates."""

    def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:  # noqa: N802
        out = []
        fmt = "%m-%d %H:%M" if spacing < 3 * 86_400 else "%Y-%m-%d"
        for v in values:
            try:
                out.append(datetime.fromtimestamp(float(v), timezone.utc).strftime(fmt))
            except (ValueError, OverflowError, OSError):
                out.append("")
        return out


class ClusterNetworkView(QWidget):
    """Force-directed view of a cluster: node size = stake, edge = similarity."""

    node_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.caption = QLabel("Select a cluster to see its co-betting network.")
        self.caption.setObjectName("dim")
        layout.addWidget(self.caption)

        self.plot = pg.PlotWidget()
        self.plot.hideAxis("bottom")
        self.plot.hideAxis("left")
        self.plot.setMenuEnabled(False)
        self.plot.setAspectLocked(True)
        layout.addWidget(self.plot, 1)

        self._graph_item = pg.GraphItem()
        self.plot.addItem(self._graph_item)
        self._labels: list[pg.TextItem] = []
        self._wallets: list[str] = []
        self._graph_item.scatter.sigClicked.connect(self._on_click)

    def clear(self) -> None:
        self._graph_item.setData(pos=np.zeros((0, 2)), adj=np.zeros((0, 2), dtype=int))
        for lbl in self._labels:
            self.plot.removeItem(lbl)
        self._labels.clear()
        self._wallets = []

    def show_cluster(self, graph: nx.Graph, members: pd.DataFrame) -> None:
        self.clear()
        if graph is None or graph.number_of_nodes() == 0:
            self.caption.setText("This cluster has no internal edges to draw.")
            return

        nodes = list(graph.nodes())
        self._wallets = nodes
        index = {n: i for i, n in enumerate(nodes)}
        seed_layout = nx.spring_layout(
            graph, weight="weight", seed=42, k=1.6 / max(np.sqrt(len(nodes)), 1)
        )
        pos = np.array([seed_layout[n] for n in nodes], dtype=float)

        edges = np.array(
            [[index[u], index[v]] for u, v in graph.edges()], dtype=int
        ) if graph.number_of_edges() else np.zeros((0, 2), dtype=int)

        # Per-edge styling must be a structured array; a list of QPen objects
        # crashes GraphItem rather than raising.
        sims = np.array(
            [d.get("sim", 0.5) for _u, _v, d in graph.edges(data=True)], dtype=float
        ) if graph.number_of_edges() else np.zeros(0)
        if len(sims):
            lo, hi = float(sims.min()), float(sims.max())
            norm = (sims - lo) / (hi - lo) if hi > lo else np.full(len(sims), 0.6)
            pens = np.zeros(
                len(sims),
                dtype=[("red", np.ubyte), ("green", np.ubyte), ("blue", np.ubyte),
                       ("alpha", np.ubyte), ("width", float)],
            )
            pens["red"], pens["green"], pens["blue"] = 79, 156, 249
            pens["alpha"] = (45 + 165 * norm).astype(np.ubyte)
            pens["width"] = 0.6 + 2.6 * norm
        else:
            pens = None

        info = members.set_index("proxy_wallet") if not members.empty else pd.DataFrame()
        usd = np.array([
            float(info.total_usd.get(n, 0.0)) if "total_usd" in info.columns else 0.0
            for n in nodes
        ])
        sizes = 10 + 26 * (np.sqrt(usd) / max(np.sqrt(usd).max(), 1e-9)) if usd.max() > 0 \
            else np.full(len(nodes), 14.0)

        brushes = []
        for n in nodes:
            roi = float(info.roi.get(n, np.nan)) if "roi" in info.columns else np.nan
            leader = bool(info.is_lead_mover.get(n, False)) if "is_lead_mover" in info.columns else False
            if leader:
                brushes.append(pg.mkBrush(QColor(WARN)))
            elif np.isfinite(roi):
                brushes.append(pg.mkBrush(QColor(GOOD if roi > 0 else BAD)))
            else:
                brushes.append(pg.mkBrush(QColor(ACCENT)))

        self._graph_item.setData(
            pos=pos,
            adj=edges,
            pen=pens if pens is not None else pg.mkPen(color=QColor(79, 156, 249, 90)),
            size=np.asarray(sizes, dtype=float),
            symbol="o",
            symbolBrush=brushes,
            symbolPen=pg.mkPen(color=QColor(20, 22, 28), width=1),
            pxMode=True,
        )

        for i, n in enumerate(nodes):
            name = str(info.display.get(n, n[:8])) if "display" in info.columns else n[:8]
            label = pg.TextItem(name[:18], color=FG_DIM, anchor=(0.5, 1.4))
            label.setPos(pos[i][0], pos[i][1])
            self.plot.addItem(label)
            self._labels.append(label)

        self.plot.autoRange(padding=0.15)
        self.caption.setText(
            f"{len(nodes)} wallets, {graph.number_of_edges()} links   ·   "
            "node size = stake, gold = lead mover, green/red = P&L sign, "
            "edge thickness = similarity"
        )

    def _on_click(self, _scatter: Any, points: Any) -> None:
        if not len(points) or not self._wallets:
            return
        idx = points[0].index()
        if 0 <= idx < len(self._wallets):
            self.node_clicked.emit(self._wallets[idx])


class EntryTimelineView(QWidget):
    """When each member entered a shared bet, and at what price.

    This is the visual answer to "who is the first bettor" and "how far off did
    each of them enter" - one row per bet, one dot per member.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.caption = QLabel("Select a cluster to see entry timing.")
        self.caption.setObjectName("dim")
        layout.addWidget(self.caption)

        self.plot = pg.PlotWidget(axisItems={"bottom": TimeAxis(orientation="bottom")})
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("left", "Shared bet")
        layout.addWidget(self.plot, 1)
        self._scatter: pg.ScatterPlotItem | None = None

    def clear(self) -> None:
        self.plot.clear()
        self._scatter = None

    def show_cluster(self, positions: pd.DataFrame, bets: pd.DataFrame, top_n: int = 22) -> None:
        self.clear()
        if positions is None or positions.empty or bets is None or bets.empty:
            self.caption.setText("No shared bets to plot.")
            return

        shared = bets[bets.n_members >= 2].nlargest(top_n, "total_usd")
        if shared.empty:
            self.caption.setText("This cluster has no bets held by two or more members.")
            return

        rows = positions[positions.bet_key.isin(set(shared.bet_key))].copy()
        if rows.empty:
            self.caption.setText("No shared bets to plot.")
            return

        order = {k: i for i, k in enumerate(shared.bet_key.tolist()[::-1])}
        rows["y"] = rows.bet_key.map(order)
        rows = rows.dropna(subset=["y", "first_buy_ts"])

        usd = rows.buy_usd.to_numpy(dtype=float)
        sizes = 6 + 20 * np.sqrt(usd / max(usd.max(), 1e-9))
        prices = rows.vwap_entry.to_numpy(dtype=float)
        brushes = [
            pg.mkBrush(QColor(int(255 * (1 - p)), int(90 + 120 * p), 120, 210))
            for p in np.clip(prices, 0, 1)
        ]

        # Mark the first entrant on each bet so leaders stand out immediately.
        first_idx = rows.groupby("bet_key").first_buy_ts.idxmin()
        pens = [
            pg.mkPen(QColor(WARN), width=2.2) if i in set(first_idx) else pg.mkPen(None)
            for i in rows.index
        ]

        spots = [
            {
                "pos": (float(r.first_buy_ts), float(r.y)),
                "size": float(s),
                "brush": b,
                "pen": p,
                "data": f"{r.proxy_wallet[:10]}  ${r.buy_usd:,.0f} @ {r.vwap_entry:.3f}",
            }
            for r, s, b, p in zip(rows.itertuples(), sizes, brushes, pens)
        ]
        self._scatter = pg.ScatterPlotItem(spots=spots, hoverable=True, tip=lambda **k: k.get("data", ""))
        self.plot.addItem(self._scatter)

        axis = self.plot.getAxis("left")
        labels = [
            (i, str(shared.set_index("bet_key").question.get(k, k))[:44])
            for k, i in order.items()
        ]
        axis.setTicks([labels])
        axis.setWidth(300)
        self.plot.autoRange(padding=0.08)
        self.caption.setText(
            f"Top {len(shared)} shared bets by stake   ·   dot size = stake, "
            "colour = entry price (red cheap → green expensive), gold ring = first in"
        )


class MetricBarView(QWidget):
    """Horizontal bars comparing one metric across selected clusters."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, alpha=0.15)
        layout.addWidget(self.plot)

    def show_metric(self, labels: list[str], values: list[float], title: str) -> None:
        self.plot.clear()
        self.plot.setTitle(title, color=FG, size="10pt")
        if not labels:
            return
        y = np.arange(len(labels))
        vals = np.nan_to_num(np.array(values, dtype=float), nan=0.0)
        brushes = [
            pg.mkBrush(QColor(GOOD if v >= 0 else BAD)) for v in vals
        ]
        bar = pg.BarGraphItem(x0=0, y=y, height=0.62, width=vals, brushes=brushes)
        self.plot.addItem(bar)
        self.plot.getAxis("left").setTicks([list(zip(y.tolist(), labels))])
        self.plot.getAxis("left").setWidth(120)
        self.plot.autoRange(padding=0.1)
