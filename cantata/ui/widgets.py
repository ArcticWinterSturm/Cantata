"""Reusable widgets: sidebar nav, chips, path rows, cards, console, and the soft-gate lock overlay."""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..core import gate
from . import icons


def rich_tip(title: str, body: str) -> str:
    return (f"<div style='margin:0'><b style='color:#d97757'>{title}</b><br/>"
            f"<span style='color:#d3cdc2'>{body}</span></div>")


class SideBarButton(QToolButton):
    def __init__(self, icon_name: str, text: str, tip_title: str, tip_body: str, parent=None):
        super().__init__(parent)
        self._icon_name, self._text = icon_name, text
        self.setProperty("nav", "true")
        self.setProperty("locked", "false")
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(rich_tip(tip_title, tip_body))
        self.set_locked(False)

    def set_locked(self, locked: bool) -> None:
        self.setProperty("locked", "true" if locked else "false")
        self.setIcon(icons.icon("lock" if locked else self._icon_name, "#6d675c" if locked else "#9a9284", 18))
        self.setText(f"  {self._text}".replace("&", "&&"))            # '&' would be eaten as a mnemonic marker
        self.style().unpolish(self)
        self.style().polish(self)


class SectionChip(QLabel):
    def __init__(self, text: str, color: str = "green", parent=None):
        super().__init__(text, parent)
        self.setProperty("chip", color)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)


class PathRow(QWidget):
    """Mono line edit with clear (x) and browse (...) buttons. Modes: dir | file (save) | open."""

    textChanged = pyqtSignal(str)

    def __init__(self, placeholder: str = "", mode: str = "dir", parent=None, title: str = "Select",
                 default_name: str = "", filt: str = "Markdown (*.md);;All files (*)"):
        super().__init__(parent)
        self.mode, self.title, self.default_name, self.filt = mode, title, default_name, filt
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = QLineEdit(self)
        self.edit.setProperty("mono", "true")
        self.edit.setPlaceholderText(placeholder)
        self.edit.textChanged.connect(self.textChanged.emit)
        lay.addWidget(self.edit, 1)
        self.clear_btn = QPushButton("×", self)
        self.clear_btn.setProperty("mini", "true")
        self.clear_btn.setFixedWidth(30)
        self.clear_btn.clicked.connect(lambda: self.edit.clear())
        lay.addWidget(self.clear_btn)
        self.browse_btn = QPushButton("…", self)
        self.browse_btn.setProperty("mini", "true")
        self.browse_btn.setFixedWidth(30)
        self.browse_btn.clicked.connect(self.browse)
        lay.addWidget(self.browse_btn)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value)

    def browse(self) -> None:
        start = self.text() or str(Path.home())
        if self.mode == "dir":
            picked = QFileDialog.getExistingDirectory(self, self.title, start)
        elif self.mode == "open":
            picked, _ = QFileDialog.getOpenFileName(self, self.title, start, self.filt)
        else:
            picked, _ = QFileDialog.getSaveFileName(self, self.title, self.text() or str(Path.home() / self.default_name), self.filt)
        if picked:
            self.edit.setText(Path(picked).as_posix() if self.mode != "dir" else picked)


