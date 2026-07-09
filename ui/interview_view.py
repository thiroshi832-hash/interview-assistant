"""Live interview view: collapsible transcript on the left, answer panel on the right."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTextEdit, QVBoxLayout, QWidget,
)


_MIN_FONT = 11
_MAX_FONT = 32
_DEFAULT_FONT = 16


class _AnswerEdit(QTextEdit):
    """
    QTextEdit with gentler mouse-wheel scrolling. Qt's default is 3 text lines
    per wheel notch, and a "line" grows with the answer font — at 24-32px the
    view leaps ~70-115px per notch, which reads as jumpy/too fast. Scroll
    exactly ONE line per notch instead, and use the OS-provided pixel deltas
    directly for touchpads (smooth by nature).
    """

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Preserve Ctrl+wheel (font zoom on read-only QTextEdit).
            super().wheelEvent(event)
            return
        sb = self.verticalScrollBar()
        pixels = event.pixelDelta().y()
        if not pixels:
            notches = event.angleDelta().y() / 120.0
            pixels = int(notches * self.fontMetrics().lineSpacing())
        if pixels:
            sb.setValue(sb.value() - pixels)
            event.accept()
        else:
            super().wheelEvent(event)


class InterviewView(QWidget):
    answer_now = Signal()
    style_request = Signal(str)
    deep_request = Signal()
    stop_request = Signal()                # legacy — direct stop without evaluation
    end_interview_request = Signal()       # NEW — runs evaluation, then stops
    swap_speakers = Signal()

    def __init__(self):
        super().__init__()
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Collapse toggle (thin button on the left edge) ───────────────────
        self._transcript_visible = True   # explicit state — don't trust isVisible() pre-show
        self.btn_collapse = QPushButton("◀")
        self.btn_collapse.setFixedWidth(22)
        self.btn_collapse.setToolTip("Hide / show the transcript panel")
        self.btn_collapse.clicked.connect(self._toggle_transcript)
        root.addWidget(self.btn_collapse)

        # ── Splitter holds both panels so the divider is draggable too ───────
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.splitter, stretch=1)

        # ── Left: transcript ("script board") ────────────────────────────────
        self.transcript_panel = QWidget()
        left = QVBoxLayout(self.transcript_panel)
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(QLabel("Live transcript"))
        # QTextEdit (rich text) so partials can be rendered muted/italic
        # and replaced in-place when STT updates them.
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Waiting for audio…")
        left.addWidget(self.transcript)
        self.splitter.addWidget(self.transcript_panel)

        # Backing model for the transcript — list of (speaker, text, is_final)
        # plus an index of the pending (non-final) line per speaker.
        self._transcript_lines: list[tuple[str, str, bool]] = []
        self._pending_idx: dict[str, int] = {}

        # ── Right: answer + health + controls ────────────────────────────────
        right_panel = QWidget()
        right = QVBoxLayout(right_panel)
        right.setContentsMargins(0, 0, 0, 0)

        # mic-level meter — pulses with input volume. Flat=no audio coming in.
        mic_row = QHBoxLayout()
        mic_row.addWidget(QLabel("Mic:"))
        self.mic_bar = QProgressBar()
        self.mic_bar.setRange(0, 100)
        self.mic_bar.setValue(0)
        self.mic_bar.setTextVisible(False)
        self.mic_bar.setFixedHeight(10)
        mic_row.addWidget(self.mic_bar, stretch=1)
        self.mic_label = QLabel("waiting…")
        self.mic_label.setMinimumWidth(110)
        self.mic_label.setObjectName("hint")
        mic_row.addWidget(self.mic_label)
        right.addLayout(mic_row)
        self._apply_mic_color(0)

        # health bar at the top
        health_row = QHBoxLayout()
        health_row.addWidget(QLabel("Interview state:"))
        self.health_bar = QProgressBar()
        self.health_bar.setRange(0, 100)
        self.health_bar.setValue(70)
        self.health_bar.setTextVisible(False)
        self.health_bar.setFixedHeight(14)
        health_row.addWidget(self.health_bar, stretch=1)
        self.health_label = QLabel("waiting")
        self.health_label.setMinimumWidth(110)
        health_row.addWidget(self.health_label)
        right.addLayout(health_row)
        self._apply_health_color(70)
        self.health_note = QLabel("Waiting for the interview to begin.")
        self.health_note.setObjectName("hint")
        self.health_note.setWordWrap(True)
        right.addWidget(self.health_note)

        # font controls + Suggested answer label
        font_row = QHBoxLayout()
        font_row.addWidget(QLabel("Suggested answer"))
        font_row.addStretch(1)
        self.font_size = _DEFAULT_FONT
        self.btn_font_smaller = QPushButton("A−")
        self.btn_font_smaller.setFixedWidth(40)
        self.btn_font_smaller.setToolTip("Smaller answer font (Ctrl+−)")
        self.btn_font_smaller.clicked.connect(lambda: self._bump_font(-1))
        self.btn_font_bigger = QPushButton("A+")
        self.btn_font_bigger.setFixedWidth(40)
        self.btn_font_bigger.setToolTip("Larger answer font (Ctrl++)")
        self.btn_font_bigger.clicked.connect(lambda: self._bump_font(+1))
        self.font_size_label = QLabel(f"{_DEFAULT_FONT}px")
        self.font_size_label.setObjectName("hint")
        self.font_size_label.setMinimumWidth(40)
        self.font_size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font_row.addWidget(self.btn_font_smaller)
        font_row.addWidget(self.font_size_label)
        font_row.addWidget(self.btn_font_bigger)
        right.addLayout(font_row)

        # answer text
        self.answer = _AnswerEdit()
        self.answer.setReadOnly(True)
        self.answer.setObjectName("answer")
        right.addWidget(self.answer, stretch=1)
        self._apply_answer_font()
        # True while the most recently inserted answer char is a newline —
        # lets append_answer_chunk collapse "\n\n" paragraph gaps across
        # chunk boundaries (LLMs stream blank lines between paragraphs, which
        # doubles the apparent line spacing).
        self._answer_at_line_start = True

        # action buttons
        btn_row = QHBoxLayout()
        self.btn_regen = QPushButton("Regenerate")
        self.btn_shorter = QPushButton("Shorter")
        self.btn_tech = QPushButton("More technical")
        self.btn_deep = QPushButton("Deeper")
        self.btn_swap = QPushButton("Swap speakers")
        self.btn_end = QPushButton("End interview")
        self.btn_end.setObjectName("danger")
        self.btn_end.setToolTip("Stop listening AND analyze the full transcript for a hireability verdict.")

        self.btn_regen.clicked.connect(self.answer_now.emit)
        self.btn_shorter.clicked.connect(lambda: self.style_request.emit("shorter — 2 sentences max"))
        self.btn_tech.clicked.connect(lambda: self.style_request.emit("more technical, include specifics"))
        self.btn_deep.clicked.connect(self.deep_request.emit)
        self.btn_swap.clicked.connect(self.swap_speakers.emit)
        self.btn_end.clicked.connect(self.end_interview_request.emit)

        for b in (self.btn_regen, self.btn_shorter, self.btn_tech, self.btn_deep, self.btn_swap, self.btn_end):
            btn_row.addWidget(b)
        right.addLayout(btn_row)

        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([520, 760])

        # ── Hotkeys ──────────────────────────────────────────────────────────
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self.answer_now.emit)
        QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self._bump_font(+1))
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self._bump_font(+1))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self._bump_font(-1))
        QShortcut(QKeySequence("Ctrl+\\"), self, activated=self._toggle_transcript)

    # ── live updates from controller (main thread) ──────────────────────────
    @Slot(str, str, bool, bool)
    def update_turn(self, speaker: str, text: str, is_final: bool, replaces_pending: bool):
        """
        Add or replace a transcript line.
        - replaces_pending=True : replace the speaker's most-recent in-progress
          line in place (partial → newer partial, or partial → final).
        - replaces_pending=False: append a brand-new line.
        """
        if replaces_pending and speaker in self._pending_idx:
            idx = self._pending_idx[speaker]
            if 0 <= idx < len(self._transcript_lines):
                self._transcript_lines[idx] = (speaker, text, is_final)
            else:
                self._transcript_lines.append((speaker, text, is_final))
                idx = len(self._transcript_lines) - 1
                self._pending_idx[speaker] = idx
        else:
            self._transcript_lines.append((speaker, text, is_final))
            idx = len(self._transcript_lines) - 1
            if not is_final:
                self._pending_idx[speaker] = idx

        if is_final and speaker in self._pending_idx:
            del self._pending_idx[speaker]

        self._rerender_transcript()

    # Legacy alias — older callers still emit `new_turn(speaker, text)`.
    @Slot(str, str)
    def append_turn(self, speaker: str, text: str):
        self.update_turn(speaker, text, True, False)

    @Slot()
    def clear_answer(self):
        self.answer.clear()
        self._answer_at_line_start = True
        # New answer just started → reset the scroll to the very top so the
        # user always reads the beginning first, regardless of how long the
        # previous answer was.
        sb = self.answer.verticalScrollBar()
        sb.setValue(sb.minimum())

    @Slot(str)
    def append_answer_chunk(self, text: str):
        # Collapse runs of newlines to a single newline (state carries across
        # chunk boundaries). LLM answers separate paragraphs with "\n\n"; the
        # resulting blank line doubles the visual line spacing and makes the
        # short answers look sparse. Leading newlines at the top are dropped.
        chars = []
        for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
            if ch == "\n":
                if not self._answer_at_line_start:
                    chars.append(ch)
                    self._answer_at_line_start = True
            else:
                chars.append(ch)
                self._answer_at_line_start = False
        text = "".join(chars)
        if not text:
            return

        # Insert at the end of the document WITHOUT touching the visible
        # viewport. We deliberately don't call `setTextCursor(cur)` here —
        # that would force Qt to scroll the view so the cursor is visible
        # (i.e. jump to the bottom), which is exactly what the user does
        # NOT want during a long streaming answer. Instead we use a
        # detached cursor for the insert, and clamp the scroll bar back
        # to the top each chunk so the document growing doesn't drag the
        # viewport down either.
        cur = QTextCursor(self.answer.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(text)
        sb = self.answer.verticalScrollBar()
        sb.setValue(sb.minimum())

    @Slot(str)
    def set_status(self, msg: str):
        # Treat status as a final system line (won't replace anything).
        self._transcript_lines.append(("_status", msg, True))
        self._rerender_transcript()

    def _rerender_transcript(self) -> None:
        # Cheap enough — typical interviews have <200 lines.
        from html import escape
        parts: list[str] = []
        for speaker, text, is_final in self._transcript_lines:
            if speaker == "_status":
                parts.append(
                    f'<p style="color:#94e2d5; margin:6px 0;">— {escape(text)} —</p>'
                )
                continue
            label = "INTERVIEWER" if speaker == "interviewer" else "CANDIDATE"
            label_color = "#89b4fa" if speaker == "interviewer" else "#a6e3a1"
            text_color = "#cdd6f4" if is_final else "#6c7086"
            style = "" if is_final else " font-style:italic;"
            parts.append(
                f'<p style="color:{text_color};{style} margin:0 0 6px 0;">'
                f'<b style="color:{label_color};">[{label}]</b> {escape(text)}'
                f'</p>'
            )
        self.transcript.setHtml("".join(parts))
        sb = self.transcript.verticalScrollBar()
        sb.setValue(sb.maximum())

    @Slot(int, str, str)
    def set_health(self, score: int, label: str, note: str):
        self.health_bar.setValue(int(score))
        self.health_label.setText(f"{label}  ·  {score}/100")
        self.health_note.setText(note)
        self._apply_health_color(score)

    @Slot(int)
    def set_mic_level(self, level: int):
        level = max(0, min(100, int(level)))
        self.mic_bar.setValue(level)
        if level < 5:
            self.mic_label.setText("silent")
        elif level < 25:
            self.mic_label.setText("quiet")
        elif level < 60:
            self.mic_label.setText("normal")
        else:
            self.mic_label.setText("loud")
        self._apply_mic_color(level)

    # ── font controls ────────────────────────────────────────────────────────
    def set_font_size(self, size: int) -> None:
        self.font_size = max(_MIN_FONT, min(_MAX_FONT, int(size)))
        self._apply_answer_font()

    def _bump_font(self, delta: int) -> None:
        self.set_font_size(self.font_size + delta)

    def _apply_answer_font(self) -> None:
        # (No line-height here — Qt stylesheets don't support it; it was
        # silently ignored. Line spacing is the font's natural spacing.)
        self.answer.setStyleSheet(
            f"QTextEdit#answer {{ font-size: {self.font_size}px; }}"
        )
        self.font_size_label.setText(f"{self.font_size}px")

    # ── collapse / expand ────────────────────────────────────────────────────
    @property
    def transcript_visible(self) -> bool:
        return self._transcript_visible

    def set_transcript_visible(self, visible: bool) -> None:
        self._transcript_visible = bool(visible)
        self.transcript_panel.setVisible(self._transcript_visible)
        self.btn_collapse.setText("◀" if self._transcript_visible else "▶")
        self.btn_collapse.setToolTip(
            "Hide the transcript panel" if self._transcript_visible else "Show the transcript panel"
        )

    def _toggle_transcript(self) -> None:
        self.set_transcript_visible(not self._transcript_visible)

    # ── mic level bar colour ────────────────────────────────────────────────
    def _apply_mic_color(self, level: int) -> None:
        # Red for silent (no audio = problem). Yellow for quiet (might miss
        # words). Green for normal/loud (good).
        if level < 5:
            chunk = "#f38ba8"      # red — flat, no audio
            txt = "#f38ba8"
        elif level < 25:
            chunk = "#f9e2af"      # yellow — quiet, may be missed
            txt = "#f9e2af"
        elif level < 80:
            chunk = "#a6e3a1"      # green — normal
            txt = "#a6e3a1"
        else:
            chunk = "#94e2d5"      # teal — loud
            txt = "#94e2d5"
        self.mic_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #45475a; border-radius: 4px; background: #313244; } "
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 3px; }}"
        )
        self.mic_label.setStyleSheet(f"color: {txt}; font-size: 11px;")

    # ── health bar colour ────────────────────────────────────────────────────
    def _apply_health_color(self, score: int) -> None:
        if score >= 75:
            chunk = "#a6e3a1"        # green
            txt = "#a6e3a1"
        elif score >= 55:
            chunk = "#94e2d5"        # teal
            txt = "#94e2d5"
        elif score >= 35:
            chunk = "#f9e2af"        # yellow
            txt = "#f9e2af"
        else:
            chunk = "#f38ba8"        # red
            txt = "#f38ba8"
        self.health_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #45475a; border-radius: 6px; background: #313244; } "
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 5px; }}"
        )
        self.health_label.setStyleSheet(f"color: {txt}; font-weight: bold;")
