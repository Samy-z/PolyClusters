"""One running copy of the app, enforced before anything else happens.

DuckDB takes an exclusive lock on the database, so a second copy cannot work
anyway - it used to get as far as opening a window's worth of setup and then
fail on the store. Detecting it here means the duplicate never starts, and the
window the user already has is brought to the front instead, which is almost
always what they were trying to do.
"""

from __future__ import annotations

import getpass
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# Per-user, so two accounts on the same machine do not block each other; they
# have separate profiles and therefore separate databases.
def _socket_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - unusual environments have no login name
        user = "default"
    return f"PolyClusters-single-instance-{user}"


class SingleInstance(QObject):
    """Holds the lock for this process, and wakes the holder if someone else has it."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._name = _socket_name()
        self._server: QLocalServer | None = None
        self._on_second_launch: Any = None

    def try_acquire(self, timeout_ms: int = 300) -> bool:
        """True if we are the only instance; False if one is already running."""
        probe = QLocalSocket()
        probe.connectToServer(self._name)
        if probe.waitForConnected(timeout_ms):
            # Someone answered: ask them to come to the front, then step aside.
            probe.write(b"raise")
            probe.waitForBytesWritten(timeout_ms)
            probe.disconnectFromServer()
            return False

        # Nobody answered. A socket file can survive a crash on Unix and will
        # then refuse to be listened on, so clear it before claiming the name.
        QLocalServer.removeServer(self._name)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._handle_connection)
        if not self._server.listen(self._name):
            # Could not claim it either; let the caller start rather than
            # blocking the user out of their own app over a socket problem.
            self._server = None
        return True

    def set_activation_handler(self, handler: Any) -> None:
        self._on_second_launch = handler

    def _handle_connection(self) -> None:
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda: conn.readAll())
        conn.disconnected.connect(conn.deleteLater)
        if self._on_second_launch is not None:
            self._on_second_launch()

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self._name)
            self._server = None
