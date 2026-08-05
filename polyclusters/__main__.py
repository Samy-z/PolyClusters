"""Application entry point: ``python -m polyclusters``."""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .config import APP_ICO, APP_NAME, AppSettings, LOGO_ICON, WINDOWS_APP_ID
from .core.db import Database
from .ui.main_window import MainWindow


def _claim_windows_taskbar_identity() -> None:
    """Give Windows our own AppUserModelID.

    Without this the taskbar groups the window under the host interpreter and
    shows the generic Python icon instead of the app's, because the process
    really is python.exe.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:  # noqa: BLE001 - cosmetic only, never fatal
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _claim_windows_taskbar_identity()

    # Qt 6 enables high-DPI pixmaps by default; no attribute needed.
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)

    icon_path = APP_ICO if APP_ICO.exists() else LOGO_ICON
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    settings = AppSettings.load()
    db = Database()

    window = MainWindow(db, settings)
    # Maximised on the primary screen. The window's restored geometry is already
    # sized to that monitor, so un-maximising lands somewhere sensible too.
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
