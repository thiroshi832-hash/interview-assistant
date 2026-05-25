"""
Dialog shown right after the mode picker when the user chose helper-network.

Asks for the sender's host and port. Validated by attempting a quick
WebSocket handshake (3-second timeout). Persists the last-good values
in cfg for next launch.
"""
from __future__ import annotations

import asyncio
import threading

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from config import Config
from ui.style import STYLE


class _Probe(QObject):
    done = Signal(bool, str)   # ok, message


def _probe_async(host: str, port: int, sig: _Probe) -> None:
    async def go():
        import websockets  # type: ignore
        url = f"ws://{host}:{port}"
        try:
            async with websockets.connect(url, open_timeout=3, close_timeout=2):
                sig.done.emit(True, f"Connected to {host}:{port}.")
        except Exception as e:
            sig.done.emit(False, f"Could not reach {host}:{port} — {e}")
    try:
        asyncio.run(go())
    except Exception as e:
        sig.done.emit(False, f"Probe failed: {e}")


class NetworkConnectDialog(QDialog):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("Connect to AetherStack Sender")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(480)
        self.host: str = ""
        self.port: int = 8765
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(12)

        title = QLabel("Connect to the interview computer")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        outer.addWidget(title)

        hint = QLabel(
            "Open AetherStack Sender on the interview computer and click Start.\n"
            "Type its IP address and port below — the Sender shows both."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(8)
        self.txt_host = QLineEdit(self.cfg.network_host or "")
        self.txt_host.setPlaceholderText("e.g. 192.168.1.42")
        self.txt_port = QLineEdit(str(self.cfg.network_port or 8765))
        self.txt_port.setMaximumWidth(120)
        form.addRow(QLabel("Sender IP / host:"), self.txt_host)
        form.addRow(QLabel("Port:"), self.txt_port)
        outer.addLayout(form)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("hint")
        self.lbl_status.setWordWrap(True)
        outer.addWidget(self.lbl_status)

        outer.addStretch(1)

        row = QHBoxLayout()
        self.btn_test = QPushButton("Test connection")
        self.btn_test.clicked.connect(self._test)
        row.addWidget(self.btn_test)
        row.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        self.btn_ok = QPushButton("Connect")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.clicked.connect(self._accept)
        row.addWidget(self.btn_ok)
        outer.addLayout(row)

        self._probe = _Probe()
        self._probe.done.connect(self._on_probe_done, Qt.ConnectionType.QueuedConnection)

    def _parse(self) -> tuple[str, int] | None:
        host = self.txt_host.text().strip()
        if not host:
            self.lbl_status.setText("Enter the sender's IP address.")
            return None
        try:
            port = int(self.txt_port.text().strip())
            if port < 1 or port > 65535:
                raise ValueError()
        except Exception:
            self.lbl_status.setText("Port must be between 1 and 65535.")
            return None
        return host, port

    def _test(self) -> None:
        parsed = self._parse()
        if parsed is None:
            return
        host, port = parsed
        self.lbl_status.setText(f"Probing {host}:{port}…")
        self.btn_test.setEnabled(False)
        self.btn_ok.setEnabled(False)
        threading.Thread(
            target=_probe_async, args=(host, port, self._probe), daemon=True
        ).start()

    def _on_probe_done(self, ok: bool, msg: str) -> None:
        self.btn_test.setEnabled(True)
        self.btn_ok.setEnabled(True)
        self.lbl_status.setText(msg)

    def _accept(self) -> None:
        parsed = self._parse()
        if parsed is None:
            return
        self.host, self.port = parsed
        # Persist for next launch
        self.cfg.network_host = self.host
        self.cfg.network_port = self.port
        try:
            self.cfg.save()
        except Exception:
            pass
        self.accept()
