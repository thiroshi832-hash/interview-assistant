"""Main window: holds the setup view, then swaps to the interview view."""
from __future__ import annotations

from PySide6.QtWidgets import QMainWindow, QStackedWidget

from config import Config
from ui.setup_view import SetupView
from ui.interview_view import InterviewView
from ui.style import STYLE


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("AetherStack Interview Assistant")
        self.setStyleSheet(STYLE)
        self.resize(1280, 760)

        self.stack = QStackedWidget()
        self.setup_view = SetupView(cfg)
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
