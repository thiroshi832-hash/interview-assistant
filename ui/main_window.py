"""Main window: holds the setup view, then swaps to the interview view."""
from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from config import Config
from ui.setup_view import SetupView
from ui.interview_view import InterviewView
from ui.style import STYLE


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, mode: str = ""):
        super().__init__()
        self.cfg = cfg
        self.mode = mode
        self.setWindowTitle("AetherStack Interview Assistant")
        self.setStyleSheet(STYLE)
        # Let the window shrink well below the content's natural size so it can
        # be made short AND narrow (e.g. a slim strip beside the interview
        # window). The explicit minimum overrides the layout's own minimum
        # (~1113x925 from the button row / panels); content clips or scrolls
        # rather than pinning a size the user can't drag past.
        self.setMinimumSize(320, 200)
        self._restore_geometry()

        self.stack = QStackedWidget()
        self.setup_view = SetupView(cfg, mode)
        self.interview_view = InterviewView()
        # Restore persisted UI preferences
        self.interview_view.set_font_size(cfg.answer_font_size)
        self.interview_view.set_transcript_visible(not cfg.transcript_collapsed)
        # Persist UI changes the user makes
        self._wire_persistence()

        self.stack.addWidget(self.setup_view)
        self.stack.addWidget(self.interview_view)
        self.setCentralWidget(self.stack)

    def _wire_persistence(self) -> None:
        # Save preferences whenever the user changes them
        iv = self.interview_view
        original_bump = iv._bump_font

        def bump_and_save(delta: int):
            original_bump(delta)
            self.cfg.answer_font_size = iv.font_size
            self.cfg.save()
        iv._bump_font = bump_and_save  # type: ignore[method-assign]

        original_toggle = iv._toggle_transcript

        def toggle_and_save():
            original_toggle()
            self.cfg.transcript_collapsed = not iv.transcript_visible
            self.cfg.save()
        iv._toggle_transcript = toggle_and_save  # type: ignore[method-assign]

    def show_interview(self):
        self.stack.setCurrentWidget(self.interview_view)

    def show_setup(self):
        self.stack.setCurrentWidget(self.setup_view)

    # ── window geometry persistence ──────────────────────────────────────────
    def _restore_geometry(self) -> None:
        """Restore the saved size, clamped to the current screen so the window
        (and its bottom resize grip) always fits — then center it."""
        w = int(self.cfg.window_width or 1280)
        h = int(self.cfg.window_height or 760)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            w = min(w, avail.width())
            h = min(h, avail.height())
        self.resize(max(w, 320), max(h, 200))
        if screen is not None:
            fg = self.frameGeometry()
            fg.moveCenter(avail.center())
            self.move(fg.topLeft())

    def closeEvent(self, event) -> None:
        # Persist the height/width the user settled on for next launch.
        self.cfg.window_width = self.width()
        self.cfg.window_height = self.height()
        try:
            self.cfg.save()
        except Exception:
            pass
        super().closeEvent(event)
