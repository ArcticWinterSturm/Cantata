"""Chat & Cowork page: Desktop catalog, browser footprint and export reconstruction."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMenu, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from . import icons
from ._paths import archive_dir, archive_report
from .widgets import ConsoleLog, PathRow, ProgressWidget, SectionChip, rich_tip


class ChatCoworkPage(QWidget):
    start_requested = pyqtSignal(object)
    cancel_requested = pyqtSignal()
    open_report = pyqtSignal(str)
    open_folder = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_report = ""
        self._last_extract_dir = ""
        self._last_indexeddb_dir = ""
        self._last_export_dir = ""
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget(scroll)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(12)
        title = QLabel("Chat & Cowork", inner)
        title.setProperty("h1", "true")
        lay.addWidget(title)
        sub = QLabel("Claude.ai Chat bodies live on Anthropic's servers. This scans what is local: Claude Desktop (Cowork transcripts & audit logs, "
                     "Code-tab wrappers), browser traces, and export archives you saved — and can rebuild exported conversations as redacted Markdown.", inner)
        sub.setProperty("muted", "true")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        lay.addWidget(SectionChip("Sources"))
        self.source_row = PathRow("~/.claude  (Code-tab wrappers point into its projects/)", mode="dir", parent=inner, title="Claude Code data directory")
        lay.addLayout(_row("Claude Code:", self.source_row))
        self.desktop_row = PathRow("auto-detect  (%APPDATA%\\Claude, MSIX LocalCache, ~/Library/Application Support/Claude …)", mode="dir", parent=inner,
                                   title="Claude Desktop data directory")
        lay.addLayout(_row("Desktop:", self.desktop_row))
        lay.addWidget(QLabel("Export archives (Settings → Privacy → Export data):", inner))
        self.exports = QListWidget(inner)
        self.exports.setMaximumHeight(110)
        lay.addWidget(self.exports)
        btns = QHBoxLayout()
        add = QPushButton(icons.icon("plus", "#9a9284", 16), " Add export…", inner)
        add.clicked.connect(self._add_exports)
        rm = QPushButton(icons.icon("cross", "#9a9284", 16), " Remove", inner)
        rm.clicked.connect(lambda: [self.exports.takeItem(self.exports.row(i)) for i in self.exports.selectedItems()])
        btns.addWidget(add)
        btns.addWidget(rm)
        btns.addStretch(1)
        lay.addLayout(btns)

        lay.addWidget(SectionChip("Options", "blue"))
        self.chk_browser = QCheckBox("Scan Chromium browser profiles (History, IndexedDB, Local Storage, Claude-in-Chrome store)", inner)
        self.chk_browser.setChecked(True)
        self.chk_transcripts = QCheckBox("Parse Cowork / Code-tab transcripts for turn, message and tool counts", inner)
        self.chk_transcripts.setChecked(True)
        self.chk_extract = QCheckBox("Rebuild conversations to redacted Markdown (export archives + Claude Desktop IndexedDB)", inner)
        self.chk_extract.setChecked(True)
        self.chk_thinking = QCheckBox("Include thinking blocks in reconstructed conversations", inner)
        self.chk_thinking.setChecked(True)
        for c in (self.chk_browser, self.chk_transcripts, self.chk_extract, self.chk_thinking):
            lay.addWidget(c)
        self.extract_row = PathRow(str(archive_dir() / "Cantata_chats"), mode="dir", parent=inner, title="Folder for reconstructed conversations")
        lay.addLayout(_row("Extract to:", self.extract_row))
        self.output_row = PathRow(str(archive_report("Cantata_chatcowork.md")), mode="file", parent=inner, default_name="Cantata_chatcowork.md",
                                  title="Save Chat & Cowork report")
        lay.addLayout(_row("Report:", self.output_row))
        self.level_combo = QComboBox(inner)
        self.level_combo.addItems(["standard", "paranoid", "none"])
        self.level_combo.setMaximumWidth(200)
        self.level_combo.setToolTip(rich_tip("Redaction level", "Same matrix as Recover. Hard secrets are hidden at every level."))
        lay.addLayout(_row("Redaction:", self.level_combo))

        lay.addWidget(SectionChip("Run", "green"))
        actions = QHBoxLayout()
        self.start_btn = QPushButton(icons.icon("play", "#1b0f0a", 16), " Scan", inner)
        self.start_btn.setProperty("primary", "true")
        self.start_btn.clicked.connect(self._emit_start)
        self.cancel_btn = QPushButton(icons.icon("stop", "#ff5d5d", 16), " Cancel", inner)
        self.cancel_btn.setProperty("danger", "true")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        self.open_btn = QPushButton(icons.icon("open", "#9a9284", 16), " Open report", inner)
        self.open_btn.setEnabled(False)
        self.open_btn.setToolTip(rich_tip("Open report", "Open the summarised Chat & Cowork Markdown report."))
        self.open_btn.clicked.connect(lambda: self.open_report.emit(self._last_report))
        self.open_dir_btn = QPushButton(icons.icon("open", "#9a9284", 16), " Open folder", inner)
        self.open_dir_btn.setEnabled(False)
        self.open_dir_btn.setToolTip(rich_tip("Open extracted folder",
                                              "Open the folder holding the per-conversation files (full bodies), not just the report. "
                                              "Right-click to pick a different folder when several were written."))
        self.open_dir_btn.clicked.connect(self._open_folder_clicked)
        self.open_dir_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.open_dir_btn.customContextMenuRequested.connect(self._open_folder_menu)
        for b in (self.start_btn, self.cancel_btn, self.open_btn, self.open_dir_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        lay.addLayout(actions)
        self.progress = ProgressWidget(inner)
        lay.addWidget(self.progress)
        self.summary = QLabel("", inner)
        self.summary.setProperty("muted", "true")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        lay.addWidget(SectionChip("Log", "blue"))
        self.console = ConsoleLog(inner)
        self.console.setMinimumHeight(180)
        lay.addWidget(self.console)
        scroll.setWidget(inner)
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.addWidget(scroll)
        self._lockable = (self.source_row, self.desktop_row, self.exports, self.chk_browser, self.chk_transcripts, self.chk_extract,
                          self.chk_thinking, self.extract_row, self.output_row, self.level_combo)

    def _add_exports(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Choose export archive(s)", str(Path.home() / "Downloads"),
                                                "Claude exports (*.zip *.json *.md);;All files (*)")
        for f in files:
            self.exports.addItem(f)

    def add_export(self, path: str) -> None:
        self.exports.addItem(path)

    def _emit_start(self) -> None:
        self.start_requested.emit({
            "source": self.source_row.text(), "desktop_dirs": [self.desktop_row.text()] if self.desktop_row.text() else [],
            "exports": [self.exports.item(i).text() for i in range(self.exports.count())],
            "scan_browser": self.chk_browser.isChecked(), "scan_transcripts": self.chk_transcripts.isChecked(),
            "extract_dir": (self.extract_row.text() or self.extract_row.edit.placeholderText()) if self.chk_extract.isChecked() else "",
            "include_thinking": self.chk_thinking.isChecked(),
            "output": self.output_row.text(), "level": self.level_combo.currentText()})

    def _folder_targets(self) -> list[tuple[str, str]]:
        """The folders that actually hold per-conversation files, best first."""
        targets: list[tuple[str, str]] = []
        for label, d in (("IndexedDB conversation bodies", self._last_indexeddb_dir),
                         ("Export-archive reconstructions", self._last_export_dir)):
            if d and Path(d).is_dir():
                targets.append((label, d))
        if not targets and self._last_extract_dir and Path(self._last_extract_dir).is_dir():
            targets.append(("Extract folder", self._last_extract_dir))
        return targets

    def _open_folder_clicked(self) -> None:
        """Open the folder that holds the per-conversation bodies (full conversation files).

        A run can write more than one such folder (``indexeddb/`` for rebuilt cache bodies,
        ``export_NN_*/conversations/`` for archive reconstructions). The primary one opens on a
        plain click; the others are on the right-click menu rather than a modal popup, so a
        single click always does something immediate.
        """
        targets = self._folder_targets()
        if targets:
            self.open_folder.emit(targets[0][1])

    def _open_folder_menu(self, pos) -> None:
        targets = self._folder_targets()
        if not targets:
            return
        menu = QMenu(self)
        for label, d in targets:
            act = menu.addAction(f"{label}  —  {d}")
            act.triggered.connect(lambda _=False, p=d: self.open_folder.emit(p))
        if self._last_extract_dir and Path(self._last_extract_dir).is_dir():
            menu.addSeparator()
            act = menu.addAction(f"Extract root  —  {self._last_extract_dir}")
            act.triggered.connect(lambda _=False: self.open_folder.emit(self._last_extract_dir))
        menu.exec(self.open_dir_btn.mapToGlobal(pos))

    def set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for w in self._lockable:
            w.setEnabled(not running)
        if running:
            self.progress.reset()

    def finished(self, path: str, stats: dict) -> None:
        self._last_report = path
        self.open_btn.setEnabled(bool(path))
        self._last_extract_dir = stats.get("extract_dir") or ""
        self._last_indexeddb_dir = stats.get("indexeddb_dir") or ""
        self._last_export_dir = stats.get("export_dir") or ""
        # Prefer a folder that actually holds conversation files.
        first = self._last_indexeddb_dir or self._last_export_dir or self._last_extract_dir
        self.open_dir_btn.setEnabled(bool(first) and Path(first).is_dir())
        self.summary.setText(f"{stats['threads']} threads ({stats['recoverable']} recoverable, {stats['server_only']} server-side only) • "
                             f"Cowork {stats['cowork']} • Code-tab {stats['code_tab']} • browser visits {stats['browser_visits']} • "
                             f"exports {stats['exports']} ({stats['export_messages']:,} messages)")
        idb_files = stats.get("indexeddb_files") or 0
        if idb_files:
            self.summary.setText(self.summary.text()
                                 + f" • IndexedDB {stats['indexeddb_conversations']} rebuilt ({idb_files} files in indexeddb/)")
        # Say plainly how much of what was opened is actually recoverable - a bare "4 recoverable"
        # next to 95 history visits reads like a failure when the cache simply no longer holds them.
        visited = stats.get("visited") or 0
        if visited:
            miss = stats.get("visited_missing") or 0
            self.summary.setText(self.summary.text()
                                 + f" • of {visited} conversations opened in the browser, "
                                 + (f"{stats.get('visited_recoverable', 0)} have a local body ({miss} server-side only)"
                                    if miss else "all have a local body"))

    def log(self, message: str, level: str = "info") -> None:
        self.console.append_log(message, level)


def _row(label: str, widget: QWidget) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(10)
    cap = QLabel(label)
    cap.setProperty("muted", "true")
    cap.setFixedWidth(84)
    layout.addWidget(cap)
    if widget.maximumWidth() < 10_000:              # width-capped widgets (combo boxes) must not be stretched/centred
        layout.addWidget(widget)
        layout.addStretch(1)
    else:
        layout.addWidget(widget, 1)
    return layout
