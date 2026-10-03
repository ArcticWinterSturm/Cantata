"""Archive page: auto-detect the Claude install, show the retention clock and headline numbers."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget

from ..core import desktop, paths, scanner, stores
from . import icons
from .widgets import PathRow, SectionChip, StatCard, rich_tip
from .worker import FnWorker


def _scan(source: str) -> dict:
    stats = scanner.quick_stats(Path(source))
    stats["inventory"] = stores.inventory(Path(source))
    live = paths.is_live_source(Path(source))
    info = desktop.scan_desktop(None if live else [], read_audits=False)
    stats["desktop"] = info.stats() if info.found else None
    stats["desktop_variants"] = info.variants
    stats["desktop_skipped"] = not live
    return stats


class DashboardPage(QWidget):
    goto_recover = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: FnWorker | None = None
        self._scanned_once = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("Archive", self)
        title.setProperty("h1", "true")
        head.addWidget(title)
        head.addStretch(1)
        self.rescan_btn = QPushButton(icons.icon("refresh", "#9a9284", 16), " Rescan", self)
        self.rescan_btn.clicked.connect(lambda: self.scan(self.source_row.text()))
        head.addWidget(self.rescan_btn)
        outer.addLayout(head)
        sub = QLabel("Auto-detects your Claude Code data directory and Claude Desktop, and shows how much of it "
                     "is about to be swept before you recover anything.", self)
        sub.setProperty("muted", "true")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        outer.addWidget(SectionChip("Source directory"))
        row = QHBoxLayout()
        self.source_row = PathRow("~/.claude  (or $CLAUDE_CONFIG_DIR, or a copied forensic image)", mode="dir", parent=self,
                                  title="Select the Claude Code data directory")
        row.addWidget(self.source_row, 1)
        detect = QPushButton(icons.icon("search", "#9a9284", 16), " Auto-detect", self)
        detect.setToolTip(rich_tip("Auto-detect", "Fill the field with $CLAUDE_CONFIG_DIR or ~/.claude and scan it."))
        detect.clicked.connect(self.detect)
        row.addWidget(detect)
        outer.addLayout(row)

        self.banner = QLabel("", self)
        self.banner.setProperty("banner", "amber")
        self.banner.setWordWrap(True)
        self.banner.hide()
        outer.addWidget(self.banner)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards = {k: StatCard(c) for k, c in (("sessions", "Sessions"), ("subagents", "Subagents"), ("orphaned", "Orphaned"),
                                                   ("projects", "Projects"), ("prompts", "Prompts"), ("size", "Data size"),
                                                   ("range", "Date range"), ("retention", "Retention"))}
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, i // 4, i % 4)
        outer.addLayout(grid)

        outer.addWidget(SectionChip("Version detection", "amber"))
        self.version_label = QLabel("Scanning…", self)
        self.version_label.setProperty("muted", "true")
        self.version_label.setWordWrap(True)
        outer.addWidget(self.version_label)

        outer.addWidget(SectionChip("Claude Desktop", "blue"))
        self.desktop_label = QLabel("Scanning…", self)
        self.desktop_label.setProperty("muted", "true")
        self.desktop_label.setWordWrap(True)
        outer.addWidget(self.desktop_label)

        outer.addWidget(SectionChip("Inventory", "blue"))
        self.inventory = QListWidget(self)
        self.inventory.setMaximumHeight(200)
        outer.addWidget(self.inventory)
        self.issues = QLabel("", self)
        self.issues.setProperty("muted", "true")
        self.issues.setWordWrap(True)
        outer.addWidget(self.issues)

        foot = QHBoxLayout()
        foot.addStretch(1)
        go = QPushButton(icons.icon("recover", "#1b0f0a", 16), " Start a recovery →", self)
        go.setProperty("primary", "true")
        go.clicked.connect(lambda: self.goto_recover.emit(self.source_row.text()))
        foot.addWidget(go)
        outer.addLayout(foot)
        outer.addStretch(1)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._scanned_once:
            self._scanned_once = True
            self.scan(self.source_row.text()) if self.source_row.text() else self.detect()

    def detect(self) -> None:
        d = scanner.detect_default_source()
        self.source_row.setText(str(d))
        self.scan(str(d))

    def scan(self, source: str) -> None:
        if not source:
            return
        self.rescan_btn.setEnabled(False)
        self.issues.setText("Scanning…")
        self._worker = FnWorker(_scan, source, parent=self)
        self._worker.result.connect(self._on_stats)
        self._worker.failed.connect(self._on_fail)
        self._worker.start()

    def _on_fail(self, message: str) -> None:
        self.rescan_btn.setEnabled(True)
        self.issues.setText(f"Scan failed: {message}")

    def _on_stats(self, s: dict) -> None:
        self.rescan_btn.setEnabled(True)
        self.cards["sessions"].set_value(str(s["active_transcripts"]), f"Active transcripts on disk (stats-cache remembers {s['sessions_reported']})")
        self.cards["subagents"].set_value(str(s["subagents"]), "Subagent transcripts (Task / Agent tool)")
        self.cards["orphaned"].set_value(str(s["orphaned_transcripts"]), ".orphaned-* / .superseded-* earlier copies")
        self.cards["projects"].set_value(str(s["projects"]), "Project folders under projects/")
        self.cards["prompts"].set_value(f"{s['history_entries']:,}", "Lines in history.jsonl (kept until you delete it)")
        self.cards["size"].set_value(f"{s['size_mb']} MB", f"{s['transcript_mb']} MB of it is transcripts")
        lo, hi = s["date_range"]
        self.cards["range"].set_value(f"{lo[5:] or '—'} → {hi[5:] or '—'}" if lo else "—", "Oldest to newest transcript (file mtime)")
        self.cards["retention"].set_value(f"{s['retention_effective']} d", "cleanupPeriodDays" + ("" if s["retention_days"] else " (default)"))
        if s["active_transcripts"] and s["expiring_soon"]:
            self.banner.setText(f"⚠  {s['expiring_soon']} transcript(s) are within a week of Claude Code's {s['retention_effective']}-day sweep "
                                f"(oldest is {s['oldest_age_days']} days old). Recover / archive them now.")
            self.banner.show()
        else:
            self.banner.hide()
        parts = []
        if s["app_version"]:
            parts.append(f"Claude Code {s['app_version']}  (seen: {', '.join(s['versions'][:5])})")
        if s["global_json"]:
            parts.append("~/.claude.json ✓")
        if s["has_credentials"]:
            parts.append(".credentials.json present (never read)")
        self.version_label.setText("  •  ".join(parts) or "No version information detected")
        d = s.get("desktop")
        if d:
            self.desktop_label.setText(f"Found: " + "; ".join(f"{v['path']} [{v['flavour']}]" for v in s["desktop_variants"]) +
                                       f"\nCowork sessions: {d['cowork_sessions']} ({d['cowork_transcripts']} transcripts) • Code-tab wrappers: "
                                       f"{d['code_tab_sessions']} • memory files: {d['memory_files']}")
        elif s.get("desktop_skipped"):
            self.desktop_label.setText("Not auto-detected: this source is not this machine's live Claude dir (a copied image?). "
                                       "Point the Desktop field on the Recover page at the copied Claude Desktop app-data directory to include it.")
        else:
            self.desktop_label.setText("No Claude Desktop data directory found on this machine.")
        self.inventory.clear()
        for name, present, text in s["inventory"]:
            self.inventory.addItem(f"{'✓' if present else '✗'}  {name:20s}  {text}")
            self.inventory.item(self.inventory.count() - 1).setForeground(QColor("#3fd68f" if present else "#6d675c"))
        issues = list(s["issues"])
        if not s["exists"]:
            issues.append("Directory does not exist - pick a valid source before recovering.")
        self.issues.setText("  •  ".join(issues) if issues else "Source looks like a genuine Claude Code installation. Ready to recover.")
