"""The collapsed form of the controls panel, and the dock's title bar.

Once the data is fetched and the analysis has run, the control panel is mostly
dead weight - it is the widest thing on screen and the tables are the part worth
looking at. Collapsing it to a labelled strip keeps it one click away without
giving it 420px of the window.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from ..theme import ACCENT, BG_ALT, BG_RAISED, BORDER, FG, FG_DIM

STRIP_WIDTH = 30


class CollapsedControlsStrip(QWidget):
    """A narrow vertical band reading "CONTROLS", clicked to reopen the panel.

    Painted by hand rather than styled: Qt has no rotated-text label, and a
    QPushButton with a stylesheet would still draw its text horizontally.
    """

    clicked = Signal()

    def __init__(self, text: str = "CONTROLS", direction: str = "right",
                 parent: QWidget | None = None):
        """``direction`` is the way the panel opens: a strip on the left edge
        opens rightward, one on the right edge opens leftward - the chevron and
        the border follow it."""
        super().__init__(parent)
        self._text = text
        self._direction = direction
        self._hover = False
        self.setFixedWidth(STRIP_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Show the controls panel")
        self.setAttribute(Qt.WA_Hover, True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(STRIP_WIDTH, 260)

    def enterEvent(self, event: Any) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(BG_ALT if self._hover else BG_RAISED))

        # Border on the side the panel opens toward.
        painter.setPen(QPen(QColor(ACCENT if self._hover else BORDER), 1))
        edge = self.width() - 1 if self._direction == "right" else 0
        painter.drawLine(edge, 0, edge, self.height())

        # A chevron at the top pointing the way the panel opens.
        painter.setPen(QPen(QColor(ACCENT if self._hover else FG_DIM), 1.6))
        cx, cy = self.width() / 2, 14
        tip = 2.5 if self._direction == "right" else -2.5
        painter.drawLine(cx - tip, cy - 3, cx + tip, cy)
        painter.drawLine(cx + tip, cy, cx - tip, cy + 3)

        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF(), 8.0))
        font.setBold(True)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 1.6)
        painter.setFont(font)
        painter.setPen(QColor(FG if self._hover else FG_DIM))

        # Rotate about the widget centre and draw into a swapped-axis rect, so
        # the text reads bottom-to-top down the strip.
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        painter.rotate(-90)
        painter.drawText(
            QRect(-self.height() // 2, -self.width() // 2, self.height(), self.width()),
            Qt.AlignCenter,
            self._text,
        )
        painter.end()


class DockTitleBar(QWidget):
    """Title strip for the controls dock, carrying the collapse button."""

    collapse_requested = Signal()

    def __init__(self, title: str = "Controls", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("dockTitle")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 4, 4)
        layout.setSpacing(6)

        label = QLabel(title)
        label.setObjectName("dockTitleText")
        layout.addWidget(label)
        layout.addStretch(1)

        button = QPushButton("‹‹")
        button.setObjectName("dockCollapse")
        button.setToolTip("Collapse the controls panel  (Ctrl+B)")
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedSize(26, 20)
        button.clicked.connect(self.collapse_requested.emit)
        layout.addWidget(button)
        self.button = button
