"""Compare tab: metrics as rows, clusters as columns, plus a bar chart."""

from __future__ import annotations

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...analysis.engine import AnalysisResult
from ..columns import COMPARE_METRICS
from ..models import fmt_value
from ..theme import BAD, FG, FG_DIM, GOOD, heat_color
from ..widgets.charts import MetricBarView


class ComparePanel(QWidget):
    """Side-by-side view of any subset of the detected clusters."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._result: AnalysisResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        title = QLabel("Cluster comparison")
        title.setObjectName("h1")
        bar.addWidget(title)
        bar.addStretch(1)
        bar.addWidget(QLabel("Chart metric:"))
        self.metric_box = QComboBox()
        for key, label, _fmt in COMPARE_METRICS:
            self.metric_box.addItem(label, key)
        self.metric_box.setCurrentIndex(0)
        self.metric_box.currentIndexChanged.connect(self._refresh_chart)
        bar.addWidget(self.metric_box)
        btn_top = QPushButton("Select top 6")
        btn_top.clicked.connect(lambda: self._select_top(6))
        bar.addWidget(btn_top)
        btn_none = QPushButton("Clear")
        btn_none.clicked.connect(self._select_none)
        bar.addWidget(btn_none)
        root.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)
        left_lay.addWidget(QLabel("Clusters to compare"))
        self.picker = QListWidget()
        self.picker.itemChanged.connect(self._refresh)
        left_lay.addWidget(self.picker, 1)
        splitter.addWidget(left)

        right = QSplitter(Qt.Vertical)
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        right.addWidget(self.table)
        self.chart = MetricBarView()
        right.addWidget(self.chart)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([200, 900])

    # -- population ---------------------------------------------------------
    def set_result(self, result: AnalysisResult) -> None:
        self._result = result
        self.picker.blockSignals(True)
        self.picker.clear()
        if not result.clusters.empty:
            for row in result.clusters.itertuples():
                item = QListWidgetItem(
                    f"Cluster {int(row.cluster_id)}  ·  {int(row.n_members)}w  "
                    f"·  {fmt_value(row.suspicion_score, 'num')}"
                )
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                item.setData(Qt.UserRole, int(row.cluster_id))
                self.picker.addItem(item)
        self.picker.blockSignals(False)
        self._select_top(6)

    def _selected_ids(self) -> list[int]:
        return [
            int(self.picker.item(i).data(Qt.UserRole))
            for i in range(self.picker.count())
            if self.picker.item(i).checkState() == Qt.Checked
        ]

    def _select_top(self, n: int) -> None:
        self.picker.blockSignals(True)
        for i in range(self.picker.count()):
            self.picker.item(i).setCheckState(Qt.Checked if i < n else Qt.Unchecked)
        self.picker.blockSignals(False)
        self._refresh()

    def _select_none(self) -> None:
        self.picker.blockSignals(True)
        for i in range(self.picker.count()):
            self.picker.item(i).setCheckState(Qt.Unchecked)
        self.picker.blockSignals(False)
        self._refresh()

    # -- rendering ----------------------------------------------------------
    def _refresh(self, *_a: object) -> None:
        self._refresh_table()
        self._refresh_chart()

    def _frame(self) -> pd.DataFrame:
        if self._result is None or self._result.clusters.empty:
            return pd.DataFrame()
        ids = self._selected_ids()
        if not ids:
            return pd.DataFrame()
        df = self._result.clusters
        return df[df.cluster_id.isin(ids)].set_index("cluster_id").reindex(ids)

    def _refresh_table(self) -> None:
        df = self._frame()
        self.table.clear()
        if df.empty:
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            return

        self.table.setRowCount(len(COMPARE_METRICS))
        self.table.setColumnCount(len(df))
        self.table.setHorizontalHeaderLabels([f"Cluster {i}" for i in df.index])
        self.table.setVerticalHeaderLabels([label for _k, label, _f in COMPARE_METRICS])

        for r, (key, _label, fmt) in enumerate(COMPARE_METRICS):
            if key not in df.columns:
                for c in range(len(df)):
                    self.table.setItem(r, c, QTableWidgetItem("—"))
                continue
            values = pd.to_numeric(df[key], errors="coerce")
            finite = values[np.isfinite(values)]
            lo, hi = (finite.min(), finite.max()) if len(finite) else (0.0, 0.0)
            for c, value in enumerate(values):
                item = QTableWidgetItem(fmt_value(value, fmt))
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if np.isfinite(value) and hi > lo and len(finite) > 1:
                    item.setBackground(heat_color(float((value - lo) / (hi - lo)), 60))
                self.table.setItem(r, c, item)

    def _refresh_chart(self) -> None:
        df = self._frame()
        key = self.metric_box.currentData()
        label = self.metric_box.currentText()
        if df.empty or key not in df.columns:
            self.chart.show_metric([], [], label)
            return
        self.chart.show_metric(
            [f"C{i}" for i in df.index],
            pd.to_numeric(df[key], errors="coerce").tolist(),
            label,
        )
