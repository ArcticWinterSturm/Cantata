"""Recover page: options, redaction level, progress and the live log console."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPushButton, QRadioButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from ..core.models import RecoveryOptions
from . import icons
from ._paths import archive_report
from .widgets import ConsoleLog, PathRow, ProgressWidget, SectionChip, rich_tip

LEVEL_HINTS = {
    "standard": "Hard secrets (API keys, OAuth tokens, JWTs, private keys, account / org ids), e-mail addresses and home "
                "paths are redacted; session ids trimmed to 8 chars, call ids to 12.",
    "paranoid": "Standard plus: every absolute path, URL, IP / MAC address and phone number stripped; ids trimmed to 4 chars. "
                "Use before sharing a report outside your machine.",
    "none": "Keeps prompts, paths and e-mails verbatim. Hard secrets (API keys, OAuth tokens, account ids) are STILL never "
            "written. For private offline use only.",
}


class RecoverPage(QWidget):
    start_requested = pyqtSignal(object)
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget(scroll)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(12)
        title = QLabel("Recover", inner)
        title.setProperty("h1", "true")
        lay.addWidget(title)

        lay.addWidget(SectionChip("Source & output"))
        self.source_row = PathRow("~/.claude", mode="dir", parent=inner, title="Select the Claude Code data directory")
        lay.addLayout(_row(_caption("Source:", inner), self.source_row))
        self.output_row = PathRow(str(archive_report("Cantata_recovery.md")), mode="file", parent=inner, default_name="Cantata_recovery.md",
                                  title="Save recovery report")
        lay.addLayout(_row(_caption("Output:", inner), self.output_row))
        self.desktop_row = PathRow("auto-detect on this machine  —  or the copied Claude Desktop app-data dir when Source is a forensic image",
                                   mode="dir", parent=inner, title="Select the Claude Desktop app-data directory")
        self.desktop_row.setToolTip(rich_tip("Desktop directory", "Leave empty to auto-detect Claude Desktop on this machine (only when Source is this "
                                                                  "machine's live Claude dir). Set it when Source is a copied image."))
        lay.addLayout(_row(_caption("Desktop:", inner), self.desktop_row))

        lay.addWidget(SectionChip("Content", "blue"))
        def chk(text, tip_title, tip, checked=True):
            c = QCheckBox(text, inner)
            c.setChecked(checked)
            c.setToolTip(rich_tip(tip_title, tip))
            lay.addWidget(c)
            return c
        self.chk_transcripts = chk("Include session transcripts (conversation flow)", "Transcripts", "Prompts, replies, tool calls and results per turn.")
        self.chk_state = chk("Include application state (settings, MCP servers, memory, plans, history)", "Application state",
                             "settings.json, ~/.claude.json summary, CLAUDE.md, auto memory, plans, tasks, prompt history, IDE locks.")
        self.chk_tokens = chk("Include token usage statistics", "Token usage", "Per-session, per-model and per-day tables (once per API response).")
        self.chk_tools = chk("Include tool call details (arguments + outputs)", "Tool calls", "Bash / Read / Edit / Task / MCP invocations, redacted.")
        self.chk_thinking = chk("Include thinking blocks", "Thinking", "Extended-thinking text where it was stored in the transcript.")
        self.chk_logs = chk("Include logs (debug tail + MCP cache)", "Logs", "debug/<session>.txt tail and claude-cli-nodejs MCP logs.")
        self.chk_desktop = chk("Include Claude Desktop (Cowork transcripts + Code-tab wrappers)", "Claude Desktop",
                               "Cowork sessions keep full JSONL transcripts under the Desktop app-data dir, not ~/.claude.")
        self.chk_orphaned = chk("Include .orphaned / .superseded transcript copies", "Earlier copies", "Sessions Claude Code set aside instead of deleting.")
        self.chk_per_session = chk("Write individual .md files per session", "Per-session", "One file per session in <report>_sessions/.")
        self.chk_subagents = chk("Include subagent sessions (Task / Agent tool)", "Subagents", "Linked to their parent session.")
        self.chk_flatten = chk("Flatten subagent turns into the parent session", "Flatten", "Show subagent turns inline at the Task call.", False)
        self.chk_flight = QCheckBox("Include time-in-flight section  (locked - experimental)", inner)
        self.chk_flight.setToolTip(rich_tip("Time in flight", "Derived request / tool / turn latencies. Locked until the experimental gate is opened."))
        self.chk_flight.setEnabled(False)
        lay.addWidget(self.chk_flight)
        self.log_limit = QSpinBox(inner)
        self.log_limit.setRange(50, 100000)
        self.log_limit.setSingleStep(250)
        self.log_limit.setValue(1000)
        self.log_limit.setMaximumWidth(240)
        lrow = QHBoxLayout()
        lrow.addWidget(_caption("Log rows:", inner))
        lrow.addWidget(self.log_limit)
        lrow.addStretch(1)
        lay.addLayout(lrow)

        lay.addWidget(SectionChip("Redaction level", "amber"))
        self.radio_standard = QRadioButton("Standard  -  secrets, e-mails, home paths (recommended)", inner)
        self.radio_standard.setChecked(True)
        self.radio_paranoid = QRadioButton("Paranoid  -  also strip paths, URLs, IPs, phone numbers", inner)
        self.radio_none = QRadioButton("None  -  keep text verbatim (hard secrets still hidden)", inner)
        self.level_hint = QLabel(LEVEL_HINTS["standard"], inner)
        self.level_hint.setProperty("muted", "true")
        self.level_hint.setWordWrap(True)
        for r in (self.radio_standard, self.radio_paranoid, self.radio_none):
            r.toggled.connect(lambda _=False: self.level_hint.setText(LEVEL_HINTS[self.level()]))
            lay.addWidget(r)
        lay.addWidget(self.level_hint)

        lay.addWidget(SectionChip("Run", "green"))
        actions = QHBoxLayout()
        self.start_btn = QPushButton(icons.icon("play", "#1b0f0a", 16), " Start Recovery", inner)
        self.start_btn.setProperty("primary", "true")
        self.start_btn.clicked.connect(self._emit_start)
        actions.addWidget(self.start_btn)
        self.cancel_btn = QPushButton(icons.icon("stop", "#ff5d5d", 16), " Cancel", inner)
        self.cancel_btn.setProperty("danger", "true")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        actions.addWidget(self.cancel_btn)
        actions.addStretch(1)
        lay.addLayout(actions)
        self.progress = ProgressWidget(inner)
        lay.addWidget(self.progress)
        lay.addWidget(SectionChip("Log", "blue"))
        self.console = ConsoleLog(inner)
        self.console.setMinimumHeight(200)
        lay.addWidget(self.console)
        scroll.setWidget(inner)
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.addWidget(scroll)
        self._toggles = (self.chk_transcripts, self.chk_state, self.chk_tokens, self.chk_tools, self.chk_thinking, self.chk_logs, self.chk_desktop,
                         self.chk_orphaned, self.chk_per_session, self.chk_subagents, self.chk_flatten, self.radio_standard, self.radio_paranoid, self.radio_none)

    def level(self) -> str:
        return "paranoid" if self.radio_paranoid.isChecked() else ("none" if self.radio_none.isChecked() else "standard")

    def set_flight_unlocked(self, unlocked: bool) -> None:
        self.chk_flight.setEnabled(unlocked)
        self.chk_flight.setText("Include time-in-flight section" + ("" if unlocked else "  (locked - experimental)"))
        if not unlocked:
            self.chk_flight.setChecked(False)

    def collect_options(self) -> RecoveryOptions:
        return RecoveryOptions(
            include_transcripts=self.chk_transcripts.isChecked(), include_app_state=self.chk_state.isChecked(),
            include_tokens=self.chk_tokens.isChecked(), include_tools=self.chk_tools.isChecked(), include_thinking=self.chk_thinking.isChecked(),
            include_logs=self.chk_logs.isChecked(), include_desktop=self.chk_desktop.isChecked(), include_orphaned=self.chk_orphaned.isChecked(),
            log_limit=self.log_limit.value(), redaction_level=self.level(), per_sessions=self.chk_per_session.isChecked(),
            include_subagents=self.chk_subagents.isChecked(), flatten_subagents=self.chk_flatten.isChecked(),
            include_flight=self.chk_flight.isEnabled() and self.chk_flight.isChecked(),
            desktop_dirs=(self.desktop_row.text(),) if self.desktop_row.text() else ())

    def _emit_start(self) -> None:
        self.start_requested.emit({"source": self.source_row.text(), "output": self.output_row.text(), "options": self.collect_options()})

    def set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for w in (self.source_row, self.output_row, self.desktop_row, self.log_limit, *self._toggles):
            w.setEnabled(not running)
        if running:
            self.progress.reset()
            self.progress.status.setText("Starting…")

    def log(self, message: str, level: str = "info") -> None:
        self.console.append_log(message, level)


def _caption(text: str, parent) -> QLabel:
    label = QLabel(text, parent)
    label.setProperty("muted", "true")
    label.setFixedWidth(60)
    return label


def _row(label: QWidget, widget: QWidget) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(10)
    layout.addWidget(label)
    layout.addWidget(widget, 1)
    return layout
