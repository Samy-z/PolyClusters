"""Taskbar tray icon: what is running, and a way to stop it.

A fetch can run for several minutes across background threads. Without this
there is nothing to look at while it does, and nothing to press if it misbehaves
- the window is the only evidence the app exists at all.

The icon lives in the notification area (Windows tucks it into the "Show hidden
icons" overflow unless promoted), and is present for exactly as long as the
process is.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayController(QObject):
    """Wraps the tray icon, its menu and its status text."""

    show_requested = Signal()
    terminate_requested = Signal()
    quit_requested = Signal()

    def __init__(self, icon: QIcon, parent: QObject | None = None):
        super().__init__(parent)
        self.tray = QSystemTrayIcon(icon, parent)
        self.tray.setToolTip("PolyClusters — idle")

        menu = QMenu()
        header = QAction("PolyClusters", menu)
        header.setEnabled(False)
        menu.addAction(header)

        self.status_action = QAction("Idle", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()

        show = QAction("Show window", menu)
        show.triggered.connect(self.show_requested.emit)
        menu.addAction(show)

        self.terminate_action = QAction("Terminate all tasks", menu)
        self.terminate_action.setToolTip(
            "Stop every running fetch, analysis or watchlist refresh."
        )
        self.terminate_action.setEnabled(False)
        self.terminate_action.triggered.connect(self.terminate_requested.emit)
        menu.addAction(self.terminate_action)
        menu.addSeparator()

        quit_action = QAction("Quit PolyClusters", menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)

        # The menu is parented to nothing by default and would be garbage
        # collected the moment this constructor returns, taking the actions
        # with it and leaving an icon whose right-click does nothing.
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)

    def _activated(self, reason: Any) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_requested.emit()

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    def set_tasks(self, names: list[str]) -> None:
        """Reflect what is running right now."""
        if names:
            label = names[0] if len(names) == 1 else f"{len(names)} tasks running"
            self.status_action.setText(f"Running: {label}")
            self.tray.setToolTip("PolyClusters — " + label)
            self.terminate_action.setEnabled(True)
        else:
            self.status_action.setText("Idle")
            self.tray.setToolTip("PolyClusters — idle")
            self.terminate_action.setEnabled(False)

    def notify(self, title: str, message: str) -> None:
        if QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, message, self.tray.icon(), 4000)
