"""Flight & Live page  (GATED).

* **Time in flight (offline)** - derived request / tool / turn latencies from the transcripts.
* **Live capture** - AUDIT (red dot, dry run) and LIVE (green dot, writes JSONL streams) modes: transcript tail,
  loopback OTLP receiver for exact ``ttft_ms``, usage diffs, byte counters.

The whole page sits under a :class:`LockOverlay` until the soft gate is opened. Both engines live in ``core/`` and are
fully tested; the lock only hides the UI entry points.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                             QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import capture, flight, gate, recovery, scanner
from ..core.models import RecoveryOptions
from ..core.redaction import Redactor
from . import icons
from ._paths import archive_report, streamed_dir
from .widgets import ConsoleLog, LockOverlay, PathRow, SectionChip, StatCard, rich_tip
from .worker import CaptureWorker, FnWorker

MODE_COLORS = {"idle": "#6d675c", "audit": "#ff5d5d", "live": "#3fd68f"}


def _analyze(source: str, include_desktop: bool, otel_dir: str):
    bundle = recovery.collect_bundle(Path(source), RecoveryOptions(include_desktop=include_desktop, include_logs=False),
                                     lambda *a, **k: None, lambda *a, **k: None, lambda: False)
    fr = flight.compute_flight(bundle["sessions"], desktop=bundle.get("desktop"), global_info=bundle.get("global"),
                               otel_dir=Path(otel_dir) if otel_dir and Path(otel_dir).is_dir() else None)
    red = Redactor("standard", home_dirs=recovery.derive_homes(bundle["sessions"], Path(source)))
    return fr, flight.render_markdown(fr, red)


def _fmt(ms) -> str:
    if ms is None:
        return "—"
    ms = float(ms)
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


class FlightLivePage(QWidget):
    unlock_requested = pyqtSignal()
    mode_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: CaptureWorker | None = None
        self._fworker: FnWorker | None = None
        self._last_md = ""
        self.mode = "idle"

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.content = QWidget(scroll)
        lay = QVBoxLayout(self.content)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(12)
        head = QHBoxLayout()
        title = QLabel("Flight & Live", self.content)
        title.setProperty("h1", "true")
        head.addWidget(title)
        chip = QLabel("EXPERIMENTAL", self.content)
        chip.setProperty("chip", "amber")
        head.addWidget(chip)
        head.addStretch(1)
        self.dot = QLabel("●  idle", self.content)
        head.addWidget(self.dot)
        lay.addLayout(head)

        # ---------------- time in flight (offline) ----------------
        lay.addWidget(SectionChip("Time in flight — from transcripts", "blue"))
        blurb = QLabel("Claude Code logs no time-to-first-token. Cantata derives request-sent → first completed block (an upper bound on TTFT), "
                       "tool wall time and turn decomposition from line timestamps. Exact TTFT needs the OpenTelemetry receiver below.", self.content)
        blurb.setProperty("muted", "true")
        blurb.setWordWrap(True)
        lay.addWidget(blurb)
        self.source_row = PathRow("~/.claude", mode="dir", parent=self.content, title="Claude Code data directory")
        lay.addWidget(self.source_row)
        row = QHBoxLayout()
        self.chk_desktop = QCheckBox("include Cowork transcripts", self.content)
        self.chk_desktop.setChecked(True)
        self.analyze_btn = QPushButton(icons.icon("flight", "#1b0f0a", 16), " Analyze time in flight", self.content)
        self.analyze_btn.setProperty("primary", "true")
        self.analyze_btn.clicked.connect(self._analyze)
        self.save_btn = QPushButton(icons.icon("report", "#9a9284", 16), " Write report", self.content)
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save_report)
        row.addWidget(self.analyze_btn)
        row.addWidget(self.save_btn)
        row.addWidget(self.chk_desktop)
        row.addStretch(1)
        lay.addLayout(row)
        cards = QGridLayout()
        cards.setSpacing(12)
        self.f_cards = {k: StatCard(c) for k, c in (("n", "Responses"), ("p50", "In flight p50"), ("p90", "In flight p90"),
                                                     ("tools", "Tool calls"), ("turns", "Turns timed"))}
        for i, c in enumerate(self.f_cards.values()):
            cards.addWidget(c, 0, i)
        lay.addLayout(cards)
        self.table = QTableWidget(0, 6, self.content)
        self.table.setHorizontalHeaderLabels(["Model", "Responses", "In flight p50", "p90", "Stream p50", "Out tok/s"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)        # model names must be readable
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(150)
        lay.addWidget(self.table)
        self.f_status = QLabel("", self.content)
        self.f_status.setProperty("muted", "true")
        self.f_status.setWordWrap(True)
        lay.addWidget(self.f_status)

        # ---------------- live capture ----------------
        lay.addWidget(SectionChip("Live capture", "red"))
        blurb2 = QLabel("AUDIT (red) is a passive dry run — pollers run and count, nothing is written. LIVE (green) also appends JSONL streams "
                        "(tokens / flight / thinking / tools / usage / bytes / otel) and writes a session report on stop. Read-only on ~/.claude.", self.content)
        blurb2.setProperty("muted", "true")
        blurb2.setWordWrap(True)
        lay.addWidget(blurb2)
        self.out_row = PathRow(str(streamed_dir()), mode="dir", parent=self.content, title="Stream output directory")
        lay.addWidget(self.out_row)
        orow = QHBoxLayout()
        self.chk_otlp = QCheckBox("Listen for Claude Code telemetry (exact TTFT) on 127.0.0.1:", self.content)
        self.chk_otlp.setChecked(True)
        self.port = QSpinBox(self.content)
        self.port.setRange(1024, 65535)
        self.port.setValue(4318)
        self.port.setMaximumWidth(110)
        orow.addWidget(self.chk_otlp)
        orow.addWidget(self.port)
        copy_bash = QPushButton(icons.icon("copy", "#9a9284", 14), " bash env", self.content)
        copy_bash.clicked.connect(lambda: self._copy_env("bash"))
        copy_ps = QPushButton(icons.icon("copy", "#9a9284", 14), " PowerShell env", self.content)
        copy_ps.clicked.connect(lambda: self._copy_env("powershell"))
        orow.addWidget(copy_bash)
        orow.addWidget(copy_ps)
        orow.addStretch(1)
        lay.addLayout(orow)
        mrow = QHBoxLayout()
        self.audit_btn = QPushButton("●  AUDIT  (dry run)", self.content)
        self.audit_btn.setProperty("mode", "audit")
        self.audit_btn.clicked.connect(lambda: self._start(False))
        self.live_btn = QPushButton("●  LIVE  (write streams)", self.content)
        self.live_btn.setProperty("mode", "live")
        self.live_btn.clicked.connect(lambda: self._start(True))
        self.stop_btn = QPushButton(icons.icon("stop", "#ff5d5d", 16), " Stop", self.content)
        self.stop_btn.setProperty("danger", "true")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        for b in (self.audit_btn, self.live_btn, self.stop_btn):
            mrow.addWidget(b)
        mrow.addStretch(1)
        lay.addLayout(mrow)
        ro = QHBoxLayout()
        self.big_flight = QLabel("—", self.content)
        self.big_flight.setObjectName("BigReadout")
        self.big_ttft = QLabel("—", self.content)
        self.big_ttft.setObjectName("BigReadout")
        for big, cap in ((self.big_flight, "LAST TIME IN FLIGHT (line timestamps)"), (self.big_ttft, "LAST EXACT TTFT (OpenTelemetry)")):
            col = QVBoxLayout()
            col.addWidget(big)
            c = QLabel(cap, self.content)
            c.setObjectName("CardCaption")
            col.addWidget(c)
            ro.addLayout(col)
        ro.addStretch(1)
        lay.addLayout(ro)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.l_cards = {k: StatCard(k) for k in ("tokens", "flight", "thinking", "tools", "usage", "bytes", "otel", "desktop", "messages")}
        for i, c in enumerate(self.l_cards.values()):
            grid.addWidget(c, 0, i)
        lay.addLayout(grid)
        self.l_status = QLabel("", self.content)
        self.l_status.setProperty("muted", "true")
        self.l_status.setWordWrap(True)
        lay.addWidget(self.l_status)
        self.console = ConsoleLog(self.content)
        self.console.setMinimumHeight(170)
        lay.addWidget(self.console)
        scroll.setWidget(self.content)
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.addWidget(scroll)

        self.overlay = LockOverlay(self)
        self.overlay.unlocked.connect(self.unlock_requested.emit)
        self.set_locked(not gate.is_unlocked())
        self._set_dot("idle")

    # -- gate ---------------------------------------------------------------------
    @property
    def locked(self) -> bool:
        return self.overlay.isVisibleTo(self)

    def set_locked(self, locked: bool) -> None:
        if locked:
            self.shutdown()
        self.content.setEnabled(not locked)
        self.overlay.setVisible(locked)
        self.overlay.fit()
        if locked:
            self.overlay.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.overlay.fit()

    # -- offline analysis -------------------------------------------------------------
    def _src(self) -> str:
        return self.source_row.text() or str(scanner.detect_default_source())

    def _analyze(self) -> None:
        self.analyze_btn.setEnabled(False)
        self.f_status.setText("Analysing…")
        self._fworker = FnWorker(_analyze, self._src(), self.chk_desktop.isChecked(), self.out_row.text(), parent=self)
        self._fworker.result.connect(self._on_analysis)
        self._fworker.failed.connect(lambda m: (self.f_status.setText(f"Failed: {m}"), self.analyze_btn.setEnabled(True)))
        self._fworker.start()

    def _on_analysis(self, payload) -> None:
        fr, md = payload
        self._last_md = md
        self.analyze_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        h = fr.headline()
        ifl = h["in_flight"]
        self.f_cards["n"].set_value(f"{h['requests']:,}", "API responses analysed")
        self.f_cards["p50"].set_value(_fmt(ifl.get("p50")), "Derived: request-sent → first completed block")
        self.f_cards["p90"].set_value(_fmt(ifl.get("p90")))
        self.f_cards["tools"].set_value(f"{h['tool_calls']:,}")
        self.f_cards["turns"].set_value(f"{h['turns']:,}")
        self.table.setRowCount(0)
        for model, m in sorted(fr.by_model.items(), key=lambda kv: -kv[1]["n"]):
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = [model, str(m["n"]), _fmt(m["in_flight"].get("p50")), _fmt(m["in_flight"].get("p90")), _fmt(m["stream"].get("p50")),
                    f"{m['tps']['p50']:.0f}" if m["tps"].get("p50") else "—"]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        ex = f" • exact TTFT p50 {_fmt(fr.otel['ttft'].get('p50'))} from {fr.otel['n']} OTel span{'s' if fr.otel['n'] != 1 else ''}" if fr.otel else ""
        self.f_status.setText(f"{h['sessions']} sessions analysed{ex}. In-flight is an upper bound on true TTFT — see the report caveats.")

    def _save_report(self) -> None:
        if self._last_md:
            out = archive_report("Cantata_flight.md")
            out.write_text(self._last_md + "\n", encoding="utf-8")
            self.f_status.setText(f"Report written: {out}")

    # -- live -----------------------------------------------------------------------------
    def log(self, msg: str, level: str = "info") -> None:
        self.console.append_log(msg, level)

    def _copy_env(self, shell: str) -> None:
        QGuiApplication.clipboard().setText(capture.env_snippet(self.port.value(), shell))
        self.log(f"Copied {shell} environment for Claude Code → 127.0.0.1:{self.port.value()} (OTLP/HTTP JSON)", "ok")

    def _set_dot(self, mode: str) -> None:
        self.mode = mode
        label = {"idle": "idle", "audit": "AUDIT — dry run", "live": "LIVE — writing streams"}[mode]
        self.dot.setText(f'<span style="color:{MODE_COLORS[mode]}">●</span>  {label}')

    def _start(self, live: bool) -> None:
        if self.worker is not None or self.locked:
            return
        out = Path(self.out_row.text() or streamed_dir())
        port = self.port.value() if self.chk_otlp.isChecked() else None
        self.worker = CaptureWorker(out, live, Path(self._src()), port, parent=self)
        self.worker.log.connect(self.log)
        self.worker.counters.connect(self._on_counters)
        self.worker.stream.connect(self._on_stream)
        self.worker.failed.connect(lambda m: self.log(m, "error"))
        self.worker.finished.connect(self._worker_done)
        for b in (self.audit_btn, self.live_btn, self.out_row, self.source_row, self.chk_otlp, self.port):
            b.setEnabled(False)
        self.stop_btn.setEnabled(True)
        mode = "live" if live else "audit"
        self._set_dot(mode)
        self.log(f"{'LIVE' if live else 'AUDIT (dry run)'} started — tailing {self._src()}/projects", "ok")
        self.mode_changed.emit(mode)
        self.worker.start()

    def _on_counters(self, snap: dict) -> None:
        for k, card in self.l_cards.items():
            card.set_value(f"{snap.get(k, 0):,}")
        self.big_flight.setText(_fmt(snap.get("last_in_flight_ms")))
        self.big_ttft.setText(_fmt(snap.get("last_ttft_ms")))
        # Plain-language status: the raw counters alone read as "everything is
        # broken" even when the Desktop reader is working, because the Code-tab
        # transcripts are not on disk while a session is running.
        conv = snap.get("desktop_conversations", 0)
        msgs = snap.get("messages", 0)
        mt, md = snap.get("messages_transcript", 0), snap.get("messages_desktop", 0)
        bits = [f"{conv:,} Desktop conversation(s)", f"{msgs:,} message(s) captured "
                f"({mt:,} transcript · {md:,} Desktop)"]
        if not snap.get("desktop_reader"):
            bits.append("Desktop reader unavailable — pip install ccl_chromium_reader")
        if snap.get("tails") and mt == 0:
            bits.append("transcripts attached but silent: Claude Code flushes its JSONL at turn end, "
                        "so a running session shows nothing here until it writes")
        self.l_status.setText(" · ".join(bits))

    def _on_stream(self, stream: str, rec: dict) -> None:
        """Render live stream events — desktop conversations, regenerations, responses."""
        if stream != "desktop":
            return
        ev = rec.get("event", "")
        name = rec.get("name", "")[:44]
        if ev == "conversation_seen":
            self.log(f"desktop: + {name} ({rec.get('n_messages', 0)} msgs)"
                     + (" +summary" if rec.get("has_summary") else " (no summary)"), "ok")
        elif ev == "conversation_grew":
            self.log(f"desktop: ~ {name} ({rec.get('prev_n', 0)} -> {rec.get('n_messages', 0)} msgs)", "info")
        elif ev == "summary_appeared":
            self.log(f"desktop: SUM {name} — summary appeared after first sighting", "ok")
        elif ev == "response":
            branch = rec.get("branch", "")
            active = rec.get("is_active", True)
            stop = rec.get("stop_mode", "normal")
            idx = rec.get("index", "?")
            ttft = rec.get("ttft_ms")
            resp = rec.get("response_ms")
            parts = [f"desktop:   {name}"]
            parts.append(f"idx={idx}")
            if branch:
                parts.append("ACTIVE" if active else "SUPERSEDED")
            if stop != "normal":
                parts.append(stop)
            if ttft is not None:
                parts.append(f"ttft={ttft}ms")
            if resp is not None:
                parts.append(f"resp={resp}ms")
            if rec.get("replaces_created_at"):
                parts.append(f"replaces {rec['replaces_created_at'][:10]}")
            if rec.get("superseded_by_created_at"):
                parts.append(f"replaced by {rec['superseded_by_created_at'][:10]}")
            self.log(" ".join(parts), "info" if active else "warn")

    def _stop(self) -> None:
        if self.worker is not None:
            self.stop_btn.setEnabled(False)
            self.worker.stop()

    def _worker_done(self) -> None:
        w, self.worker = self.worker, None
        if w is not None and w.report_path:
            self.log(f"Session report written: {w.report_path}", "ok")
        for b in (self.audit_btn, self.live_btn, self.out_row, self.source_row, self.chk_otlp, self.port):
            b.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_dot("idle")
        self.log("Stopped.", "warn")
        self.mode_changed.emit("stopped")

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(4000)
