"""Left-hand control dock: window, sectors, exact markets, thresholds, weights."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import json
import time

from PySide6.QtCore import QDate, QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout,
    QWidget,
)

from ...analysis.cluster import ClusterParams
from ...config import AnalysisFilters, AppSettings
from ...core.db import Database
from ...ingest.gamma import CURATED_SECTOR_SLUGS
from ..theme import FG_DIM

PRESETS: list[tuple[str, int | None]] = [
    ("Last 24 hours", 1),
    ("Last 3 days", 3),
    ("Last 7 days", 7),
    ("Last 14 days", 14),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("Last 180 days", 180),
    ("Last 365 days", 365),
    ("Everything ingested", None),
    ("Custom range…", -1),
]


class WheelBlocker(QObject):
    """Swallows wheel events on value widgets.

    The whole panel is one tall scroll area, so a wheel roll aimed at scrolling
    would silently retune whichever spin box or combo happened to be under the
    cursor. Blocking the event lets the scroll pass through to the panel.
    """

    def eventFilter(self, obj: QObject, event: Any) -> bool:  # noqa: N802
        if event.type() == QEvent.Wheel:
            event.ignore()
            return True
        return super().eventFilter(obj, event)


def _utc_ts(qdate: QDate, end_of_day: bool = False) -> int:
    d = qdate.toPython()
    t = datetime(d.year, d.month, d.day, 23, 59, 59 if end_of_day else 0,
                 tzinfo=timezone.utc) if end_of_day else datetime(
        d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(t.timestamp())


class ControlPanel(QScrollArea):
    """Emits the filter/param set the rest of the app runs on."""

    ingest_requested = Signal(object, bool)   # AnalysisFilters, refresh_tags
    analyse_requested = Signal(object, object)  # AnalysisFilters, ClusterParams
    cancel_requested = Signal()
    market_search_requested = Signal(str)
    weights_changed = Signal(dict)

    def __init__(self, db: Database, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self._selected_markets: dict[str, str] = {}

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        self.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        root.addWidget(self._build_preset_group())
        root.addWidget(self._build_window_group())
        root.addWidget(self._build_sector_group())
        root.addWidget(self._build_market_group())
        root.addWidget(self._build_wallet_group())
        root.addWidget(self._build_cluster_group())
        root.addWidget(self._build_weights_group())
        root.addLayout(self._build_actions())
        root.addStretch(1)

        self._on_preset_changed()
        self.reload_tags()
        self._block_wheel()
        self.reload_presets()

    def _block_wheel(self) -> None:
        """Stop the wheel from editing values anywhere in the panel."""
        self._wheel_blocker = WheelBlocker(self)
        for kind in (QAbstractSpinBox, QComboBox, QDateEdit):
            for widget in self.findChildren(kind):
                widget.installEventFilter(self._wheel_blocker)
                # Without this a widget can still take focus by hover-scroll on
                # some styles; StrongFocus means click or tab only.
                widget.setFocusPolicy(Qt.StrongFocus)

    # -- presets ------------------------------------------------------------
    def _build_preset_group(self) -> QGroupBox:
        box = QGroupBox("Saved setups")
        lay = QVBoxLayout(box)
        lay.setSpacing(5)

        self.preset_box = QComboBox()
        self.preset_box.setToolTip(
            "Every control on this panel, saved under a name and reloaded on "
            "later runs."
        )
        self.preset_box.activated.connect(self._apply_selected_preset)
        lay.addWidget(self.preset_box)

        row = QHBoxLayout()
        save = QPushButton("Save…")
        save.setToolTip("Store the current controls under a name.")
        save.clicked.connect(self.save_preset)
        row.addWidget(save)
        update = QPushButton("Update")
        update.setToolTip("Overwrite the selected setup with the current controls.")
        update.clicked.connect(self.update_preset)
        row.addWidget(update)
        delete = QPushButton("Delete")
        delete.setObjectName("danger")
        delete.clicked.connect(self.delete_preset)
        row.addWidget(delete)
        lay.addLayout(row)
        return box

    def reload_presets(self, select: str | None = None) -> None:
        try:
            df = self.db.query("SELECT name FROM presets ORDER BY name")
        except Exception:  # noqa: BLE001
            df = pd.DataFrame()
        self.preset_box.blockSignals(True)
        self.preset_box.clear()
        self.preset_box.addItem("— current (unsaved) —", "")
        for row in df.itertuples():
            self.preset_box.addItem(row.name, row.name)
        if select:
            idx = self.preset_box.findData(select)
            if idx >= 0:
                self.preset_box.setCurrentIndex(idx)
        self.preset_box.blockSignals(False)

    def save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save setup", "Name for this setup:")
        name = (name or "").strip()
        if not ok or not name:
            return
        existing = self.db.scalar("SELECT count(*) FROM presets WHERE name = ?", [name])
        if existing and QMessageBox.question(
            self, "Overwrite?", f"“{name}” already exists. Replace it?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._write_preset(name)
        self.reload_presets(select=name)

    def update_preset(self) -> None:
        name = self.preset_box.currentData()
        if not name:
            self.save_preset()
            return
        self._write_preset(name)
        self.reload_presets(select=name)

    def _write_preset(self, name: str) -> None:
        now = int(time.time())
        self.db.execute(
            "INSERT INTO presets (name, created_at, updated_at, payload_json) "
            "VALUES (?,?,?,?) ON CONFLICT (name) DO UPDATE SET "
            "updated_at = excluded.updated_at, payload_json = excluded.payload_json",
            [name, now, now, json.dumps(self.export_state())],
        )

    def delete_preset(self) -> None:
        name = self.preset_box.currentData()
        if not name:
            return
        if QMessageBox.question(
            self, "Delete setup", f"Delete “{name}”?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.db.execute("DELETE FROM presets WHERE name = ?", [name])
        self.reload_presets()

    def _apply_selected_preset(self, *_a: Any) -> None:
        name = self.preset_box.currentData()
        if not name:
            return
        raw = self.db.scalar("SELECT payload_json FROM presets WHERE name = ?", [name])
        if raw:
            self.apply_state(json.loads(raw))

    # -- state serialisation -------------------------------------------------
    def export_state(self) -> dict[str, Any]:
        """Every control on the panel, as plain JSON-able values."""
        state: dict[str, Any] = {
            "preset_index": self.preset.currentIndex(),
            "date_from": self.date_from.date().toString("yyyy-MM-dd"),
            "date_to": self.date_to.date().toString("yyyy-MM-dd"),
            "tag_ids": self.selected_tag_ids(),
            "markets": dict(self._selected_markets),
            "market_mode": self.market_mode.currentData(),
            "resolved_only": self.resolved_only.isChecked(),
            "method": self.method.currentText(),
            "size_weighting": self.size_weighting.currentText(),
            "use_idf": self.use_idf.isChecked(),
            "timing_bonus": self.timing_bonus.isChecked(),
            "weights": {k: w.value() for k, w in self.weight_widgets.items()},
        }
        for name in ("min_market_volume", "max_markets", "min_user_usd", "min_user_bets",
                     "max_user_bets", "min_position_usd", "min_entry_price",
                     "max_entry_price", "min_shared", "similarity", "resolution",
                     "min_cluster_size", "max_bet_frac", "timing_window", "core_pct"):
            widget = getattr(self, name, None)
            if widget is not None:
                state[name] = widget.value()
        return state

    def apply_state(self, state: dict[str, Any]) -> None:
        if not state:
            return
        for name in ("min_market_volume", "max_markets", "min_user_usd", "min_user_bets",
                     "max_user_bets", "min_position_usd", "min_entry_price",
                     "max_entry_price", "min_shared", "similarity", "resolution",
                     "min_cluster_size", "max_bet_frac", "timing_window", "core_pct"):
            widget = getattr(self, name, None)
            if widget is not None and name in state:
                widget.setValue(type(widget.value())(state[name]))

        if "preset_index" in state:
            self.preset.setCurrentIndex(int(state["preset_index"]))
            self._on_preset_changed()
        for key, edit in (("date_from", self.date_from), ("date_to", self.date_to)):
            if state.get(key):
                edit.setDate(QDate.fromString(state[key], "yyyy-MM-dd"))

        self.resolved_only.setChecked(bool(state.get("resolved_only", False)))
        self.use_idf.setChecked(bool(state.get("use_idf", True)))
        self.timing_bonus.setChecked(bool(state.get("timing_bonus", True)))
        for combo, key in ((self.method, "method"), (self.size_weighting, "size_weighting")):
            if state.get(key):
                idx = combo.findText(state[key])
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        if state.get("market_mode"):
            idx = self.market_mode.findData(state["market_mode"])
            if idx >= 0:
                self.market_mode.setCurrentIndex(idx)

        for attr, value in (state.get("weights") or {}).items():
            if attr in self.weight_widgets:
                self.weight_widgets[attr].setValue(float(value))

        self._selected_markets = dict(state.get("markets") or {})
        self.market_selected.clear()
        for cid, question in self._selected_markets.items():
            entry = QListWidgetItem(str(question)[:80])
            entry.setData(Qt.UserRole, cid)
            self.market_selected.addItem(entry)

        wanted = set(state.get("tag_ids") or [])
        self.tag_list.blockSignals(True)
        for i in range(self.tag_list.count()):
            item = self.tag_list.item(i)
            if self._is_selectable(item):
                item.setCheckState(
                    Qt.Checked if int(item.data(Qt.UserRole)) in wanted else Qt.Unchecked
                )
        self.tag_list.blockSignals(False)
        self._update_tag_count()
        self._emit_weights()

    # -- groups -------------------------------------------------------------
    def _build_window_group(self) -> QGroupBox:
        box = QGroupBox("Time window")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)

        self.preset = QComboBox()
        for label, _days in PRESETS:
            self.preset.addItem(label)
        self.preset.setCurrentIndex(4)  # Last 30 days
        self.preset.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow("Range", self.preset)

        today = QDate.currentDate()
        self.date_from = QDateEdit(today.addDays(-30))
        self.date_to = QDateEdit(today)
        for d in (self.date_from, self.date_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        form.addRow("From", self.date_from)
        form.addRow("To", self.date_to)

        self.window_hint = QLabel("")
        self.window_hint.setObjectName("dim")
        self.window_hint.setWordWrap(True)
        form.addRow(self.window_hint)
        return box

    def _build_sector_group(self) -> QGroupBox:
        box = QGroupBox("Sectors")
        lay = QVBoxLayout(box)
        lay.setSpacing(5)

        self.tag_search = QLineEdit()
        self.tag_search.setPlaceholderText("Search sectors (e.g. politics, geopolitics)…")
        self.tag_search.setClearButtonEnabled(True)
        self.tag_search.textChanged.connect(self._filter_tags)
        self.tag_search.returnPressed.connect(self._select_matching)
        lay.addWidget(self.tag_search)

        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QListWidget.NoSelection)
        self.tag_list.setMinimumHeight(230)  # keeps the pinned sectors on screen
        self.tag_list.itemChanged.connect(self._update_tag_count)
        lay.addWidget(self.tag_list)

        row = QHBoxLayout()
        select_matching = QPushButton("Select matching")
        select_matching.setToolTip(
            "Tick every sector matching the search box (Enter does the same)."
        )
        select_matching.clicked.connect(self._select_matching)
        row.addWidget(select_matching)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_tags)
        row.addWidget(clear)
        lay.addLayout(row)

        self.tag_count = QLabel("none selected")
        self.tag_count.setObjectName("dim")
        self.tag_count.setWordWrap(True)
        lay.addWidget(self.tag_count)

        self.tag_hint = QLabel("")
        self.tag_hint.setObjectName("dim")
        self.tag_hint.setWordWrap(True)
        lay.addWidget(self.tag_hint)
        return box

    def _build_market_group(self) -> QGroupBox:
        box = QGroupBox("Specific markets")
        lay = QVBoxLayout(box)
        lay.setSpacing(5)

        row = QHBoxLayout()
        self.market_search = QLineEdit()
        self.market_search.setPlaceholderText("Search a market or event slug…")
        self.market_search.returnPressed.connect(self._search_markets)
        row.addWidget(self.market_search, 1)
        btn = QPushButton("Find")
        btn.clicked.connect(self._search_markets)
        row.addWidget(btn)
        lay.addLayout(row)

        self.market_results = QListWidget()
        self.market_results.setMaximumHeight(120)
        self.market_results.itemDoubleClicked.connect(self._add_market)
        lay.addWidget(self.market_results)

        lay.addWidget(QLabel("Selected (double-click to remove):"))
        self.market_selected = QListWidget()
        self.market_selected.setMaximumHeight(90)
        self.market_selected.itemDoubleClicked.connect(self._remove_market)
        lay.addWidget(self.market_selected)

        self.market_mode = QComboBox()
        self.market_mode.addItem("Seed: cluster whoever bet these", "seed")
        self.market_mode.addItem("Restrict: analyse only these markets", "restrict")
        self.market_mode.setToolTip(
            "Seed (recommended for a single bet): take everyone who traded the\n"
            "selected market, then score their similarity across the whole\n"
            "universe. Restricting to one market cannot cluster anything - two\n"
            "wallets could share at most one bet."
        )
        lay.addWidget(self.market_mode)

        pick = QPushButton("Add markets from local database…")
        pick.clicked.connect(self._pick_local_markets)
        lay.addWidget(pick)

        form = QFormLayout()
        self.min_market_volume = QDoubleSpinBox()
        self.min_market_volume.setRange(0, 1e9)
        self.min_market_volume.setSingleStep(10_000)
        self.min_market_volume.setValue(self.settings.min_market_volume)
        self.min_market_volume.setPrefix("$ ")
        self.min_market_volume.setGroupSeparatorShown(True)
        form.addRow("Min market volume", self.min_market_volume)

        self.max_markets = QSpinBox()
        self.max_markets.setRange(10, 200_000)
        self.max_markets.setSingleStep(250)
        self.max_markets.setValue(self.settings.max_markets_per_fetch)
        self.max_markets.setGroupSeparatorShown(True)
        self.max_markets.setToolTip(
            "Fetch limit. An unfiltered 30-day window matches ~28,000 markets;\n"
            "crawling all of them takes hours. Only the highest-volume markets\n"
            "up to this cap are crawled. Markets you picked explicitly always\n"
            "get fetched regardless."
        )
        form.addRow("Max markets per fetch", self.max_markets)

        self.resolved_only = QCheckBox("Resolved markets only")
        self.resolved_only.setToolTip(
            "Win rate and ROI need a resolution. Turn this on when you care about\n"
            "measured performance rather than open positions to copy."
        )
        form.addRow(self.resolved_only)
        lay.addLayout(form)
        return box

    def _build_wallet_group(self) -> QGroupBox:
        box = QGroupBox("Wallet filter")
        form = QFormLayout(box)

        self.min_user_usd = QDoubleSpinBox()
        self.min_user_usd.setRange(0, 1e9)
        self.min_user_usd.setSingleStep(1_000)
        self.min_user_usd.setValue(self.settings.min_user_usd)
        self.min_user_usd.setPrefix("$ ")
        self.min_user_usd.setGroupSeparatorShown(True)
        self.min_user_usd.setToolTip("Minimum total stake in the window - this is the 'rich' gate.")
        form.addRow("Min wallet stake", self.min_user_usd)

        self.min_user_bets = QSpinBox()
        self.min_user_bets.setRange(1, 1000)
        self.min_user_bets.setValue(self.settings.min_user_bets)
        form.addRow("Min bets", self.min_user_bets)

        self.max_user_bets = QSpinBox()
        self.max_user_bets.setRange(2, 100_000)
        self.max_user_bets.setValue(self.settings.max_user_bets)
        self.max_user_bets.setToolTip(
            "Upper bound excludes market makers and spray bots, which touch\n"
            "thousands of markets and would dominate every co-occurrence graph."
        )
        form.addRow("Max bets", self.max_user_bets)

        self.min_position_usd = QDoubleSpinBox()
        self.min_position_usd.setRange(0, 1e9)
        self.min_position_usd.setSingleStep(100)
        self.min_position_usd.setValue(self.settings.min_position_usd)
        self.min_position_usd.setPrefix("$ ")
        self.min_position_usd.setGroupSeparatorShown(True)
        form.addRow("Min position size", self.min_position_usd)

        prices = QHBoxLayout()
        self.min_entry_price = QDoubleSpinBox()
        self.min_entry_price.setRange(0.0, 1.0)
        self.min_entry_price.setSingleStep(0.01)
        self.min_entry_price.setDecimals(2)
        self.min_entry_price.setValue(self.settings.min_entry_price)
        self.max_entry_price = QDoubleSpinBox()
        self.max_entry_price.setRange(0.0, 1.0)
        self.max_entry_price.setSingleStep(0.01)
        self.max_entry_price.setDecimals(2)
        self.max_entry_price.setValue(self.settings.max_entry_price)
        prices.addWidget(self.min_entry_price)
        prices.addWidget(QLabel("–"))
        prices.addWidget(self.max_entry_price)
        holder = QWidget()
        holder.setLayout(prices)
        holder.setToolTip(
            "Entry-price band. Excluding near-certain fills is the single most\n"
            "important noise filter: everyone who buys the 98c favourite 'agrees'\n"
            "and 'wins', which otherwise merges the whole population into one blob."
        )
        form.addRow("Entry price band", holder)
        return box

    def _build_cluster_group(self) -> QGroupBox:
        box = QGroupBox("Clustering")
        form = QFormLayout(box)

        self.min_shared = QSpinBox()
        self.min_shared.setRange(1, 100)
        self.min_shared.setValue(self.settings.min_shared_bets)
        self.min_shared.setToolTip("Two wallets need at least this many identical bets to be linked.")
        form.addRow("Min shared bets", self.min_shared)

        self.similarity = QDoubleSpinBox()
        self.similarity.setRange(0.0, 1.0)
        self.similarity.setSingleStep(0.05)
        self.similarity.setDecimals(2)
        self.similarity.setValue(self.settings.similarity_threshold)
        self.similarity.setToolTip("Cosine similarity gate on the IDF-weighted position vectors.")
        form.addRow("Similarity ≥", self.similarity)

        self.resolution = QDoubleSpinBox()
        self.resolution.setRange(0.1, 8.0)
        self.resolution.setSingleStep(0.1)
        self.resolution.setValue(self.settings.louvain_resolution)
        self.resolution.setToolTip("Higher = more, smaller communities.")
        form.addRow("Resolution", self.resolution)

        self.min_cluster_size = QSpinBox()
        self.min_cluster_size.setRange(2, 500)
        self.min_cluster_size.setValue(self.settings.min_cluster_size)
        form.addRow("Min cluster size", self.min_cluster_size)

        self.method = QComboBox()
        self.method.addItems(["louvain", "greedy_modularity", "components"])
        form.addRow("Method", self.method)

        self.max_bet_frac = QDoubleSpinBox()
        self.max_bet_frac.setRange(0.01, 1.0)
        self.max_bet_frac.setSingleStep(0.05)
        self.max_bet_frac.setDecimals(2)
        self.max_bet_frac.setValue(self.settings.max_bet_user_frac)
        self.max_bet_frac.setToolTip(
            "Bets held by more than this fraction of the pool are treated as\n"
            "stopwords and dropped - they carry no discriminating information."
        )
        form.addRow("Max bet popularity", self.max_bet_frac)

        self.timing_window = QDoubleSpinBox()
        self.timing_window.setRange(0.05, 720.0)
        self.timing_window.setSingleStep(1.0)
        self.timing_window.setValue(self.settings.timing_window_hours)
        self.timing_window.setSuffix(" h")
        self.timing_window.setToolTip("Entries within this gap count as synchronised.")
        form.addRow("Sync window", self.timing_window)

        self.core_pct = QDoubleSpinBox()
        self.core_pct.setRange(0.1, 1.0)
        self.core_pct.setSingleStep(0.05)
        self.core_pct.setDecimals(2)
        self.core_pct.setValue(self.settings.unanimity_core_pct)
        form.addRow("Core membership", self.core_pct)

        self.use_idf = QCheckBox("Weight rare bets higher (IDF)")
        self.use_idf.setChecked(True)
        form.addRow(self.use_idf)

        self.timing_bonus = QCheckBox("Boost synchronised pairs")
        self.timing_bonus.setChecked(True)
        form.addRow(self.timing_bonus)

        self.size_weighting = QComboBox()
        self.size_weighting.addItems(["log_usd", "usd", "binary"])
        form.addRow("Size weighting", self.size_weighting)
        return box

    def _build_weights_group(self) -> QGroupBox:
        box = QGroupBox("Suspicion score weights")
        form = QFormLayout(box)
        self.weight_widgets: dict[str, QDoubleSpinBox] = {}
        for attr, label in (
            ("weight_roi", "Realised edge / ROI"),
            ("weight_winrate", "Win rate"),
            ("weight_unanimity", "Agreement"),
            ("weight_sync", "Timing sync"),
            ("weight_earliness", "Earliness"),
            ("weight_rarity", "Bet rarity"),
            ("weight_wealth", "Wallet size"),
        ):
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 5.0)
            spin.setSingleStep(0.25)
            spin.setValue(getattr(self.settings, attr))
            spin.valueChanged.connect(self._emit_weights)
            form.addRow(label, spin)
            self.weight_widgets[attr] = spin
        note = QLabel("Re-ranks instantly; no re-run needed.")
        note.setObjectName("dim")
        form.addRow(note)
        return box

    def _build_actions(self) -> QVBoxLayout:
        lay = QVBoxLayout()
        lay.setSpacing(6)

        self.refresh_tags_check = QCheckBox("Also refresh sector catalogue")
        lay.addWidget(self.refresh_tags_check)

        self.btn_ingest = QPushButton("1 · Fetch data from Polymarket")
        self.btn_ingest.clicked.connect(
            lambda: self.ingest_requested.emit(
                self.filters(), self.refresh_tags_check.isChecked()
            )
        )
        lay.addWidget(self.btn_ingest)

        self.btn_analyse = QPushButton("2 · Run cluster analysis")
        self.btn_analyse.setObjectName("primary")
        self.btn_analyse.clicked.connect(
            lambda: self.analyse_requested.emit(self.filters(), self.cluster_params())
        )
        lay.addWidget(self.btn_analyse)

        self.btn_cancel = QPushButton("Cancel running job")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_requested.emit)
        lay.addWidget(self.btn_cancel)
        return lay

    # -- behaviour ----------------------------------------------------------
    def _on_preset_changed(self) -> None:
        days = PRESETS[self.preset.currentIndex()][1]
        custom = days == -1
        self.date_from.setEnabled(custom)
        self.date_to.setEnabled(custom)
        if days is None:
            self.window_hint.setText("Every trade in the local database.")
        elif custom:
            self.window_hint.setText("Pick the exact dates above (UTC).")
        else:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)
            self.date_from.setDate(QDate(start.year, start.month, start.day))
            self.date_to.setDate(QDate(end.year, end.month, end.day))
            self.window_hint.setText(
                f"{start:%Y-%m-%d} → {end:%Y-%m-%d} UTC ({days}d)"
            )

    def reload_tags(self, keep_selection: bool = True) -> None:
        """Rebuild the sector list, pinning the headline sectors to the top.

        Gamma exposes ~6,000 tags, most of them attached to a single event, so
        the ones worth scoping a run to are pinned above a separator rather than
        left to be found by scrolling.
        """
        selected = set(self.selected_tag_ids()) if keep_selection else set()
        if not selected:
            selected = set(self.settings.selected_tag_ids or [])
        try:
            tags = self.db.query(
                """
                SELECT t.tag_id, t.label, t.slug,
                       (SELECT count(*) FROM market_tags mt WHERE mt.tag_id = t.tag_id) AS n
                FROM tags t
                WHERE coalesce(t.label, '') <> ''
                ORDER BY n DESC, t.label
                """
            )
        except Exception:  # noqa: BLE001 - an empty DB is fine
            tags = pd.DataFrame()

        self.tag_list.blockSignals(True)
        self.tag_list.clear()

        if tags.empty:
            self.tag_hint.setText(
                "Sector list is still loading from Polymarket. "
                "Fetching now would sweep every sector."
            )
            self.tag_list.blockSignals(False)
            self._update_tag_count()
            return

        priority = {slug: i for i, slug in enumerate(CURATED_SECTOR_SLUGS)}
        tags["_rank"] = tags.slug.map(priority)
        pinned = tags[tags._rank.notna()].sort_values("_rank")
        rest = tags[tags._rank.isna()]

        def add(row: Any) -> None:
            count = int(row.n)
            label = f"{row.label}" + (f"  ({count:,})" if count else "")
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if int(row.tag_id) in selected else Qt.Unchecked
            )
            item.setData(Qt.UserRole, int(row.tag_id))
            item.setData(Qt.UserRole + 1, f"{row.label} {row.slug or ''}".lower())
            self.tag_list.addItem(item)

        for row in pinned.itertuples():
            add(row)
        if not pinned.empty and not rest.empty:
            sep = QListWidgetItem("──  all other sectors  ──")
            sep.setFlags(Qt.NoItemFlags)
            sep.setForeground(QColor(FG_DIM))
            sep.setData(Qt.UserRole + 1, "")
            self.tag_list.addItem(sep)
        for row in rest.itertuples():
            add(row)

        self.tag_list.blockSignals(False)
        self.tag_hint.setText(
            f"{len(tags):,} sectors loaded. No selection = every sector."
        )
        self._update_tag_count()
        self._filter_tags(self.tag_search.text())

    @staticmethod
    def _is_selectable(item: QListWidgetItem) -> bool:
        return bool(item.flags() & Qt.ItemIsUserCheckable)

    def _filter_tags(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for i in range(self.tag_list.count()):
            item = self.tag_list.item(i)
            if not self._is_selectable(item):
                item.setHidden(bool(needle))  # hide the separator while searching
                continue
            label = str(item.data(Qt.UserRole + 1) or "")
            item.setHidden(
                bool(needle) and needle not in label
                and item.checkState() != Qt.Checked
            )

    def _select_matching(self) -> None:
        """Tick everything currently visible under the search term."""
        needle = self.tag_search.text().strip().lower()
        if not needle:
            return
        self.tag_list.blockSignals(True)
        n = 0
        for i in range(self.tag_list.count()):
            item = self.tag_list.item(i)
            if not self._is_selectable(item):
                continue
            if needle in str(item.data(Qt.UserRole + 1) or ""):
                item.setCheckState(Qt.Checked)
                n += 1
        self.tag_list.blockSignals(False)
        self._update_tag_count()

    def _clear_tags(self) -> None:
        self.tag_list.blockSignals(True)
        for i in range(self.tag_list.count()):
            item = self.tag_list.item(i)
            if self._is_selectable(item):
                item.setCheckState(Qt.Unchecked)
        self.tag_list.blockSignals(False)
        self._update_tag_count()

    def _update_tag_count(self, *_a: Any) -> None:
        names = self.selected_tag_labels()
        if not names:
            self.tag_count.setText(
                "<b>none selected — every sector will be fetched</b>"
            )
            return
        shown = ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
        self.tag_count.setText(f"<b>{len(names)} selected:</b> {shown}")

    def selected_tag_ids(self) -> list[int]:
        out = []
        for i in range(self.tag_list.count()):
            item = self.tag_list.item(i)
            if self._is_selectable(item) and item.checkState() == Qt.Checked:
                out.append(int(item.data(Qt.UserRole)))
        return out

    def selected_tag_labels(self) -> list[str]:
        out = []
        for i in range(self.tag_list.count()):
            item = self.tag_list.item(i)
            if self._is_selectable(item) and item.checkState() == Qt.Checked:
                out.append(item.text().split("  (")[0])
        return out

    # -- market picker ------------------------------------------------------
    def _search_markets(self) -> None:
        term = self.market_search.text().strip()
        if term:
            self.market_results.clear()
            self.market_results.addItem("Searching…")
            self.market_search_requested.emit(term)

    def show_market_results(self, markets: pd.DataFrame) -> None:
        self.market_results.clear()
        if markets is None or markets.empty:
            self.market_results.addItem("No markets found.")
            return
        for row in markets.itertuples():
            item = QListWidgetItem(f"{row.question[:80]}  ·  ${row.volume:,.0f}")
            item.setData(Qt.UserRole, row.condition_id)
            item.setData(Qt.UserRole + 1, row.question)
            self.market_results.addItem(item)

    def _pick_local_markets(self) -> None:
        """Offer markets already in the database, newest and largest first."""
        try:
            df = self.db.query(
                "SELECT condition_id, question, volume FROM markets "
                "ORDER BY volume DESC LIMIT 500"
            )
        except Exception:  # noqa: BLE001
            df = pd.DataFrame()
        self.show_market_results(df)

    def _add_market(self, item: QListWidgetItem) -> None:
        cid = item.data(Qt.UserRole)
        if not cid or cid in self._selected_markets:
            return
        question = item.data(Qt.UserRole + 1) or cid
        self._selected_markets[cid] = question
        entry = QListWidgetItem(str(question)[:80])
        entry.setData(Qt.UserRole, cid)
        self.market_selected.addItem(entry)

    def _remove_market(self, item: QListWidgetItem) -> None:
        self._selected_markets.pop(item.data(Qt.UserRole), None)
        self.market_selected.takeItem(self.market_selected.row(item))

    # -- outputs ------------------------------------------------------------
    def filters(self) -> AnalysisFilters:
        days = PRESETS[self.preset.currentIndex()][1]
        if days is None:
            start_ts, end_ts = 0, 0
        else:
            start_ts = _utc_ts(self.date_from.date())
            end_ts = _utc_ts(self.date_to.date(), end_of_day=True)
        picked = list(self._selected_markets)
        seed_mode = self.market_mode.currentData() == "seed"
        return AnalysisFilters(
            start_ts=start_ts,
            end_ts=end_ts,
            tag_ids=self.selected_tag_ids(),
            condition_ids=[] if seed_mode else picked,
            seed_condition_ids=picked if seed_mode else [],
            min_market_volume=float(self.min_market_volume.value()),
            resolved_only=self.resolved_only.isChecked(),
        )

    def cluster_params(self) -> ClusterParams:
        return ClusterParams(
            min_shared_bets=self.min_shared.value(),
            similarity_threshold=float(self.similarity.value()),
            louvain_resolution=float(self.resolution.value()),
            min_cluster_size=self.min_cluster_size.value(),
            max_bet_user_frac=float(self.max_bet_frac.value()),
            use_idf=self.use_idf.isChecked(),
            size_weighting=self.size_weighting.currentText(),
            method=self.method.currentText(),
            timing_bonus=self.timing_bonus.isChecked(),
            timing_window_hours=float(self.timing_window.value()),
            core_pct=float(self.core_pct.value()),
        )

    def apply_to_settings(self) -> AppSettings:
        s = self.settings
        s.min_market_volume = float(self.min_market_volume.value())
        s.max_markets_per_fetch = int(self.max_markets.value())
        s.selected_tag_ids = self.selected_tag_ids()
        s.min_user_usd = float(self.min_user_usd.value())
        s.min_user_bets = self.min_user_bets.value()
        s.max_user_bets = self.max_user_bets.value()
        s.min_position_usd = float(self.min_position_usd.value())
        s.min_entry_price = float(self.min_entry_price.value())
        s.max_entry_price = float(self.max_entry_price.value())
        s.min_shared_bets = self.min_shared.value()
        s.similarity_threshold = float(self.similarity.value())
        s.louvain_resolution = float(self.resolution.value())
        s.min_cluster_size = self.min_cluster_size.value()
        s.max_bet_user_frac = float(self.max_bet_frac.value())
        s.timing_window_hours = float(self.timing_window.value())
        s.unanimity_core_pct = float(self.core_pct.value())
        for attr, spin in self.weight_widgets.items():
            setattr(s, attr, float(spin.value()))
        return s

    def weights(self) -> dict[str, float]:
        return {a: float(w.value()) for a, w in self.weight_widgets.items()}

    def _emit_weights(self) -> None:
        self.weights_changed.emit(self.weights())

    def set_busy(self, busy: bool) -> None:
        self.btn_ingest.setEnabled(not busy)
        self.btn_analyse.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
