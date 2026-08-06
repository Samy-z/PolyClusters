"""Application entry point: ``python -m polyclusters``."""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

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
    try:
        db = Database()
    except Exception as exc:  # noqa: BLE001
        # Launched under pythonw there is no console, so an unhandled failure
        # here looks like the shortcut simply doing nothing. DuckDB takes an
        # exclusive lock, so a second instance is the usual cause.
        already_open = "another process" in str(exc).lower() or "being used" in str(exc).lower()
        QMessageBox.critical(
            None,
            f"{APP_NAME} cannot start",
            (
                "PolyClusters is already running.\n\nThe database is held open by "
                "the other window — switch to it, or close it and try again."
                if already_open else
                f"Could not open the database.\n\n{exc}"
            ),
        )
        return 1

    window = MainWindow(db, settings)
    # Maximised on the primary screen. The window's restored geometry is already
    # sized to that monitor, so un-maximising lands somewhere sensible too.
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
