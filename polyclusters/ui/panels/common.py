"""Small shared presentation widgets."""

from __future__ import annotations

from typing import Iterable

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..models import fmt_value
from ..theme import BAD, FG, FG_DIM, GOOD


class StatCard(QFrame):
    """One headline number with a caption underneath."""

    def __init__(self, label: str, tip: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumWidth(112)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(1)

        self.value = QLabel("—")
        self.value.setObjectName("metricValue")
        self.caption = QLabel(label)
        self.caption.setObjectName("metricLabel")
        lay.addWidget(self.value)
        lay.addWidget(self.caption)
        if tip:
            self.setToolTip(tip)

    def set_value(self, value: object, fmt: str = "auto", signed: bool = False) -> None:
        self.value.setText(fmt_value(value, fmt))
        colour = FG
        if signed and isinstance(value, (int, float, np.integer, np.floating)):
            if np.isfinite(value):
                colour = GOOD if value > 0 else (BAD if value < 0 else FG_DIM)
        self.value.setStyleSheet(f"color: {colour};")


class StatRow(QWidget):
    """A horizontal strip of StatCards addressed by key."""

    def __init__(self, specs: Iterable[tuple[str, str, str]], parent: QWidget | None = None):
        """specs: (key, label, tooltip)"""
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)
        self.cards: dict[str, StatCard] = {}
        for key, label, tip in specs:
            card = StatCard(label, tip)
            self.cards[key] = card
            lay.addWidget(card)
        lay.addStretch(1)

    def set(self, key: str, value: object, fmt: str = "auto", signed: bool = False) -> None:
        card = self.cards.get(key)
        if card is not None:
            card.set_value(value, fmt, signed)

    def clear(self) -> None:
        for card in self.cards.values():
            card.set_value(None)


class SectionLabel(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("metricLabel")
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
