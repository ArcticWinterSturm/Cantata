"""Background workers: recovery, Chat & Cowork scan, generic function, Live capture."""

from __future__ import annotations

import time
import traceback
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from .. import __version__
from ..core.models import RecoveryCancelled, RecoveryOptions
from ..core.recovery import run_recovery


class RecoveryWorker(QThread):
    log = pyqtSignal(str, str)
    progress = pyqtSignal(float, str)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, source: str, output: str, options: RecoveryOptions, parent=None):
        super().__init__(parent)
        self.source, self.output, self.options = source, output, options
        self._cancel = False
        self.result = None

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread entry
        try:
            self.result = run_recovery(self.source, self.output, self.options,
                                       log=lambda m, lvl="info": self.log.emit(m, lvl),
                                       progress=lambda f, label="": self.progress.emit(f, label), cancel=lambda: self._cancel)
            self.cancelled.emit() if self._cancel else self.succeeded.emit(str(self.result.output_path))
        except RecoveryCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.log.emit(traceback.format_exc(limit=6), "error")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


_RUNNING: set = set()            # every live FnWorker, so the window can join them all before it is destroyed


def wait_all(timeout_ms: int = 5000) -> bool:
    """Join every running :class:`FnWorker`. A QThread destroyed while running aborts the whole process."""
    deadline = time.monotonic() + timeout_ms / 1000
    for w in list(_RUNNING):
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            w.wait(remaining)
        except RuntimeError:          # C++ object already gone
            _RUNNING.discard(w)
    return not _RUNNING


