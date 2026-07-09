"""
AetherStack Sender — tiny tray-icon-only Windows app.

Runs on the interview computer. NO main window — sits in the system tray
so it can't accidentally leak onto a screen-shared display. Captures the
mic + the system-audio loopback and streams both to the helper laptop
(running AetherStack Interview Assistant in helper-network mode) over a
single WebSocket.

Streaming starts AUTOMATICALLY on launch (with the saved devices/port) — the
tray menu is only needed to pause, change settings, or quit.

Tray menu:
    ▸ Status line (port + client count)
    ▸ Stop / Start streaming   (pause without quitting)
    ▸ Settings...              (only dialog; opens on demand, streaming resumes on Save)
    ▸ Show local IP addresses
    ▸ Quit

No transcription, no LLM, no resume. The receiver does the heavy lifting.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

# Same windowed-mode stdout/stderr fix as the main app.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QSharedMemory
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout,
    QWidget,
)

# When running from source, `sender_app.py` is invoked as a script; relative
# imports break. Add the repo root to sys.path so `audio._pcm`, `paths`, and
# `ui.style` resolve identically in source and PyInstaller bundle.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from paths import icon_path
from sender.sender_streamer import SenderStreamer
from ui.style import STYLE


CONFIG_DIR = Path.home() / ".interview_assistant"
SENDER_CONFIG_PATH = CONFIG_DIR / "sender_config.json"
DEFAULT_PORT = 8765


def _load_sender_cfg() -> dict:
    try:
        if SENDER_CONFIG_PATH.exists():
            return json.loads(SENDER_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_sender_cfg(data: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SENDER_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


class _StreamerBridge(QObject):
    """Marshals streamer callbacks (worker threads) onto the Qt main thread."""
    status = Signal(str)
    client_count = Signal(int)


class SettingsDialog(QDialog):
    """The only window this app ever opens — and only on user demand."""

    def __init__(self, current: dict):
        super().__init__()
        self.setWindowTitle("AetherStack Sender — Settings")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(520)
        try:
            self.setWindowIcon(QIcon(icon_path("png")))
        except Exception:
            pass
        self.result_cfg: dict | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(12)

        title = QLabel("Audio devices and network port")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        outer.addWidget(title)

        hint = QLabel(
            "Pick the devices to stream and the port to listen on. The window\n"
            "will close after you click Save and streaming resumes automatically\n"
            "with the new settings."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(8)

        self.cmb_mic = QComboBox()
        self.cmb_loop = QComboBox()
        self.txt_port = QLineEdit(str(current.get("port", DEFAULT_PORT)))
        self.txt_port.setMaximumWidth(120)

        # Populate devices
        inputs, loopbacks = SenderStreamer.list_devices()
        self.cmb_mic.addItem("Default microphone", None)
        for d in inputs:
            self.cmb_mic.addItem(f"{d['name']} ({d['channels']}ch, {d['rate']} Hz)", d["index"])
        self.cmb_loop.addItem("Default system audio (loopback)", None)
        for d in loopbacks:
            self.cmb_loop.addItem(f"{d['name']} ({d['channels']}ch, {d['rate']} Hz)", d["index"])

        # Restore selections
        for i in range(self.cmb_mic.count()):
            if self.cmb_mic.itemData(i) == current.get("mic_index"):
                self.cmb_mic.setCurrentIndex(i); break
        for i in range(self.cmb_loop.count()):
            if self.cmb_loop.itemData(i) == current.get("loopback_index"):
                self.cmb_loop.setCurrentIndex(i); break

        form.addRow(QLabel("Mic (→ candidate):"), self.cmb_mic)
        form.addRow(QLabel("System audio (→ interviewer):"), self.cmb_loop)
        form.addRow(QLabel("Port:"), self.txt_port)
        outer.addLayout(form)

        # IP list — copy/paste handy
        ips = SenderStreamer.local_ips()
        ip_label = QLabel("  ".join(ips) if ips else "(no LAN address found)")
        ip_label.setStyleSheet("font-family: 'Consolas'; color: #a6e3a1;")
        ip_label.setWordWrap(True)
        ip_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form2 = QFormLayout()
        form2.addRow(QLabel("This computer's IP:"), ip_label)
        outer.addLayout(form2)

        outer.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        btn_save = QPushButton("Save")
        btn_save.setObjectName("primary")
        btn_save.clicked.connect(self._accept)
        row.addWidget(btn_save)
        outer.addLayout(row)

    def _accept(self) -> None:
        try:
            port = int(self.txt_port.text().strip())
            if port < 1 or port > 65535:
                raise ValueError()
        except Exception:
            QMessageBox.warning(self, "Invalid port",
                                "Port must be a number between 1 and 65535.")
            return
        self.result_cfg = {
            "port": port,
            "mic_index": self.cmb_mic.currentData(),
            "loopback_index": self.cmb_loop.currentData(),
        }
        self.accept()


class SenderTrayApp(QObject):
    """Whole sender = one tray icon + a transient settings dialog."""

    def __init__(self, qt_app: QApplication):
        super().__init__()
        self.qt_app = qt_app
        # Critical: without this, the app would quit the moment the
        # settings dialog closes (Qt considers "last window closed").
        qt_app.setQuitOnLastWindowClosed(False)

        self.cfg = _load_sender_cfg()
        self.cfg.setdefault("port", DEFAULT_PORT)
        self.cfg.setdefault("mic_index", None)
        self.cfg.setdefault("loopback_index", None)

        self._running = False
        self._client_count = 0

        # Streamer + thread bridge
        self.bridge = _StreamerBridge()
        self.bridge.status.connect(self._on_status, Qt.ConnectionType.QueuedConnection)
        self.bridge.client_count.connect(self._on_client_count, Qt.ConnectionType.QueuedConnection)
        self.streamer = SenderStreamer(
            on_status=lambda m: self.bridge.status.emit(m),
            on_client_count=lambda n: self.bridge.client_count.emit(n),
            on_level=lambda _side, _lvl: None,  # tray app has no meters
        )

        # ── Tray icon ────────────────────────────────────────────────────
        self.tray = QSystemTrayIcon()
        # A null QIcon (bad path) yields an INVISIBLE tray icon with no error —
        # so check isNull() explicitly rather than trusting a try/except.
        icon = QIcon(icon_path("png"))
        if icon.isNull():
            icon = self.qt_app.style().standardIcon(
                self.qt_app.style().StandardPixmap.SP_MediaPlay)
        self.tray.setIcon(icon)
        self.tray.setToolTip("AetherStack Sender — idle")
        self.tray.activated.connect(self._on_tray_activated)

        # Menu
        self.menu = QMenu()
        self.menu.setStyleSheet(STYLE)
        self.action_status = QAction("Idle.", self.menu)
        self.action_status.setEnabled(False)
        self.menu.addAction(self.action_status)

        self.action_clients = QAction("0 clients connected", self.menu)
        self.action_clients.setEnabled(False)
        self.menu.addAction(self.action_clients)

        self.menu.addSeparator()

        self.action_toggle = QAction("Start streaming", self.menu)
        self.action_toggle.triggered.connect(self._toggle)
        self.menu.addAction(self.action_toggle)

        self.action_settings = QAction("Settings…", self.menu)
        self.action_settings.triggered.connect(self._open_settings)
        self.menu.addAction(self.action_settings)

        self.action_show_ip = QAction("Show this computer's IP…", self.menu)
        self.action_show_ip.triggered.connect(self._show_ip)
        self.menu.addAction(self.action_show_ip)

        self.menu.addSeparator()
        action_quit = QAction("Quit", self.menu)
        action_quit.triggered.connect(self._quit)
        self.menu.addAction(action_quit)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

        # Hello bubble so the user can see where the icon lives. On Windows 11
        # new tray icons are hidden in the overflow flyout by default, so point
        # the user at the "show hidden icons" arrow — otherwise the app looks
        # like it never started.
        self.tray.showMessage(
            "AetherStack Sender is running",
            "Streaming starts automatically. The icon lives in the system "
            "tray — on Windows 11 click the ^ \"show hidden icons\" arrow "
            "near the clock, then right-click for Stop / Settings / Quit.",
            QSystemTrayIcon.MessageIcon.Information,
            6000,
        )

        # Auto-start streaming: launching this app has exactly one purpose, so
        # don't make the user hunt down a hidden tray icon to click Start.
        # Deferred one tick so the tray icon and hello bubble render before
        # start() briefly blocks on the WebSocket server coming up.
        QTimer.singleShot(200, self._autostart)

    def _autostart(self) -> None:
        if not self._running:
            self._start()

    # ── Tray interactions ────────────────────────────────────────────────
    def _on_tray_activated(self, reason) -> None:
        # Left-click toggles streaming (handy when the menu would be slow);
        # Windows uses Trigger for left, Context for right.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle()

    def _toggle(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        try:
            self.streamer.start(
                port=int(self.cfg["port"]),
                mic_idx=self.cfg["mic_index"],
                loopback_idx=self.cfg["loopback_index"],
            )
        except Exception as e:
            self.tray.showMessage(
                "AetherStack Sender — failed to start",
                str(e),
                QSystemTrayIcon.MessageIcon.Critical,
                6000,
            )
            return
        self._running = True
        self.action_toggle.setText("Stop streaming")
        self.action_settings.setEnabled(False)
        self.tray.setToolTip(f"AetherStack Sender — streaming on port {self.cfg['port']}")
        self.tray.showMessage(
            "AetherStack Sender",
            f"Streaming on port {self.cfg['port']}. "
            f"Connect from the helper laptop using one of the IPs in Settings.",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )

    def _stop(self) -> None:
        self.streamer.stop()
        self._running = False
        self.action_toggle.setText("Start streaming")
        self.action_settings.setEnabled(True)
        self.tray.setToolTip("AetherStack Sender — idle")

    def _open_settings(self) -> None:
        if self._running:
            QMessageBox.information(
                None, "AetherStack Sender",
                "Stop streaming first to change devices or port.",
            )
            return
        dlg = SettingsDialog(self.cfg)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_cfg:
            self.cfg = dlg.result_cfg
            _save_sender_cfg(self.cfg)
            # Auto-stream philosophy: resume immediately with the new
            # devices/port instead of waiting for a manual Start.
            self._start()

    def _show_ip(self) -> None:
        ips = SenderStreamer.local_ips()
        body = ("\n".join(ips) if ips
                else "(no LAN address found — check your network)")
        QMessageBox.information(
            None, "Your IP addresses",
            f"Type one of these into the helper laptop:\n\n{body}\n\n"
            f"Port: {self.cfg['port']}",
        )

    def _quit(self) -> None:
        try:
            if self._running:
                self.streamer.stop()
        except Exception:
            pass
        self.tray.hide()
        self.qt_app.quit()

    # ── Streamer callbacks (main thread via QueuedConnection) ────────────
    def _on_status(self, msg: str) -> None:
        # Truncate so the menu doesn't grow comically wide.
        short = msg if len(msg) < 80 else msg[:77] + "…"
        self.action_status.setText(short)

    def _on_client_count(self, n: int) -> None:
        self._client_count = n
        self.action_clients.setText(f"{n} client{'s' if n != 1 else ''} connected")


# ── Crash diagnostics ────────────────────────────────────────────────────
_CRASH_LOG = CONFIG_DIR / "sender_crash.log"


def _log_crash(exc: BaseException) -> None:
    import traceback, datetime
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass


def main() -> int:
    try:
        qt_app = QApplication(sys.argv)
        try:
            qt_app.setWindowIcon(QIcon(icon_path("png")))
        except Exception:
            pass

        # Tray support sanity check — some Windows installs disable it.
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.critical(
                None, "AetherStack Sender",
                "System tray is not available on this system. "
                "Enable tray icons in Windows settings and try again.",
            )
            return 1

        # Single-instance guard. The tray icon hides in Windows 11's overflow
        # flyout, so a user who can't see it tends to relaunch — piling up
        # invisible, unkillable processes. Detect an existing instance and tell
        # them where to look instead of starting another one. (Held for the
        # process lifetime via a module global so it isn't garbage-collected.)
        global _instance_lock
        _instance_lock = QSharedMemory("AetherStackSender-singleton")
        if not _instance_lock.create(1):
            QMessageBox.information(
                None, "AetherStack Sender",
                "AetherStack Sender is already running.\n\n"
                "Its icon is in the system tray — on Windows 11 click the ^ "
                "\"show hidden icons\" arrow near the clock to see it, then "
                "right-click for Start / Settings / Quit.",
            )
            return 0

        app = SenderTrayApp(qt_app)  # noqa: F841 — kept alive by Qt
        return qt_app.exec()
    except BaseException as e:
        _log_crash(e)
        try:
            QMessageBox.critical(
                None, "AetherStack Sender — startup failed",
                f"{type(e).__name__}: {e}\n\n"
                f"Full traceback saved to:\n{_CRASH_LOG}",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
