"""Settings & About page - includes the experimental gate switch."""

from __future__ import annotations

import platform

from PyQt6.QtCore import QT_VERSION_STR, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout, QWidget

from .. import APP_NAME, APP_TAGLINE, __version__
from ..core import gate, scanner
from . import theme
from .widgets import PathRow, SectionChip, rich_tip


class SettingsPage(QWidget):
    accent_changed = pyqtSignal(str)
    gate_changed = pyqtSignal(bool)
    output_dir_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(14)
        title = QLabel("Settings", self)
        title.setProperty("h1", "true")
        lay.addWidget(title)

        lay.addWidget(SectionChip("Defaults"))
        row = QHBoxLayout()
        cap = QLabel("Output dir:", self)
        cap.setProperty("muted", "true")
        cap.setFixedWidth(84)
        row.addWidget(cap)
        self.default_out = PathRow("(Archive/ next to the app)", mode="dir", parent=self, title="Default output folder")
        self.default_out.setToolTip(rich_tip("Default output folder", "Where reports and reconstructed chats are written when a page's own "
                                                                     "output field is empty. Empty = Archive/ next to the app."))
        self.default_out.setText(str(gate.get_setting("output_dir", "") or ""))
        self.default_out.textChanged.connect(self._out_dir)
        row.addWidget(self.default_out, 1)
        lay.addLayout(row)
        self.auto_open = QCheckBox("Switch to the Report page automatically when a recovery finishes", self)
        self.auto_open.setChecked(bool(gate.get_setting("auto_open", True)))
        self.auto_open.toggled.connect(lambda v: gate.set_setting("auto_open", bool(v)))
        lay.addWidget(self.auto_open)
        self.reflect_auto = QCheckBox("Auto-generate reflection when the Reflect tab is opened", self)
        self.reflect_auto.setChecked(bool(gate.get_setting("reflect_auto_generate", True)))
        self.reflect_auto.toggled.connect(lambda v: gate.set_setting("reflect_auto_generate", bool(v)))
        lay.addWidget(self.reflect_auto)
        arow = QHBoxLayout()
        acap = QLabel("Accent:", self)
        acap.setProperty("muted", "true")
        acap.setFixedWidth(84)
        arow.addWidget(acap)
        self.accent_combo = QComboBox(self)
        self.accent_combo.addItems(list(theme.ACCENTS))
        saved = gate.get_setting("accent", theme.DEFAULT_ACCENT)
        if saved in theme.ACCENTS:
            self.accent_combo.setCurrentText(saved)
        self.accent_combo.currentTextChanged.connect(self._accent)
        arow.addWidget(self.accent_combo, 1)
        lay.addLayout(arow)

        lay.addWidget(SectionChip("Experimental", "amber"))
        self.gate_box = QCheckBox("Unlock time-in-flight analytics and the Live modes", self)
        self.gate_box.setChecked(gate.is_unlocked())
        self.gate_box.setToolTip(rich_tip("Soft gate", "A speed bump, not security. Also openable with CANTATA_EXPERIMENTAL=1 or "
                                                       "`cli gate --unlock`. Off by default."))
        self.gate_box.toggled.connect(self._gate)
        lay.addWidget(self.gate_box)
        hint = QLabel("Built but switched off for now: per-request time in flight (time to first token, tool wait, turn decomposition) and a live "
                      "transcript / OpenTelemetry tracker. Everything they do is local and read-only on ~/.claude.", self)
        hint.setProperty("muted", "true")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        lay.addWidget(SectionChip("About", "blue"))
        about = QTextBrowser(self)
        about.setOpenExternalLinks(False)
        about.setHtml(f"""
        <div style="font-family:'Segoe UI',sans-serif; color:#e6e0d6; font-size:13px;">
          <h2 style="color:#d97757; margin-top:0;">{APP_NAME} {__version__}</h2>
          <p>{APP_TAGLINE}.</p>
          <p>Reads <code>~/.claude</code> (JSONL transcripts, history.jsonl, stats-cache.json, sessions-index.json, settings, memory, plans,
             todos), Claude Desktop (Cowork transcripts + audit logs, Code-tab wrappers) and the claude.ai browser footprint / export archives;
             reconstructs every conversation with turns, tool calls, subagents and token usage; redacts PII and secrets; and writes one
             navigable Markdown report plus a file per session.</p>
          <ul>
            <li><b>Privacy:</b> offline; the only socket is the optional Live-mode receiver on 127.0.0.1.</li>
            <li><b>Safety:</b> everything is opened read-only; Claude is never modified. Token values are never read out.</li>
            <li><b>Hard secrets</b> (API keys, OAuth tokens, JWTs, private keys, account / org ids) are redacted at every level, including <i>None</i>.</li>
            <li><b>Retention:</b> Claude Code deletes transcripts after <code>cleanupPeriodDays</code> (default 30) — archive early.</li>
          </ul>
          <p style="color:#9a9284;">Python {platform.python_version()} &nbsp;•&nbsp; Qt {QT_VERSION_STR} &nbsp;•&nbsp; {platform.system()} {platform.release()}</p>
          <p style="color:#9a9284;">Default source: <code>{scanner.detect_default_source()}</code></p>
        </div>""")
        lay.addWidget(about, 1)

    def _out_dir(self, text: str) -> None:
        gate.set_setting("output_dir", text.strip())
        self.output_dir_changed.emit(text.strip())

    def _accent(self, name: str) -> None:
        gate.set_setting("accent", name)
        self.accent_changed.emit(name)

    def _gate(self, value: bool) -> None:
        gate.set_unlocked(value)
        self.gate_changed.emit(value)

    def sync_gate(self, unlocked: bool) -> None:
        """Reflect a gate change made elsewhere (the lock overlay) without re-emitting."""
        self.gate_box.blockSignals(True)
        self.gate_box.setChecked(unlocked)
        self.gate_box.blockSignals(False)