class FnWorker(QThread):
    """Runs a plain callable off the UI thread."""

    result = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)
        self.fn, self.args = fn, args
        self.finished.connect(lambda: _RUNNING.discard(self))

    def start(self, *a) -> None:  # noqa: D102 - register *before* the thread runs so wait_all() can never miss it
        _RUNNING.add(self)
        super().start(*a)

    def run(self) -> None:  # noqa: D102
        try:
            self.result.emit(self.fn(*self.args))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class WebChatWorker(QThread):
    """Runs the Chat & Cowork scan and writes its report."""

    log = pyqtSignal(str, str)
    progress = pyqtSignal(float, str)
    succeeded = pyqtSignal(str, dict)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, bundle: dict, parent=None):
        super().__init__(parent)
        self.bundle = bundle
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D102
        from ..core.redaction import Redactor
        from ..core.webchat import run_webchat, extract_indexeddb_conversations, write_indexeddb_conversations
        from ..core.webchat_report import build_webchat_report
        b = self.bundle
        # The scan owns the first 90% of the bar; the report write + IndexedDB rebuild own
        # the rest. `run_webchat` reports its own 0..1, so rescale it - otherwise it hits
        # 100% and then the rebuild drops the bar back to 96%, where it used to stick.
        def scan_progress(f: float, label: str = "") -> None:
            self.progress.emit(round(min(max(f, 0.0), 1.0) * 0.90, 4), label)

        try:
            rep = run_webchat(Path(b["source"]), [Path(p) for p in b["exports"]], desktop_dirs=[Path(p) for p in b["desktop_dirs"]] or None,
                              scan_browser=b["scan_browser"], scan_transcripts=b["scan_transcripts"],
                              extract_dir=Path(b["extract_dir"]) if b.get("extract_dir") else None, redaction_level=b["level"],
                              include_thinking=b.get("include_thinking", True),
                              log=lambda m, lvl="info": self.log.emit(m, lvl),
                              progress=scan_progress, cancel=lambda: self._cancel)
            if self._cancel:
                self.progress.emit(1.0, "Cancelled")
                self.cancelled.emit()
                return
            red = Redactor(b["level"], home_dirs=[Path.home()])
            out = Path(b["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            self.progress.emit(0.90, "Writing report…")
            out.write_text(build_webchat_report(rep, red, version=__version__), encoding="utf-8")

            # Rebuild the full IndexedDB conversation bodies next to the export
            # reconstruction. The CLI has done this behind --idb-out; the GUI never
            # called it, so "Extract to" silently produced zero files for anyone whose
            # only local history was IndexedDB (i.e. no export archives).
            stats = rep.stats()
            if b.get("extract_dir"):
                extract_dir = Path(b["extract_dir"])
                extract_dir.mkdir(parents=True, exist_ok=True)      # always exists -> "Open folder" works
                stats = dict(stats, extract_dir=str(extract_dir))
                # Export-archive reconstructions land in export_NN_*/conversations/ - remember them
                # so "Open folder" can point at the folder that actually holds conversation files.
                exp_dirs = [str(p.parent) for p in sorted(extract_dir.glob("export_*/conversations")) if p.is_dir()]
                if exp_dirs:
                    stats = dict(stats, export_dir=exp_dirs[0], export_dirs=exp_dirs)
                try:
                    self.progress.emit(0.92, "Rebuilding IndexedDB conversations…")
                    convs = extract_indexeddb_conversations(
                        log=lambda m, lvl="info": self.log.emit(m, lvl),
                        progress=lambda f, label="": self.progress.emit(0.92 + 0.07 * min(max(f, 0.0), 1.0),
                                                                        label or "Rebuilding IndexedDB conversations…"),
                        cancel=lambda: self._cancel)
                    if convs:
                        idb_dir = extract_dir / "indexeddb"
                        written = write_indexeddb_conversations(convs, idb_dir, redact=red.apply, cancel=lambda: self._cancel)
                        stats = dict(stats, indexeddb_conversations=len(convs),
                                     indexeddb_files=len(written), indexeddb_dir=str(idb_dir))
                        self.log.emit(f"IndexedDB: rebuilt {len(written)} file(s) from {len(convs)} conversation(s) "
                                      f"in {idb_dir.name}/", "ok")
                    else:
                        # Nothing to rebuild is a normal outcome (a signed-out account switch empties
                        # the cache) - say so instead of leaving a silent 96%-looking bar.
                        stats = dict(stats, indexeddb_conversations=0, indexeddb_files=0, indexeddb_dir="")
                        self.log.emit("IndexedDB: no cached conversation bodies to rebuild "
                                      "(the claude.ai store holds none for the signed-in account)", "warn")
                except RecoveryCancelled:
                    raise
                except Exception as exc:
                    self.log.emit(f"IndexedDB rebuild skipped: {type(exc).__name__}: {exc}", "warn")
            self.progress.emit(1.0, "Done")
            self.succeeded.emit(str(out), stats)
        except RecoveryCancelled:
            self.progress.emit(1.0, "Cancelled")
            self.cancelled.emit()
        except Exception as exc:
            self.log.emit(traceback.format_exc(limit=6), "error")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class CaptureWorker(QThread):
    """Drives the Live engine's ``tick()`` off the GUI thread (GATED feature)."""

    log = pyqtSignal(str, str)
    counters = pyqtSignal(dict)
    stream = pyqtSignal(str, dict)   # (stream_name, record) — live event feed
    failed = pyqtSignal(str)

    def __init__(self, out_dir: Path, live: bool, config_dir: Path, otlp_port=None, parent=None):
        super().__init__(parent)
        self.out_dir, self.live, self.config_dir, self.otlp_port = Path(out_dir), live, Path(config_dir), otlp_port
        self._stop = False
        self.engine = None
        self.report_path = None

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        from ..core.capture import CaptureEngine
        try:
            self.engine = CaptureEngine(self.out_dir, self.live, lambda m, lvl="info": self.log.emit(m, lvl),
                                        config_dir=self.config_dir, otlp_port=self.otlp_port)
        except OSError as exc:
            self.failed.emit(f"Could not start: {exc}")
            return
        # Forward every stream event to the UI (desktop, tokens, flight, …)
        _orig_w = self.engine._w
        def _w(stream, rec):
            _orig_w(stream, rec)
            self.stream.emit(stream, rec)
        self.engine._w = _w
        last = 0.0
        try:
            while not self._stop:
                self.engine.tick()
                now = time.monotonic()
                if now - last >= 0.25:
                    last = now
                    self.counters.emit(self.engine.counter_snapshot())
                self.msleep(40)
        except Exception as exc:
            self.log.emit(traceback.format_exc(limit=4), "error")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.counters.emit(self.engine.counter_snapshot())
            self.engine.close()
            if self.live:
                try:
                    self.report_path = self.engine.write_session_md(self.out_dir / f"session-{time.strftime('%Y%m%d-%H%M%S')}.md")
                except OSError as exc:
                    self.log.emit(f"Could not write session report: {exc}", "warn")
