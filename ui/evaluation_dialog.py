"""
End-of-interview evaluation dialog.

Phase 1 — shows a spinner + "Analyzing..." while the LLM scores the transcript
         on a background thread.
Phase 2 — renders the verdict: a 0-100 score bar (color-coded), a one-line
         summary, strengths, concerns, and notable quoted moments.
"""
from __future__ import annotations

import threading
from html import escape

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from pipeline.evaluation import InterviewEvaluation
from ui.style import STYLE


class EvaluationDialog(QDialog):
    """
    Open with `exec()` after the user clicks End interview. Pass an
    `evaluator()` callable that returns InterviewEvaluation — it runs on a
    worker thread so the UI doesn't freeze during the LLM call.
    """

    _done = Signal(object)   # InterviewEvaluation

    def __init__(self, evaluator):
        super().__init__()
        self.setWindowTitle("Interview verdict")
        self.setStyleSheet(STYLE)
        self.setMinimumSize(720, 580)
        self._evaluator = evaluator
        self._evaluation: InterviewEvaluation | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(12)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        # ── Phase 1: loading ─────────────────────────────────────────────
        loading_w = QWidget()
        loading_l = QVBoxLayout(loading_w)
        loading_l.setSpacing(12)
        title1 = QLabel("Analyzing the interview…")
        title1.setStyleSheet("font-size: 16px; font-weight: bold;")
        loading_l.addWidget(title1)
        hint = QLabel(
            "Sending the full transcript to the LLM for a hireability "
            "assessment. This usually takes 5-15 seconds."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        loading_l.addWidget(hint)
        spinner = QProgressBar()
        spinner.setRange(0, 0)
        spinner.setFixedHeight(14)
        spinner.setTextVisible(False)
        loading_l.addWidget(spinner)
        loading_l.addStretch(1)
        self.stack.addWidget(loading_w)

        # ── Phase 2: verdict ─────────────────────────────────────────────
        verdict_w = QWidget()
        verdict_l = QVBoxLayout(verdict_w)
        verdict_l.setSpacing(10)

        score_row = QHBoxLayout()
        score_row.addWidget(QLabel("Hireability:"))
        self.score_bar = QProgressBar()
        self.score_bar.setRange(0, 100)
        self.score_bar.setTextVisible(False)
        self.score_bar.setFixedHeight(16)
        score_row.addWidget(self.score_bar, stretch=1)
        self.score_lbl = QLabel("")
        self.score_lbl.setMinimumWidth(180)
        self.score_lbl.setStyleSheet("font-weight: bold;")
        score_row.addWidget(self.score_lbl)
        verdict_l.addLayout(score_row)

        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setStyleSheet("font-size: 14px;")
        verdict_l.addWidget(self.summary_lbl)

        # Scrollable details
        details = QTextEdit()
        details.setReadOnly(True)
        details.setObjectName("eval_details")
        details.setStyleSheet(
            "QTextEdit#eval_details { font-size: 13px; line-height: 1.5; "
            "background: #313244; border: 1px solid #45475a; border-radius: 6px; }"
        )
        verdict_l.addWidget(details, stretch=1)
        self.details = details

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_close = QPushButton("Close — end session")
        self.btn_close.setObjectName("primary")
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        verdict_l.addLayout(btn_row)

        self.stack.addWidget(verdict_w)
        self.stack.setCurrentIndex(0)

        # Kick off background evaluation
        self._done.connect(self._on_done)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            ev = self._evaluator()
        except Exception as e:
            ev = InterviewEvaluation(
                score=0, verdict="fail", summary="",
                error=f"Evaluation failed: {e}",
            )
        self._done.emit(ev)

    @Slot(object)
    def _on_done(self, ev: InterviewEvaluation):
        self._evaluation = ev
        if ev.error:
            self._render_error(ev.error)
        else:
            self._render(ev)
        self.stack.setCurrentIndex(1)

    # ── rendering ────────────────────────────────────────────────────────
    def _render(self, ev: InterviewEvaluation):
        self.score_bar.setValue(int(ev.score))
        self._apply_score_color(ev.score)
        label = InterviewEvaluation.label_for_verdict(ev.verdict)
        self.score_lbl.setText(f"{label}  ·  {ev.score}/100")
        self.summary_lbl.setText(ev.summary or "")

        parts: list[str] = []
        if ev.strengths:
            parts.append('<h3 style="color:#a6e3a1; margin:4px 0;">Strengths</h3>')
            parts.append("<ul>")
            for s in ev.strengths:
                parts.append(f"<li>{escape(s)}</li>")
            parts.append("</ul>")
        if ev.concerns:
            parts.append('<h3 style="color:#f9e2af; margin:14px 0 4px 0;">Concerns</h3>')
            parts.append("<ul>")
            for s in ev.concerns:
                parts.append(f"<li>{escape(s)}</li>")
            parts.append("</ul>")
        if ev.specific_moments:
            parts.append('<h3 style="color:#89b4fa; margin:14px 0 4px 0;">Specific moments</h3>')
            for m in ev.specific_moments:
                quote = escape(str(m.get("quote", "")))
                comment = escape(str(m.get("comment", "")))
                parts.append(
                    f'<div style="margin:6px 0; padding:8px 12px; '
                    f'border-left: 3px solid #45475a; background: #1e1e2e;">'
                    f'<div style="color:#cdd6f4; font-style: italic;">"{quote}"</div>'
                    f'<div style="color:#a6adc8; font-size: 12px; margin-top: 4px;">{comment}</div>'
                    f"</div>"
                )
        self.details.setHtml("".join(parts) or '<p style="color:#a6adc8;">No detail available.</p>')

    def _render_error(self, msg: str):
        self.score_bar.setValue(0)
        self.score_lbl.setText("Error")
        self.score_lbl.setStyleSheet("color:#f38ba8; font-weight:bold;")
        self.summary_lbl.setText("Could not evaluate the interview.")
        self.details.setHtml(
            f'<p style="color:#f38ba8;">{escape(msg)}</p>'
            '<p style="color:#a6adc8;">The session will still end normally.</p>'
        )

    def _apply_score_color(self, score: int):
        if score >= 75:
            chunk = "#a6e3a1"
        elif score >= 55:
            chunk = "#94e2d5"
        elif score >= 35:
            chunk = "#f9e2af"
        else:
            chunk = "#f38ba8"
        self.score_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #45475a; border-radius: 6px; background: #313244; } "
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 5px; }}"
        )
        self.score_lbl.setStyleSheet(f"color: {chunk}; font-weight: bold;")
