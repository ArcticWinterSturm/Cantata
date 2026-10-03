"""Main window: sidebar navigation + stacked pages + background workers."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QStackedWidget, QStatusBar, QVBoxLayout, QWidget

from .. import APP_NAME, APP_TAGLINE, __version__
from ..core import gate, scanner
from . import icons, theme
from ._paths import archive_dir
from .page_chatcowork import ChatCoworkPage
from .page_dashboard import DashboardPage
from .page_flight import FlightLivePage
from .page_recover import RecoverPage
from .page_reflect import ReflectPage
from .page_report import ReportPage
from .page_settings import SettingsPage
from .widgets import SideBarButton, rich_tip
from .worker import RecoveryWorker, WebChatWorker, wait_all

PAGES = ("Archive", "Recover", "Chat & Cowork", "Flight & Live", "Reflect", "Report", "Settings")

# Named indices. Use these instead of bare numbers: inserting a page shifts every
# literal after it, which previously left tests asserting a stale Report index (4 vs 5)
# and silently pointing at the wrong page in `show_page()`.
PAGE_INDEX = {name: i for i, name in enumerate(PAGES)}
ARCHIVE, RECOVER, CHATCOWORK, FLIGHT, REFLECT, REPORT, SETTINGS = range(len(PAGES))


class MainWindow(QMainWindow):
    def __init__(self, initial_source: str = ""):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - {APP_TAGLINE}")
        self.setWindowIcon(icons.icon("logo", "#d97757", 64))
        self.resize(1260, 840)
        self.setMinimumSize(1040, 700)
        self.worker: RecoveryWorker | None = None
        self._chat_worker: WebChatWorker | None = None
        self._out_dir = ""

        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QWidget(central)
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(224)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 14)
        side.setSpacing(6)
        logo_row = QHBoxLayout()
        logo_icon = QLabel(sidebar)
        logo_icon.setPixmap(icons.icon("logo", "#d97757", 34).pixmap(34, 34))
        logo_row.addWidget(logo_icon)
        col = QVBoxLayout()
        logo = QLabel(APP_NAME, sidebar)
        logo.setObjectName("LogoLabel")
        logo.setToolTip(rich_tip(APP_NAME, APP_TAGLINE))
        sub = QLabel("CLAUDE ARCHIVE", sidebar)
        sub.setObjectName("LogoSub")
        col.addWidget(logo)
        col.addWidget(sub)
        logo_row.addLayout(col)
        logo_row.addStretch(1)
        side.addLayout(logo_row)
        side.addSpacing(16)

        self.nav_buttons = [
            SideBarButton("dashboard", "Archive", "Archive", "Detect the Claude Code data dir and Claude Desktop, see the retention clock, then recover."),
            SideBarButton("recover", "Recover", "Recover", "Choose content and redaction, then write the Markdown report plus per-session files."),
            SideBarButton("chatcowork", "Chat & Cowork", "Chat & Cowork",
                          "Cowork transcripts, Code-tab wrappers, browser traces and export archives (with full conversation reconstruction)."),
            SideBarButton("flight", "Flight & Live", "Flight & Live (experimental)",
                          "Time in flight (TTFT-style latency analytics) and live capture. Locked until you open the experimental gate."),
            SideBarButton("reflect", "Reflect", "Reflect",
                          "Generate a structured reflection of your Claude activity — topics, skills, insights."),
            SideBarButton("report", "Report", "Report", "Preview the generated Markdown, open it or reveal it in your file manager."),
            SideBarButton("settings", "Settings", "Settings & About", "Defaults, accent colour, the experimental gate, and what Cantata will never write."),
        ]
        for i, b in enumerate(self.nav_buttons):
            b.clicked.connect(lambda _=False, idx=i: self.show_page(idx))
            side.addWidget(b)
        side.addStretch(1)
        ver = QLabel(f"v{__version__}  •  local only", sidebar)
        ver.setObjectName("SideVersion")
        side.addWidget(ver)
        root.addWidget(sidebar)

        self.stack = QStackedWidget(central)
        self.dashboard = DashboardPage(self)
        self.recover = RecoverPage(self)
        self.chatcowork = ChatCoworkPage(self)
        self.flight = FlightLivePage(self)
        self.reflect = ReflectPage(self)
        self.report = ReportPage(self)
        self.settings = SettingsPage(self)
        for page in (self.dashboard, self.recover, self.chatcowork, self.flight, self.reflect, self.report, self.settings):
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        status = QStatusBar(self)
        self.status_label = QLabel("Ready", status)
        status.addWidget(self.status_label, 1)
        self.setStatusBar(status)

        self.dashboard.goto_recover.connect(self.start_from_dashboard)
        self.recover.start_requested.connect(self.begin_recovery)
        self.recover.cancel_requested.connect(self.cancel_recovery)
        self.chatcowork.start_requested.connect(self.begin_chatcowork)
        self.chatcowork.cancel_requested.connect(self.cancel_chatcowork)
        self.chatcowork.open_report.connect(self._open_report_file)
        self.chatcowork.open_folder.connect(self._open_folder)
        self.settings.accent_changed.connect(self.apply_accent)
        self.settings.output_dir_changed.connect(self.apply_output_dir)
        self.apply_output_dir(str(gate.get_setting("output_dir", "") or ""))
        self.settings.gate_changed.connect(self.set_gate)
        self.flight.unlock_requested.connect(lambda: self.set_gate(True))
        self.flight.mode_changed.connect(self._on_live_mode)
        self.apply_gate(gate.is_unlocked())

        if initial_source:
            for row in (self.recover.source_row, self.dashboard.source_row, self.chatcowork.source_row, self.flight.source_row):
                row.setText(initial_source)
        self.show_page(ARCHIVE)

    # -- gate -------------------------------------------------------------------------
    def set_gate(self, unlocked: bool) -> None:
        gate.set_unlocked(unlocked)
        self.apply_gate(unlocked)

    def apply_gate(self, unlocked: bool) -> None:
        self.flight.set_locked(not unlocked)
        self.nav_buttons[FLIGHT].set_locked(not unlocked)
        self.recover.set_flight_unlocked(unlocked)
        self.settings.sync_gate(unlocked)
        if unlocked:
            self.status_label.setText("Experimental features unlocked")

    # -- navigation --------------------------------------------------------------------
    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, b in enumerate(self.nav_buttons):
            b.setChecked(i == index)
        self.status_label.setText(PAGES[index] + (" — locked" if index == FLIGHT and self.flight.locked else ""))

    def _on_live_mode(self, mode: str) -> None:
        self.status_label.setText({"live": "Live — LIVE capture running", "audit": "Live — AUDIT dry run running"}.get(mode, "Live stopped"))

    def start_from_dashboard(self, source: str) -> None:
        if source:
            self.recover.source_row.setText(source)
        self.show_page(RECOVER)

    # -- output location --------------------------------------------------------------------
    def default_output(self, name: str) -> Path:
        base = Path(self._out_dir) if self._out_dir else archive_dir()
        return base / name

    def apply_output_dir(self, directory: str) -> None:
        """Settings -> Default output folder: update what the pages show as their defaults."""
        self._out_dir = directory.strip()
        self.recover.output_row.edit.setPlaceholderText(str(self.default_output("Cantata_recovery.md")))
        self.chatcowork.output_row.edit.setPlaceholderText(str(self.default_output("Cantata_chatcowork.md")))
        self.chatcowork.extract_row.edit.setPlaceholderText(str(self.default_output("Cantata_chats")))

    def apply_accent(self, name: str) -> None:
        theme.apply(QApplication.instance(), name)

    # -- recovery -----------------------------------------------------------------------
    def begin_recovery(self, bundle: dict) -> None:
        source = bundle["source"] or str(scanner.detect_default_source())
        output = bundle["output"] or str(self.default_output("Cantata_recovery.md"))
        if not Path(source).is_dir():
            QMessageBox.critical(self, "Invalid source", f"The source directory does not exist:\n{source}\n\n"
                                                        "Pick a valid Claude Code data directory on the Archive or Recover page.")
            return
        self.worker = RecoveryWorker(source, output, bundle["options"], self)
        self.worker.log.connect(self.recover.log)
        self.worker.progress.connect(self.recover.progress.update)
        self.worker.succeeded.connect(self.recovery_done)
        self.worker.failed.connect(self.recovery_failed)
        self.worker.cancelled.connect(lambda: (self.recover.log("Recovery cancelled - no report written.", "warn"), self.status_label.setText("Cancelled")))
        self.worker.finished.connect(self._worker_finished)
        self.recover.set_running(True)
        self.recover.log(f"Recovery started: {source} -> {output}", "ok")
        self.status_label.setText("Recovering…")
        self.worker.start()

    def cancel_recovery(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.recover.log("Cancel requested - finishing current step…", "warn")
            self.worker.cancel()

    def recovery_done(self, path: str) -> None:
        self.recover.log(f"Recovery complete: {path}", "ok")
        res = self.worker.result if self.worker is not None else None
        if res is not None:
            st = res.stats
            self.recover.log(f"{st['total_sessions']} sessions, {st['subagents']} subagents, {st['index_only']} index-only, "
                             f"{sum(st['redactions'].values()):,} redactions", "ok")
            if res.per_session_paths:
                self.recover.log(f"Per-session files: {len(res.per_session_paths)} in {Path(path).stem}_sessions/", "ok")
        self.status_label.setText(f"Report written: {Path(path).name}")
        self.report.load(path)
        if self.settings.auto_open.isChecked():
            self.show_page(REPORT)

    def recovery_failed(self, message: str) -> None:
        self.recover.log(f"Recovery failed: {message}", "error")
        self.status_label.setText("Recovery failed")
        QMessageBox.critical(self, "Recovery failed", message)

    def _worker_finished(self) -> None:
        self.recover.set_running(False)
        self.worker = None

    # -- chat & cowork ---------------------------------------------------------------------
    def begin_chatcowork(self, bundle: dict) -> None:
        bundle["source"] = bundle["source"] or str(scanner.detect_default_source())
        bundle["output"] = bundle["output"] or str(self.default_output("Cantata_chatcowork.md"))
        self._chat_worker = WebChatWorker(bundle, self)
        w = self._chat_worker
        w.log.connect(self.chatcowork.log)
        w.progress.connect(self.chatcowork.progress.update)
        w.succeeded.connect(self._chat_done)
        w.failed.connect(lambda m: (self.chatcowork.log(f"Scan failed: {m}", "error"), self.status_label.setText("Scan failed")))
        w.cancelled.connect(lambda: (self.chatcowork.log("Scan cancelled.", "warn"), self.status_label.setText("Cancelled")))
        w.finished.connect(lambda: (self.chatcowork.set_running(False), setattr(self, "_chat_worker", None)))
        self.chatcowork.set_running(True)
        self.chatcowork.log("Chat & Cowork scan started", "ok")
        self.status_label.setText("Scanning Chat & Cowork…")
        w.start()

    def cancel_chatcowork(self) -> None:
        if self._chat_worker is not None and self._chat_worker.isRunning():
            self._chat_worker.cancel()

    def _chat_done(self, path: str, stats: dict) -> None:
        self.chatcowork.log(f"Report written: {path}", "ok")
        self.chatcowork.finished(path, stats)
        self.status_label.setText(f"Chat & Cowork report: {Path(path).name}")

    def _open_report_file(self, path: str) -> None:
        if path and Path(path).exists():
            self.report.load(path)
            self.show_page(REPORT)

    def _open_folder(self, path: str) -> None:
        """Reveal the per-conversation extract folder in the OS file manager."""
        if path and Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path))))

    # -- lifecycle ------------------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        self.flight.shutdown()
        if self._chat_worker is not None and self._chat_worker.isRunning():
            self._chat_worker.cancel()
            self._chat_worker.wait(4000)
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(self, "Recovery running", "A recovery is still running. Cancel it and quit?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(4000)
        wait_all(5000)                       # dashboard scan / report load / flight analysis must not outlive the window
        event.accept()
