"""A table with a search box, column picker and CSV export."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from ..models import Col, DataFrameModel, SortProxy


class MetricTable(QWidget):
    """Table + toolbar used for clusters, members, bets and markets alike."""

    row_selected = Signal(object)      # pd.Series or None
    row_activated = Signal(object)     # double-click
    watch_toggled = Signal()           # a star was clicked

    #: Column injected at position 0 when the table supports starring.
    STAR_COL = Col("_watched", "★", "star", 30,
                   tip="Click to add or remove this row from the watchlist")

    def __init__(
        self,
        columns: list[Col],
        *,
        title: str = "",
        parent: QWidget | None = None,
        show_toolbar: bool = True,
    ):
        super().__init__(parent)
        self._columns = columns
        self._all_columns = list(columns)
        self._watch_store: Any = None
        self._watch_kind: str = ""
        self._watch_ref: Any = None
        self._watch_label: Any = None

        self.model = DataFrameModel(columns, self)
        self.proxy = SortProxy(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(23)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.horizontalHeader().customContextMenuRequested.connect(self._column_menu)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.table.doubleClicked.connect(self._on_activated)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        if show_toolbar:
            bar = QHBoxLayout()
            bar.setSpacing(6)
            if title:
                lbl = QLabel(title)
                lbl.setObjectName("h1")
                bar.addWidget(lbl)
            self.search = QLineEdit()
            self.search.setPlaceholderText("Filter rows…")
            self.search.setClearButtonEnabled(True)
            self.search.textChanged.connect(self.proxy.set_search)
            bar.addWidget(self.search, 1)
            self.count_label = QLabel("0 rows")
            self.count_label.setObjectName("dim")
            bar.addWidget(self.count_label)
            export = QPushButton("Export CSV")
            export.clicked.connect(self.export_csv)
            bar.addWidget(export)
            layout.addLayout(bar)
        else:
            self.search = None
            self.count_label = None

        layout.addWidget(self.table, 1)

        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        copy = QAction("Copy", self)
        copy.setShortcut(QKeySequence.Copy)
        copy.triggered.connect(self.copy_selection)
        self.addAction(copy)

    # -- watchlist ----------------------------------------------------------
    def enable_watching(self, store: Any, kind: str, ref_fn: Any, label_fn: Any) -> None:
        """Prepend a star column bound to ``store``.

        ``ref_fn(row)`` returns the identity dict for the watchlist, and
        ``label_fn(row)`` the human-readable name shown there.
        """
        self._watch_store = store
        self._watch_kind = kind
        self._watch_ref = ref_fn
        self._watch_label = label_fn
        if not self._columns or self._columns[0].key != self.STAR_COL.key:
            self._columns = [self.STAR_COL] + list(self._columns)
            self._all_columns = [self.STAR_COL] + list(self._all_columns)
        self.table.clicked.connect(self._maybe_toggle_star)

    def _annotate_watched(self, df: pd.DataFrame) -> pd.DataFrame:
        if self._watch_store is None or df is None or df.empty:
            return df
        df = df.copy()
        try:
            df["_watched"] = [
                self._watch_store.is_watched(self._watch_kind, self._watch_ref(r))
                for r in df.itertuples()
            ]
        except Exception:  # noqa: BLE001 - a bad ref must not break the table
            df["_watched"] = False
        return df

    def _maybe_toggle_star(self, index: Any) -> None:
        spec = self.model.column_spec(index.column())
        if spec is None or spec.fmt != "star" or self._watch_store is None:
            return
        row = self.model.row_record(self.proxy.mapToSource(index).row())
        if row is None:
            return
        try:
            self._watch_store.toggle(
                self._watch_kind, self._watch_ref(row), str(self._watch_label(row))
            )
        except Exception:  # noqa: BLE001
            return
        self.refresh_watch_column()
        self.watch_toggled.emit()

    def refresh_watch_column(self) -> None:
        if self._watch_store is None or self.model.dataframe.empty:
            return
        self.model.set_dataframe(
            self._annotate_watched(self.model.dataframe), self._columns
        )
        self._apply_widths()

    # -- data ---------------------------------------------------------------
    def set_dataframe(self, df: pd.DataFrame, columns: list[Col] | None = None) -> None:
        if columns is not None:
            star = self._columns[0] if self._columns and self._columns[0].key == self.STAR_COL.key else None
            self._columns = ([star] if star else []) + list(columns)
            self._all_columns = list(self._columns)
        df = self._annotate_watched(df if df is not None else pd.DataFrame())
        self.model.set_dataframe(df, self._columns)
        self._apply_widths()
        if self.count_label is not None:
            self.count_label.setText(f"{len(self.model.dataframe):,} rows")

    def _apply_widths(self) -> None:
        for i, spec in enumerate(getattr(self.model, "_visible", [])):
            self.table.setColumnWidth(i, spec.width)

    def current_row(self) -> pd.Series | None:
        idx = self.table.selectionModel().currentIndex()
        if not idx.isValid():
            return None
        return self.model.row_record(self.proxy.mapToSource(idx).row())

    def selected_rows(self) -> pd.DataFrame:
        rows = {
            self.proxy.mapToSource(i).row()
            for i in self.table.selectionModel().selectedRows()
        }
        if not rows:
            return pd.DataFrame()
        return self.model.dataframe.iloc[sorted(rows)]

    def visible_dataframe(self) -> pd.DataFrame:
        """What the user currently sees, honouring both filter and sort."""
        order = [
            self.proxy.mapToSource(self.proxy.index(r, 0)).row()
            for r in range(self.proxy.rowCount())
        ]
        if not order:
            return pd.DataFrame()
        return self.model.dataframe.iloc[order]

    def select_first(self) -> None:
        if self.proxy.rowCount():
            self.table.selectRow(0)

    # -- interaction --------------------------------------------------------
    def _on_selection(self, *_a: Any) -> None:
        self.row_selected.emit(self.current_row())

    def _on_activated(self, *_a: Any) -> None:
        row = self.current_row()
        if row is not None:
            self.row_activated.emit(row)

    def _column_menu(self, pos: Any) -> None:
        menu = QMenu(self)
        menu.addAction("Show all columns").triggered.connect(self._show_all)
        menu.addAction("Fit columns to contents").triggered.connect(
            self.table.resizeColumnsToContents
        )
        menu.addSeparator()
        for spec in self._all_columns:
            act = menu.addAction(spec.title)
            act.setCheckable(True)
            act.setChecked(spec in self._columns)
            act.toggled.connect(lambda on, s=spec: self._toggle_column(s, on))
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _toggle_column(self, spec: Col, on: bool) -> None:
        if on and spec not in self._columns:
            order = {c.key: i for i, c in enumerate(self._all_columns)}
            self._columns = sorted(self._columns + [spec], key=lambda c: order[c.key])
        elif not on and spec in self._columns:
            self._columns = [c for c in self._columns if c.key != spec.key]
        self.model.set_dataframe(self.model.dataframe, self._columns)
        self._apply_widths()

    def _show_all(self) -> None:
        self._columns = list(self._all_columns)
        self.model.set_dataframe(self.model.dataframe, self._columns)
        self._apply_widths()

    def _row_menu(self, pos: Any) -> None:
        menu = QMenu(self)
        menu.addAction("Copy selection").triggered.connect(self.copy_selection)
        menu.addAction("Export visible rows to CSV…").triggered.connect(self.export_csv)
        row = self.current_row()
        if row is not None:
            for key, label in (
                ("proxy_wallet", "Copy wallet address"),
                ("first_entrant", "Copy first-entrant address"),
                ("biggest_bettor", "Copy biggest-bettor address"),
                ("condition_id", "Copy condition id"),
            ):
                if key in row.index and isinstance(row[key], str) and row[key]:
                    menu.addAction(label).triggered.connect(
                        lambda _c=False, v=row[key]: self._copy_text(v)
                    )
            for key, base in (
                ("proxy_wallet", "https://polymarket.com/profile/"),
                ("market_slug", "https://polymarket.com/market/"),
                ("slug", "https://polymarket.com/market/"),
            ):
                if key in row.index and isinstance(row[key], str) and row[key]:
                    menu.addAction(f"Open {base.split('/')[-2]} on Polymarket").triggered.connect(
                        lambda _c=False, u=base + row[key]: self._open_url(u)
                    )
                    break
        menu.exec(self.table.viewport().mapToGlobal(pos))

    @staticmethod
    def _copy_text(text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(str(text))

    @staticmethod
    def _open_url(url: str) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl(url))

    def copy_selection(self) -> None:
        df = self.selected_rows()
        if df.empty:
            df = self.visible_dataframe()
        if not df.empty:
            self._copy_text(df.to_csv(sep="\t", index=False))

    def export_csv(self) -> None:
        df = self.visible_dataframe()
        if df.empty:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", str(Path.home() / "polycluster_export.csv"), "CSV (*.csv)"
        )
        if not path:
            return
        try:
            df.to_csv(path, index=False, encoding="utf-8-sig")
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export", f"Wrote {len(df):,} rows to\n{path}")
