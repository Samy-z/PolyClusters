"""A sortable, formatted, heat-shaded Qt table model over a pandas DataFrame."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor

from .theme import BAD, FG, FG_DIM, GOOD, WARN, heat_color


@dataclass(frozen=True)
class Col:
    """Presentation spec for one DataFrame column."""

    key: str
    title: str
    fmt: str = "auto"     # auto|usd|pct|ratio|num|int|ts|hours|bool|text|wallet
    width: int = 100
    heat: bool = False    # shade by percentile within the column
    invert: bool = False  # for heat: lower values are "better"
    tip: str = ""


def fmt_value(value: Any, fmt: str) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d %H:%M")
    try:
        if fmt == "usd":
            v = float(value)
            for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
                if abs(v) >= cut:
                    return f"${v / cut:,.2f}{suffix}"
            return f"${v:,.0f}"
        if fmt == "pct":
            return f"{float(value) * 100:,.1f}%"
        if fmt == "ratio":
            return f"{float(value):.3f}"
        if fmt == "num":
            return f"{float(value):,.2f}"
        if fmt == "int":
            return f"{int(value):,}"
        if fmt == "hours":
            v = float(value)
            if abs(v) >= 48:
                return f"{v / 24:,.1f}d"
            if abs(v) < 1:
                return f"{v * 60:,.0f}m"
            return f"{v:,.1f}h"
        if fmt == "ts":
            v = int(value)
            if v <= 0:
                return "—"
            return datetime.fromtimestamp(v, timezone.utc).strftime("%Y-%m-%d %H:%M")
        if fmt == "bool":
            if isinstance(value, float) and np.isnan(value):
                return "—"
            return "yes" if bool(value) else "—"
        if fmt == "wallet":
            s = str(value)
            return f"{s[:6]}…{s[-4:]}" if len(s) > 14 else s
        if fmt == "star":
            return "★" if value else "☆"  # filled vs hollow star
    except (TypeError, ValueError):
        return str(value)
    # auto / text
    if isinstance(value, float):
        return f"{value:,.3f}"
    if isinstance(value, (bool, np.bool_)):
        return "yes" if value else "—"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    return str(value)


class DataFrameModel(QAbstractTableModel):
    """Renders a DataFrame through an explicit column spec list."""

    def __init__(self, columns: list[Col] | None = None, parent: Any = None):
        super().__init__(parent)
        self._df = pd.DataFrame()
        self._cols: list[Col] = columns or []
        self._ranks: dict[str, np.ndarray] = {}

    # -- data plumbing ------------------------------------------------------
    def set_dataframe(self, df: pd.DataFrame, columns: list[Col] | None = None) -> None:
        self.beginResetModel()
        if columns is not None:
            self._cols = columns
        if df is None:
            df = pd.DataFrame()
        if not self._cols and not df.empty:
            self._cols = [Col(c, c.replace("_", " ").title()) for c in df.columns]
        present = [c for c in self._cols if c.key in df.columns]
        self._visible = present
        self._df = df.reset_index(drop=True)
        self._precompute_heat()
        self.endResetModel()

    def _precompute_heat(self) -> None:
        """Cache percentile ranks so painting stays cheap during scrolling."""
        self._ranks = {}
        for col in getattr(self, "_visible", []):
            if not col.heat or col.key not in self._df.columns:
                continue
            series = pd.to_numeric(self._df[col.key], errors="coerce")
            if series.notna().sum() < 2:
                continue
            ranks = series.rank(pct=True, na_option="keep").to_numpy(dtype=float)
            if col.invert:
                ranks = 1.0 - ranks
            self._ranks[col.key] = ranks

    @property
    def dataframe(self) -> pd.DataFrame:
        return self._df

    def row_record(self, row: int) -> pd.Series | None:
        if 0 <= row < len(self._df):
            return self._df.iloc[row]
        return None

    def column_spec(self, col: int) -> Col | None:
        vis = getattr(self, "_visible", [])
        return vis[col] if 0 <= col < len(vis) else None

    # -- QAbstractTableModel ------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._df)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(getattr(self, "_visible", []))

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:  # noqa: N802
        if not index.isValid():
            return None
        spec = self.column_spec(index.column())
        if spec is None:
            return None
        value = self._df.iat[index.row(), self._df.columns.get_loc(spec.key)]

        if role == Qt.DisplayRole:
            return fmt_value(value, spec.fmt)
        if role == Qt.ToolTipRole:
            if spec.fmt == "star":
                return "Click to add or remove this row from the watchlist"
            return f"{spec.title}: {value}" + (f"\n{spec.tip}" if spec.tip else "")
        if role == Qt.TextAlignmentRole:
            if spec.fmt == "star":
                return int(Qt.AlignCenter)
            numeric = spec.fmt in ("usd", "pct", "ratio", "num", "int", "hours")
            return int(Qt.AlignVCenter | (Qt.AlignRight if numeric else Qt.AlignLeft))
        if role == Qt.BackgroundRole:
            ranks = self._ranks.get(spec.key)
            if ranks is not None and index.row() < len(ranks):
                r = ranks[index.row()]
                if np.isfinite(r):
                    return heat_color(float(r))
            return None
        if role == Qt.ForegroundRole:
            if spec.fmt == "star":
                return QColor(WARN) if value else QColor(FG_DIM)
            if spec.fmt in ("pct", "ratio", "num", "usd") and isinstance(
                value, (int, float, np.integer, np.floating)
            ) and np.isfinite(value) and not spec.heat:
                if spec.key in ("pnl", "roi", "resolved_roi", "shared_roi",
                                "unanimous_roi", "longshot_roi", "edge_per_share",
                                "mean_edge_per_share", "entry_vs_cluster",
                                "avg_entry_vs_cluster", "entry_vs_market",
                                "avg_lead_time_h", "median_lead_time_h"):
                    return QColor(GOOD) if value > 0 else (
                        QColor(BAD) if value < 0 else QColor(FG_DIM)
                    )
            if value is None or (isinstance(value, float) and not np.isfinite(value)):
                return QColor(FG_DIM)
            return QColor(FG)
        # Sort on the raw value, never the formatted string.
        if role == Qt.UserRole:
            if isinstance(value, (bool, np.bool_)):
                return int(value)
            if isinstance(value, (int, float, np.integer, np.floating)):
                return float(value) if np.isfinite(value) else float("-inf")
            return str(value)
        return None

    def headerData(  # noqa: N802
        self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole
    ) -> Any:
        if orientation != Qt.Horizontal:
            if role == Qt.DisplayRole:
                return str(section + 1)
            return None
        spec = self.column_spec(section)
        if spec is None:
            return None
        if role == Qt.DisplayRole:
            return spec.title
        if role == Qt.ToolTipRole:
            return spec.tip or spec.title
        return None


class SortProxy(QSortFilterProxyModel):
    """Sorts on the model's raw UserRole value and free-text filters all columns."""

    def __init__(self, parent: Any = None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._needle = ""

    def set_search(self, text: str) -> None:
        self._needle = (text or "").strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        if not self._needle:
            return True
        model = self.sourceModel()
        for col in range(model.columnCount()):
            idx = model.index(row, col, parent)
            if self._needle in str(model.data(idx, Qt.DisplayRole) or "").lower():
                return True
            if self._needle in str(model.data(idx, Qt.UserRole) or "").lower():
                return True
        return False