class StatCard(QFrame):
    def __init__(self, caption: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setProperty("card", "true")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        self.value_label = QLabel(value, self)
        self.value_label.setObjectName("CardValue")
        self.caption_label = QLabel(caption.upper(), self)
        self.caption_label.setObjectName("CardCaption")
        lay.addWidget(self.value_label)
        lay.addWidget(self.caption_label)

    def set_value(self, value: str, tip: str = "") -> None:
        self.value_label.setText(value)
        if tip:
            self.setToolTip(rich_tip(self.caption_label.text().title(), tip))


class ConsoleLog(QPlainTextEdit):
    LEVEL_COLORS = {"info": "#d3cdc2", "ok": "#3fd68f", "warn": "#ffb454", "error": "#ff5d5d"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("console", "true")
        self.setReadOnly(True)
        self.setMaximumBlockCount(4000)

    def append_log(self, message: str, level: str = "info") -> None:
        color = self.LEVEL_COLORS.get(level, "#d3cdc2")
        safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.appendHtml(f'<span style="color:#6d675c">[{time.strftime("%H:%M:%S")}]</span> <span style="color:{color}">{safe}</span>')
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


class ProgressWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 1000)
        self.bar.setFormat("")
        row.addWidget(self.bar, 1)
        self.pct = QLabel("0%", self)
        self.pct.setFixedWidth(44)
        row.addWidget(self.pct)
        lay.addLayout(row)
        self.status = QLabel("Idle", self)
        self.status.setProperty("muted", "true")
        lay.addWidget(self.status)

    def update(self, frac: float, label: str = "") -> None:  # noqa: A003 - Qt-style name
        value = int(max(0.0, min(frac, 1.0)) * 1000)
        self.bar.setValue(value)
        self.pct.setText(f"{value // 10}%")
        if label:
            self.status.setText(label)

    def reset(self) -> None:
        self.bar.setValue(0)
        self.pct.setText("0%")
        self.status.setText("Idle")


class LabeledRow(QWidget):
    def __init__(self, label: str, widget: QWidget, width: int = 110, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        cap = QLabel(label, self)
        cap.setFixedWidth(width)
        cap.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cap.setProperty("muted", "true")
        lay.addWidget(cap)
        lay.addWidget(widget, 1)


class LockOverlay(QFrame):
    """The soft gate: covers a page until the user clicks Unlock and confirms. A speed bump, not security."""

    unlocked = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("LockOverlay")
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card = QFrame(self)
        card.setObjectName("LockCard")
        card.setFixedWidth(520)
        self._card, self._texts = card, []
        lay = QVBoxLayout(card)
        lay.setContentsMargins(30, 28, 30, 26)
        lay.setSpacing(12)
        ico = QLabel(card)
        ico.setPixmap(icons.icon("lock", "#d97757", 44).pixmap(44, 44))
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(ico)
        title = QLabel(gate.LOCK_TITLE, card)
        title.setProperty("h1", "true")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        self._texts.append((title, gate.LOCK_TITLE))
        body = QLabel(gate.LOCK_BODY, card)
        body.setWordWrap(True)
        body.setProperty("muted", "true")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(body)
        self._texts.append((body, gate.LOCK_BODY))
        self.unlock_btn = QPushButton(icons.icon("unlock", "#1b0f0a", 16), "  Unlock experimental features…", card)
        self.unlock_btn.setProperty("primary", "true")
        self.unlock_btn.setObjectName("UnlockButton")
        self.unlock_btn.clicked.connect(self._ask)
        lay.addWidget(self.unlock_btn, 0, Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("You can re-lock it any time in Settings → Experimental.", card)
        hint.setProperty("muted", "true")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hint)
        outer.addWidget(card)
        self.raise_()

    def _fit_card(self) -> None:
        """A word-wrapped QLabel inside a fixed-width card under-reports its height; measure with the *styled* font."""
        self.ensurePolished()
        width = self._card.width() - 60                      # card margins
        for label, text in self._texts:
            label.ensurePolished()
            need = label.fontMetrics().boundingRect(0, 0, width, 6000, Qt.TextFlag.TextWordWrap, text).height()
            label.setMinimumHeight(need + 12)
        self._card.layout().activate()
        self._card.adjustSize()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._fit_card()

    def _ask(self) -> None:
        answer = QMessageBox.question(
            self, "Unlock experimental features?",
            "Enable time-in-flight analytics and the Live modes?\n\nThey only read local files and listen on 127.0.0.1, "
            "but they are still experimental.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.unlocked.emit()

    def fit(self) -> None:
        if self.parent() is not None:
            self.setGeometry(self.parent().rect())       # type: ignore[union-attr]


def mono_font() -> QFont:
    font = QFont("Cascadia Code")
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


__all__ = ["ConsoleLog", "LabeledRow", "LockOverlay", "PathRow", "ProgressWidget", "SectionChip", "SideBarButton",
           "StatCard", "rich_tip", "mono_font", "QComboBox", "QCheckBox"]
