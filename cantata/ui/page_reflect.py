"""Reflect page: generate a structured reflection of the user's Claude activity.

The reflection is generated on-demand when the tab is opened (if the setting is enabled)
or when the user clicks "Generate". It uses the same memory schema as the rest of
Cantata and produces a Markdown report plus a visual summary.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QScrollArea, QTextBrowser,
                             QVBoxLayout, QWidget)

from ..core import gate, reflect, scanner
from . import icons
from ._paths import archive_report
from .widgets import ConsoleLog, PathRow, SectionChip, StatCard, rich_tip
from .worker import FnWorker


def _generate(source: str) -> dict:
    """Run the reflection engine."""
    config_dir = Path(source) if source else scanner.detect_default_source()
    return reflect.generate_reflection(config_dir)


def _render(reflection: dict) -> str:
    """Render the reflection as Markdown."""
    return reflect.render_markdown(reflection)


class ReflectPage(QWidget):
    """Reflect page — generates a structured reflection of Claude activity."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: FnWorker | None = None
        self._last_reflection: dict | None = None
        self._last_markdown: str = ""

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget(scroll)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(12)

        # Header
        head = QHBoxLayout()
        title = QLabel("Reflect", inner)
        title.setProperty("h1", "true")
        head.addWidget(title)
        chip = QLabel("MEMORY", inner)
        chip.setProperty("chip", "blue")
        head.addWidget(chip)
        head.addStretch(1)
        self.gen_btn = QPushButton(icons.icon("refresh", "#1b0f0a", 16), " Generate", inner)
        self.gen_btn.setProperty("primary", "true")
        self.gen_btn.clicked.connect(self._generate)
        head.addWidget(self.gen_btn)
        self.save_btn = QPushButton(icons.icon("report", "#9a9284", 16), " Save report", inner)
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        head.addWidget(self.save_btn)
        lay.addLayout(head)

        sub = QLabel("Generates a structured reflection of your Claude activity — topics, skills, insights — "
                     "from the same memory schema Cantata already reads. Generated on-demand when you open this "
                     "tab (if enabled in Settings) or when you click Generate.", inner)
        sub.setProperty("muted", "true")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        # Source
        lay.addWidget(SectionChip("Source"))
        self.source_row = PathRow("~/.claude", mode="dir", parent=inner, title="Claude Code data directory")
        lay.addWidget(self.source_row)

        # Activity cards
        lay.addWidget(SectionChip("Activity", "blue"))
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.a_cards = {k: StatCard(c) for k, c in (
            ("conversations", "Conversations"),
            ("sessions", "Sessions"),
            ("day", "Most active day"),
            ("hour", "Peak hour"),
            ("range", "Date range"),
        )}
        for c in self.a_cards.values():
            cards.addWidget(c)
        lay.addLayout(cards)

        # Topics
        lay.addWidget(SectionChip("Topics", "green"))
        self.topics_label = QLabel("Generate a reflection to see topic breakdown.", inner)
        self.topics_label.setProperty("muted", "true")
        self.topics_label.setWordWrap(True)
        lay.addWidget(self.topics_label)

        # Skills
        lay.addWidget(SectionChip("Skills", "amber"))
        self.skills_label = QLabel("Generate a reflection to see skills.", inner)
        self.skills_label.setProperty("muted", "true")
        self.skills_label.setWordWrap(True)
        lay.addWidget(self.skills_label)

        # Insights
        lay.addWidget(SectionChip("Insights", "blue"))
        self.insights_label = QLabel("Generate a reflection to see insights.", inner)
        self.insights_label.setProperty("muted", "true")
        self.insights_label.setWordWrap(True)
        lay.addWidget(self.insights_label)

        # Memory files
        lay.addWidget(SectionChip("Memory files", "blue"))
        self.memory_label = QLabel("Generate a reflection to see memory file counts.", inner)
        self.memory_label.setProperty("muted", "true")
        self.memory_label.setWordWrap(True)
        lay.addWidget(self.memory_label)

        # Report preview
        lay.addWidget(SectionChip("Report", "blue"))
        self.preview = QTextBrowser(inner)
        self.preview.setOpenExternalLinks(False)
        self.preview.setPlaceholderText("The reflection report will appear here…")
        self.preview.setMinimumHeight(200)
        lay.addWidget(self.preview, 1)

        # Log
        lay.addWidget(SectionChip("Log", "blue"))
        self.console = ConsoleLog(inner)
        self.console.setMinimumHeight(120)
        lay.addWidget(self.console)

        scroll.setWidget(inner)
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.addWidget(scroll)

        self._lockable = (self.source_row,)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Auto-generate on first open if the setting is enabled
        if gate.get_setting("reflect_auto_generate", True) and self._last_reflection is None:
            self._generate()

    def _source(self) -> str:
        return self.source_row.text() or str(scanner.detect_default_source())

    def _generate(self) -> None:
        self.gen_btn.setEnabled(False)
        self.console.append_log("Generating reflection…", "info")
        self._worker = FnWorker(_generate, self._source(), parent=self)
        self._worker.result.connect(self._on_result)
        self._worker.failed.connect(self._on_fail)
        self._worker.start()

    def _on_result(self, reflection: dict) -> None:
        self.gen_btn.setEnabled(True)
        self._last_reflection = reflection
        self._last_markdown = _render(reflection)
        self.save_btn.setEnabled(True)

        # Activity
        act = reflection["activity"]
        self.a_cards["conversations"].set_value(f"{act['total_conversations']:,}")
        self.a_cards["sessions"].set_value(f"{act['total_sessions']:,}")
        self.a_cards["day"].set_value(act["most_active_day"])
        self.a_cards["hour"].set_value(act["peak_hour"])
        self.a_cards["range"].set_value(act["date_range"])

        # Topics
        if reflection["topics"]:
            topic_lines = []
            for t in reflection["topics"]:
                terms = ", ".join(t.get("terms", [])[:4])
                topic_lines.append(f"**{t['name']}** — {t['description']}")
                if terms:
                    topic_lines.append(f"  {terms}")
            self.topics_label.setText("\n".join(topic_lines))
            self.topics_label.setProperty("muted", "false")
        else:
            self.topics_label.setText("No topics detected.")
            self.topics_label.setProperty("muted", "true")

        # Skills
        if reflection["skills"]:
            skill_lines = []
            for s in reflection["skills"]:
                skill_lines.append(f"**{s['name']}** — {s['statement']}")
                skill_lines.append(f"  {s['evidence']}")
            self.skills_label.setText("\n".join(skill_lines))
            self.skills_label.setProperty("muted", "false")
        else:
            self.skills_label.setText("No skills detected.")
            self.skills_label.setProperty("muted", "true")

        # Insights
        if reflection["insights"]:
            self.insights_label.setText("\n".join(f"• {i}" for i in reflection["insights"]))
            self.insights_label.setProperty("muted", "false")
        else:
            self.insights_label.setText("No insights generated.")
            self.insights_label.setProperty("muted", "true")

        # Memory files
        mf = reflection["memory_files"]
        self.memory_label.setText(
            f"CLAUDE.md files: {mf['claude_md']}  •  "
            f"Auto-memory files: {mf['auto_memory']}  •  "
            f"Agent memory files: {mf['agent_memory']}"
        )
        self.memory_label.setProperty("muted", "false")

        # Preview
        self.preview.setMarkdown(self._last_markdown)

        self.console.append_log("Reflection generated.", "ok")

    def _on_fail(self, message: str) -> None:
        self.gen_btn.setEnabled(True)
        self.console.append_log(f"Failed: {message}", "error")

    def _save(self) -> None:
        if self._last_markdown:
            out = archive_report("Cantata_reflect.md")
            out.write_text(self._last_markdown + "\n", encoding="utf-8")
            self.console.append_log(f"Report written: {out}", "ok")

    def log(self, message: str, level: str = "info") -> None:
        self.console.append_log(message, level)
