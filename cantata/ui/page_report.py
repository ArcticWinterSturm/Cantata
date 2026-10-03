"""Report page: preview the generated Markdown and open it externally."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from . import icons
from .widgets import SectionChip
from .worker import FnWorker


def _read(path: Path) -> tuple[str, int]:
    size = path.stat().st_size
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(8_000_000), size            # preview cap; the file on disk is complete


class ReportPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.path: Path | None = None
        self._worker: FnWorker | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(14)
        head = QHBoxLayout()
        title = QLabel("Report", self)
        title.setProperty("h1", "true")
        head.addWidget(title)
        head.addStretch(1)
        for text, icon, fn in ((" Open file", "open", self.open_file), (" Show in folder", "folder", self.show_in_folder), (" Copy path", "copy", self.copy_path)):
            b = QPushButton(icons.icon(icon, "#9a9284", 16), text, self)
            b.clicked.connect(fn)
            head.addWidget(b)
        lay.addLayout(head)
        self.meta = QLabel("No report generated yet - run a recovery first.", self)
        self.meta.setProperty("muted", "true")
        lay.addWidget(self.meta)
        lay.addWidget(SectionChip("Preview", "blue"))
        self.preview = QPlainTextEdit(self)
        self.preview.setProperty("preview", "true")
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("The recovered Markdown report will appear here…")
        lay.addWidget(self.preview, 1)

    def load(self, path: str) -> None:
        self.path = Path(path)
        self.meta.setText(f"Loading {self.path} …")
        self._worker = FnWorker(_read, self.path, parent=self)
        self._worker.result.connect(self._show)
        self._worker.failed.connect(lambda m: self.meta.setText(f"Could not read report: {m}"))
        self._worker.start()

    def _show(self, payload: tuple) -> None:
        text, size = payload
        self.preview.setPlainText(text)
        self.preview.verticalScrollBar().setValue(0)
        note = "  •  preview capped at 8 MB — open the file for the rest" if size > 8_000_000 else ""
        self.meta.setText(f"{self.path}  •  {size:,} bytes  •  {len(text.splitlines()):,} lines shown{note}")

    def open_file(self) -> None:
        if self.path and self.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))

    def show_in_folder(self) -> None:
        if self.path and self.path.parent.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))

    def copy_path(self) -> None:
        if self.path:
            QApplication.clipboard().setText(str(self.path))
