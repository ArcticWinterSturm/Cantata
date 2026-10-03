"""Live capture engine  (GATED: see ``gate.py``).

Two modes, same engine:

* **AUDIT** (``live=False``)  - passive dry run: every poller runs and counts, *nothing is written*.
* **LIVE**  (``live=True``)   - also appends JSONL streams to ``out_dir`` and writes a session report on close:
  ``tokens`` / ``flight`` / ``thinking`` / ``tools`` / ``usage`` / ``bytes`` / ``otel``.

Everything is READ-ONLY on ``~/.claude``; streams land in the chosen output directory.

Sources
-------
* **Transcript tail** - every ``projects/**/*.jsonl`` (and Cowork copies) is tailed; sessions that already exist
  when the engine starts are read from EOF, files that appear later from byte 0.  Time in flight is measured two
  ways: from the line timestamps (``*_line_ms``) and from wall-clock *observation* (``*_seen_ms``).
* **OTLP receiver** - a **loopback-only** HTTP server that accepts Claude Code's OpenTelemetry export
  (``OTEL_EXPORTER_OTLP_PROTOCOL=http/json``) and records the *exact* ``ttft_ms`` of ``claude_code.llm_request``
  spans - the only place time-to-first-token exists.
* **Usage** - diffs of ``stats-cache.json``, ``~/.claude.json`` (per-project last cost / duration) and ``history.jsonl``.
* **Bytes** - NIC byte counters (no process spawn on Linux/Windows).

Threading: the HTTP thread only enqueues; :meth:`CaptureEngine.tick` (any single thread) drains and writes.
"""

from __future__ import annotations

import gzip
import json
import os
import queue
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import paths
from .flight import summarize
from .redaction import Redactor
from .transcripts import content_text, ms_between, parse_ts

POLL_S = {"transcripts": 0.08, "attach": 2.0, "usage": 1.0, "bytes": 1.0, "desktop": 3.0}
STREAMS = ("tokens", "flight", "thinking", "tools", "usage", "bytes", "otel", "desktop")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
ERR_LOG_INTERVAL = 10.0
OTEL_ENV = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_TRACES_EXPORT_INTERVAL": "1000",
    "OTEL_LOGS_EXPORT_INTERVAL": "1000",
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def wall_iso(ms: Optional[int] = None) -> str:
    d = datetime.fromtimestamp((ms or now_ms()) / 1000)
    return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"


# --------------------------------------------------------------------------- #
# OTLP / JSON helpers + receiver
# --------------------------------------------------------------------------- #

def env_snippet(port: int, shell: str = "bash") -> str:
    """Environment that makes Claude Code export telemetry to the local receiver."""
    env = dict(OTEL_ENV, OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{port}")
    if shell in ("powershell", "pwsh"):
        return "\n".join(f'$env:{k} = "{v}"' for k, v in env.items()) + "\nclaude"
    if shell == "cmd":
        return "\n".join(f"set {k}={v}" for k, v in env.items()) + "\nclaude"
    return "\n".join(f"export {k}={v}" for k, v in env.items()) + "\nclaude"


def _anyvalue(v: Any) -> Any:
    if not isinstance(v, dict):
        return v
    for key in ("stringValue", "boolValue"):
        if key in v:
            return v[key]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "arrayValue" in v:
        return [_anyvalue(x) for x in (v["arrayValue"] or {}).get("values", [])]
    if "kvlistValue" in v:
        return _attrs((v["kvlistValue"] or {}).get("values", []))
    return None


def _attrs(items: Any) -> dict:
    out: dict = {}
    for it in items or []:
        if isinstance(it, dict) and "key" in it:
            out[str(it["key"])] = _anyvalue(it.get("value"))
    return out


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _span_rows(payload: dict) -> list[dict]:
    rows = []
    for rs in payload.get("resourceSpans", []) or []:
        res = _attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans", []) or []:
            for sp in ss.get("spans", []) or []:
                a = _attrs(sp.get("attributes"))
                start, end = _num(sp.get("startTimeUnixNano")), _num(sp.get("endTimeUnixNano"))
                rows.append({"name": str(sp.get("name", "")), "attrs": a, "res": res,
                             "span_id": sp.get("spanId", ""), "parent": sp.get("parentSpanId", ""),
                             "wall_ms": int((end - start) / 1e6) if start and end and end >= start else None,
                             "start_ms": int(start / 1e6) if start else None})
    return rows


def _log_rows(payload: dict) -> list[dict]:
    rows = []
    for rl in payload.get("resourceLogs", []) or []:
        res = _attrs((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs", []) or []:
            for rec in sl.get("logRecords", []) or []:
                a = _attrs(rec.get("attributes"))
                rows.append({"attrs": a, "res": res, "body": _anyvalue(rec.get("body")),
                             "time_ms": int(float(rec["timeUnixNano"]) / 1e6) if rec.get("timeUnixNano") else None})
    return rows


class _Handler(BaseHTTPRequestHandler):
    server_version = "CantataOTLP/1"

    def log_message(self, *args: Any) -> None:         # silence default stderr logging
        pass

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        # Always drain the request body FIRST. Returning early without reading it leaves
        # bytes in the socket buffer, and the handler's close() then sends a TCP RST
        # instead of a clean FIN — the client sees ConnectionAbortedError (WinError 10053)
        # instead of the 404. Reproduced at ~15% of requests before this fix.
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 32 * 1024 * 1024)) if length else b""
        signal = {"/v1/traces": "traces", "/v1/logs": "logs", "/v1/metrics": "metrics"}.get(self.path.split("?")[0])
        if signal is None:
            return self._reply(404, {"error": "unknown OTLP path"})
        if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                return self._reply(400, {"error": "bad gzip"})
        if "json" not in (self.headers.get("Content-Type") or "").lower():
            self.server.unsupported += 1                                # type: ignore[attr-defined]
            return self._reply(415, {"error": "Cantata speaks OTLP/HTTP JSON only: set OTEL_EXPORTER_OTLP_PROTOCOL=http/json"})
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            return self._reply(400, {"error": "bad json"})
        self.server.q.put((signal, payload))                            # type: ignore[attr-defined]
        self.server.received += 1                                       # type: ignore[attr-defined]
        self._reply(200, {"partialSuccess": {}})


class OtlpReceiver:
    """Loopback-only OTLP/HTTP-JSON receiver. Refuses any non-loopback bind address."""

    def __init__(self, port: int = 4318, host: str = "127.0.0.1"):
        if host not in LOOPBACK:
            raise ValueError("The OTLP receiver only binds to loopback (127.0.0.1).")
        self.q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.server = ThreadingHTTPServer((host, port), _Handler)
        self.server.q = self.q                                          # type: ignore[attr-defined]
        self.server.received = 0                                        # type: ignore[attr-defined]
        self.server.unsupported = 0                                     # type: ignore[attr-defined]
        self.server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def received(self) -> int:
        return self.server.received                                     # type: ignore[attr-defined]

    @property
    def unsupported(self) -> int:
        return self.server.unsupported                                  # type: ignore[attr-defined]

    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="cantata-otlp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Safe before ``start()`` too: ``shutdown()`` blocks forever if ``serve_forever()`` never ran."""
        try:
            if self._thread is not None:
                self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None


# --------------------------------------------------------------------------- #
# network byte counters
# --------------------------------------------------------------------------- #
#
# These live in `iface.py` because the counter MUST NOT spawn a process.
# Cantata runs under pythonw.exe (no console), and a console-less process that
# spawns a console app forces Windows to allocate a console for it — creating a
# conhost.exe that flashes a window and dies. At the 1 s `bytes` cadence that
# was one flash per second. `iface.iface_bytes()` reads the interface table via
# the iphlpapi syscall instead.
from .iface import iface_bytes                                       # noqa: E402


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #

class CaptureEngine:
    def __init__(self, out_dir: Path, live: bool, log: Callable[[str, str], None], *, config_dir: Optional[Path] = None,
                 extra_projects: Iterable[Path] = (), otlp_port: Optional[int] = None):
        self.out_dir = Path(out_dir)
        self.live = live
        self.log = log
        self.config_dir = Path(config_dir) if config_dir else paths.claude_config_dir()
        self.roots = [self.config_dir / "projects"] + [Path(p) for p in extra_projects]
        self.started_ms = now_ms()
        self.counts = {s: 0 for s in STREAMS}
        self._fh: dict = {}
        self.tails: dict[str, dict] = {}
        self._first_scan = True
        self._seen_msgs: dict[str, set] = defaultdict(set)
        self.pending_calls: dict[str, dict] = {}
        self._next_due = {k: 0.0 for k in POLL_S}
        self._last_err: dict = {}
        self.events: dict[str, list] = {"flight": [], "tools": [], "tokens": []}
        self.session_ids: set[str] = set()
        self.last_in_flight_ms: Optional[int] = None
        self.last_in_flight_model: str = ""
        self.last_ttft_ms: Optional[int] = None
        self.prev_bytes: Optional[tuple[int, int]] = None
        self.prev_bytes_t: float = 0.0
        self._usage_armed = False
        self._sc_mtime = 0.0
        self._sc_models: dict = {}
        self._gj_mtime = 0.0
        self._gj_proj: dict = {}
        self._hist_size = 0
        self._hard = Redactor("none")                 # streams are raw local captures, but hard secrets never land in them
        # Desktop IndexedDB state: fingerprints of every conversation seen, and
        # the per-conversation provenance record (first sighting, summary arrival).
        self.desktop: dict[str, dict] = {}
        self.desktop_convs: dict[str, dict] = {}       # cached conversation objects for the report
        self._desktop_emitted: set[str] = set()       # "uuid:n:written_at" keys already emitted
        self._desktop_stop_modes: dict[str, int] = {}  # stop-mode census from streaming
        self.msg_total = 0                            # every human/assistant message seen, either source
        self.msg_by_source: dict[str, int] = {"transcript": 0, "desktop": 0}
        self.desktop_ok = False                       # False = reader unavailable; report why, don't fail
        self.desktop_error = ""
        self._desktop_warned = False
        self.receiver: Optional[OtlpReceiver] = None
        if otlp_port is not None:
            self.receiver = OtlpReceiver(otlp_port)
            self.receiver.start()
            self.log(f"OTLP receiver listening on 127.0.0.1:{self.receiver.port} (HTTP/JSON)", "ok")

    # -- output -----------------------------------------------------------------
    def _err(self, stream: str, msg: str) -> None:
        now = time.monotonic()
        key = (stream, msg[:160])
        if now - self._last_err.get(key, -1e9) >= ERR_LOG_INTERVAL:
            self._last_err[key] = now
            self.log(f"{stream}: {msg}", "warn")

    def _w(self, stream: str, rec: dict) -> None:
        self.counts[stream] += 1
        rec.setdefault("ts_ms", now_ms())
        rec.setdefault("wall", wall_iso(rec["ts_ms"]))
        if stream in self.events and len(self.events[stream]) < 5000:
            self.events[stream].append(rec)
        if not self.live:
            return
        day = datetime.now().strftime("%Y%m%d")
        fh = self._fh.get(stream)
        if fh is None or getattr(fh, "_day", None) != day:
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
            self.out_dir.mkdir(parents=True, exist_ok=True)
            fh = open(self.out_dir / f"{stream}-{day}.jsonl", "a", encoding="utf-8")
            fh._day = day                                              # type: ignore[attr-defined]
            self._fh[stream] = fh
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def _flush(self) -> None:
        for fh in self._fh.values():
            try:
                fh.flush()
            except Exception:
                pass

    # -- transcript tailing -----------------------------------------------------
    def scan(self) -> int:
        added = 0
        first = self._first_scan
        self._first_scan = False
        for root in self.roots:
            if not root.is_dir():
                continue
            try:
                candidates = list(root.rglob("*.jsonl"))
            except OSError:
                continue
            for f in candidates:
                p = str(f)
                if p in self.tails or ".orphaned-" in f.name:
                    continue
                try:
                    size = f.stat().st_size
                    fh = open(f, "r", encoding="utf-8", errors="replace")
                except OSError:
                    continue
                offset = size if first else 0
                fh.seek(offset)
                self.tails[p] = {"fh": fh, "offset": offset, "sent_ts": "", "sent_seen": None, "await_first": False,
                                 "first_ts": "", "first_seen": None, "model": "", "sub": "subagents" in f.parts}
                added += 1
                self.log(f"attached: {f.name}" + (" (from start)" if offset == 0 else " (tailing from EOF)"),
                         "ok" if offset == 0 else "info")
        return added

    def poll_tails(self) -> None:
        for p, t in list(self.tails.items()):
            fh = t["fh"]
            try:
                if os.path.getsize(p) < t["offset"]:
                    fh.close()
                    del self.tails[p]
                    self.log(f"transcript rotated/truncated, will re-attach: {Path(p).name}", "warn")
                    continue
            except OSError:
                fh.close()
                del self.tails[p]
                self.log(f"transcript removed: {Path(p).name}", "warn")
                continue
            try:
                got = False
                while True:
                    pos = fh.tell()
                    raw = fh.readline()
                    if not raw:
                        break
                    if not raw.endswith("\n"):                          # partial line being written - retry next tick
                        fh.seek(pos)
                        break
                    got = True
                    self._line(p, t, raw)
                if got:
                    t["offset"] = fh.tell()
            except Exception as exc:
                try:
                    fh.close()
                except Exception:
                    pass
                self.tails.pop(p, None)
                self._err("transcripts", f"tail lost for {Path(p).name}: {exc}")

    def _line(self, path: str, t: dict, raw: str) -> None:
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        typ = d.get("type")
        ts = d.get("timestamp") or ""
        seen = now_ms()
        sid = str(d.get("sessionId") or Path(path).stem)
        if sid:
            self.session_ids.add(sid)
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        content = msg.get("content")
        if typ in ("user", "assistant"):
            self.msg_total += 1
            self.msg_by_source["transcript"] += 1

        if typ == "user":
            blocks = content if isinstance(content, list) else []
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                for b in results:
                    call = self.pending_calls.pop(str(b.get("tool_use_id") or ""), None)
                    row = {"event": "tool_result", "session": sid[:8], "tool_use_id": str(b.get("tool_use_id") or "")[:14],
                           "is_error": bool(b.get("is_error")), "name": (call or {}).get("name", "?")}
                    if call:
                        row["wall_line_ms"] = ms_between(call["ts"], ts)
                        row["wall_seen_ms"] = seen - call["seen"]
                    self._w("tools", row)
            else:
                text = content_text(content)
                if text.startswith(("[Request interrupted", "<local-command", "<system-reminder")) or d.get("isMeta"):
                    return
                self._w("flight", {"event": "prompt", "session": sid[:8], "project": Path(d.get("cwd") or "").name,
                                   "chars": len(text), "sidechain": bool(d.get("isSidechain"))})
            t.update(sent_ts=ts, sent_seen=seen, await_first=True)
        elif typ == "assistant":
            mid = str(msg.get("id") or d.get("uuid"))
            model = str(msg.get("model") or "")
            first_of_msg = mid not in self._seen_msgs[path]
            self._seen_msgs[path].add(mid)
            if len(self._seen_msgs[path]) > 4000:
                self._seen_msgs[path] = set(list(self._seen_msgs[path])[-1500:])
            if t["await_first"] and first_of_msg and model != "<synthetic>":
                t["await_first"] = False
                t["first_ts"], t["first_seen"], t["model"] = ts, seen, model
                inflight = ms_between(t["sent_ts"], ts)
                self.last_in_flight_ms, self.last_in_flight_model = inflight, model
                self._w("flight", {"event": "first_block", "session": sid[:8], "model": model,
                                   "in_flight_line_ms": inflight, "in_flight_seen_ms": (seen - t["sent_seen"]) if t["sent_seen"] else None,
                                   "sidechain": bool(d.get("isSidechain"))})
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "thinking" and (b.get("thinking") or b.get("text")):
                    self._w("thinking", {"session": sid[:8], "model": model, "text": self._hard.hard(str(b.get("thinking") or b.get("text")))[:300]})
                elif b.get("type") in ("tool_use", "server_tool_use"):
                    cid = str(b.get("id") or "")
                    self.pending_calls[cid] = {"name": str(b.get("name") or ""), "ts": ts, "seen": seen}
                    if len(self.pending_calls) > 2000:
                        self.pending_calls.pop(next(iter(self.pending_calls)))
                    self._w("tools", {"event": "tool_use", "session": sid[:8], "name": str(b.get("name") or ""), "tool_use_id": cid[:14]})
            if first_of_msg and isinstance(msg.get("usage"), dict) and model != "<synthetic>":
                u = msg["usage"]
                self._w("tokens", {"session": sid[:8], "model": model, "message_id": mid[-10:],
                                   "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                                   "cache_read_tokens": u.get("cache_read_input_tokens"),
                                   "cache_write_tokens": u.get("cache_creation_input_tokens"), "speed": u.get("speed"),
                                   "stop_reason": msg.get("stop_reason")})
            if msg.get("stop_reason") and model != "<synthetic>":
                u = msg.get("usage") or {}
                stream = ms_between(t["first_ts"], ts) if t["first_ts"] else None
                out_tok = u.get("output_tokens") or 0
                self._w("flight", {"event": "response_done", "session": sid[:8], "model": model, "stop_reason": msg["stop_reason"],
                                   "total_line_ms": ms_between(t["sent_ts"], ts) if t["sent_ts"] else None,
                                   "stream_line_ms": stream, "output_tokens": out_tok,
                                   "tps": round(out_tok / (stream / 1000), 1) if stream and stream > 0 and out_tok else None})
        elif typ == "system" and d.get("subtype") == "turn_duration":
            self._w("flight", {"event": "turn_complete", "session": sid[:8], "duration_ms": d.get("durationMs")})

    # -- OTLP -----------------------------------------------------------------------
    def _drain_otlp(self) -> None:
        if not self.receiver:
            return
        for _ in range(200):
            try:
                signal, payload = self.receiver.q.get_nowait()
            except queue.Empty:
                break
            try:
                self.ingest_otlp(signal, payload)
            except Exception as exc:
                self._err("otel", f"could not ingest {signal}: {exc}")
        if self.receiver.unsupported:
            self._err("otel", f"{self.receiver.unsupported} non-JSON export(s) rejected - set OTEL_EXPORTER_OTLP_PROTOCOL=http/json")

    def ingest_otlp(self, signal: str, payload: dict) -> None:
        if signal == "traces":
            for sp in _span_rows(payload):
                a, name = sp["attrs"], sp["name"]
                sess = str(a.get("session.id") or sp["res"].get("session.id") or "")[:8]
                if name == "claude_code.llm_request":
                    ttft = _num(a.get("ttft_ms"))
                    row = {"event": "llm_request", "source": "otel", "session": sess, "model": a.get("model"),
                           "ttft_ms": int(ttft) if ttft is not None else None,
                           "first_content_ms": a.get("first_content_ms"), "duration_ms": a.get("duration_ms", sp["wall_ms"]),
                           "attempt": a.get("attempt"), "success": a.get("success"), "speed": a.get("speed"),
                           "effort": a.get("effort"), "stop_reason": a.get("stop_reason"), "input_tokens": a.get("input_tokens"),
                           "output_tokens": a.get("output_tokens"), "cache_read_tokens": a.get("cache_read_tokens"),
                           "cache_creation_tokens": a.get("cache_creation_tokens"), "request_id": a.get("request_id"),
                           "query_source": a.get("llm_request.context")}
                    if ttft is not None:
                        self.last_ttft_ms = int(ttft)
                    self._w("flight", row)
                elif name == "claude_code.interaction":
                    self._w("flight", {"event": "interaction", "source": "otel", "session": sess,
                                       "duration_ms": a.get("interaction.duration_ms", sp["wall_ms"])})
                elif name in ("claude_code.tool", "claude_code.tool.execution", "claude_code.tool.blocked_on_user"):
                    self._w("tools", {"event": name.split("claude_code.")[1], "source": "otel", "session": sess,
                                      "name": a.get("tool_name"), "duration_ms": a.get("duration_ms", sp["wall_ms"]),
                                      "success": a.get("success"), "decision": a.get("decision")})
                elif name == "claude_code.hook":
                    self._w("tools", {"event": "hook", "source": "otel", "session": sess, "name": a.get("hook_name"),
                                      "duration_ms": a.get("duration_ms", sp["wall_ms"])})
                else:
                    self._w("otel", {"signal": "traces", "span": name, "wall_ms": sp["wall_ms"]})
        elif signal == "logs":
            for lr in _log_rows(payload):
                a = lr["attrs"]
                ev = str(a.get("event.name") or lr["body"] or "")
                keep = {k: v for k, v in a.items() if k not in ("prompt", "response", "tool_input", "new_context", "user.email",
                                                                 "user.id", "organization.id", "user.account_uuid", "user.account_id")}
                self._w("otel", {"signal": "logs", "event": ev, "attrs": keep})
        elif signal == "metrics":
            n_points = 0
            for rm in payload.get("resourceMetrics", []) or []:
                for sm in rm.get("scopeMetrics", []) or []:
                    for m in sm.get("metrics", []) or []:
                        n_points += 1
                        self._w("otel", {"signal": "metrics", "metric": m.get("name")})

    # -- usage / bytes ------------------------------------------------------------------
    def poll_usage(self) -> None:
        sc = self.config_dir / "stats-cache.json"
        try:
            st = sc.stat()
            if st.st_mtime != self._sc_mtime:
                data = json.loads(sc.read_text(encoding="utf-8", errors="replace"))
                models = {m: sum(int(v.get(k, 0) or 0) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
                          for m, v in (data.get("modelUsage") or {}).items() if isinstance(v, dict)}
                if self._usage_armed:
                    deltas = {m: t - self._sc_models.get(m, 0) for m, t in models.items() if t != self._sc_models.get(m, 0)}
                    if deltas:
                        self._w("usage", {"source": "stats-cache.json", "total_sessions": data.get("totalSessions"),
                                          "total_messages": data.get("totalMessages"), "token_delta_by_model": deltas})
                self._sc_models, self._sc_mtime = models, st.st_mtime
        except (OSError, ValueError):
            pass
        gj = paths.claude_global_json(self.config_dir)
        if gj:
            try:
                st = gj.stat()
                if st.st_mtime != self._gj_mtime:
                    data = json.loads(gj.read_text(encoding="utf-8", errors="replace"))
                    proj = {pp: (i.get("lastSessionId"), i.get("lastCost"), i.get("lastDuration"), i.get("lastAPIDuration"))
                            for pp, i in (data.get("projects") or {}).items() if isinstance(i, dict)}
                    if self._usage_armed:
                        for pp, cur in proj.items():
                            if cur != self._gj_proj.get(pp):
                                self._w("usage", {"source": ".claude.json", "project": Path(pp).name, "last_session": str(cur[0])[:8],
                                                  "last_cost_usd": cur[1], "last_duration_ms": cur[2], "last_api_duration_ms": cur[3]})
                    self._gj_proj, self._gj_mtime = proj, st.st_mtime
            except (OSError, ValueError):
                pass
        hist = self.config_dir / "history.jsonl"
        try:
            size = hist.stat().st_size
            if self._usage_armed and size > self._hist_size:
                with open(hist, "rb") as fh:
                    fh.seek(self._hist_size)
                    added = [ln for ln in fh.read().splitlines() if ln.strip()]
                self._w("usage", {"source": "history.jsonl", "new_prompts": len(added)})
            self._hist_size = size
        except OSError:
            pass
        if not self._usage_armed:
            self._usage_armed = True
            self.log(f"usage baseline primed ({len(self._sc_models)} model(s), {len(self._gj_proj)} project(s)) - only changes are captured", "info")

    def poll_bytes(self) -> None:
        cur = iface_bytes()
        if cur is None:
            return
        t = time.monotonic()
        if self.prev_bytes is not None and t > self.prev_bytes_t:
            dt_s = t - self.prev_bytes_t
            rx, tx = cur[0] - self.prev_bytes[0], cur[1] - self.prev_bytes[1]
            if rx > 0 or tx > 0:
                self._w("bytes", {"rx_bps": int(rx / dt_s), "tx_bps": int(tx / dt_s), "rx_total": cur[0], "tx_total": cur[1]})
        self.prev_bytes, self.prev_bytes_t = cur, t

    def poll_desktop(self) -> None:
        """Sweep Claude Desktop's IndexedDB for new/updated conversations.

        This is the half of LIVE that captures what you *flick through*: opening
        a conversation in the Claude Desktop sidebar makes the client write its
        record, so an old conversation is picked up without any new turn.

        Change detection is (message count, writtenAt) per conversation — `trees`
        gets a new record on every write, so comparing the richest record is
        enough. Provenance (`first_seen_at`, whether a summary existed at first
        sighting, when the summary appeared) is tracked here and survives because
        the engine holds it for the whole session.
        """
        if not self.desktop_ok:
            try:
                from . import desktop_live
                self._desktop_live = desktop_live
                self.desktop_ok = True
            except Exception as e:
                self.desktop_error = f"{type(e).__name__}: {e}"
                if not self._desktop_warned:
                    self._desktop_warned = True
                    self.log("desktop: IndexedDB reader unavailable "
                             "(pip install ccl_chromium_reader) — Desktop conversations "
                             f"will not be captured ({self.desktop_error})", "warn")
                return
        dl = self._desktop_live

        try:
            convs = dl.scan_all(on_error=lambda m: self._err("desktop", m))
        except Exception as e:
            self._err("desktop", f"{type(e).__name__}: {e}")
            return

        now_iso = wall_iso(now_ms())
        for conv in convs:
            fp = dl.conversation_fingerprint(conv)
            uid = fp["uuid"]
            if not uid:
                continue
            prov = self.desktop.get(uid)
            if prov is None:
                prov = {
                    "uuid": uid,
                    "first_seen_at": now_iso,
                    "first_seen_summary": fp["has_summary"],
                    "messages_first_seen": fp["n_messages"],
                    "summary_first_at": None,
                    "summary_at_first_sighting": fp["has_summary"],
                    "sightings": 0,
                    "last_n": 0,
                    "last_written": 0,
                    "name": fp["name"],
                    "model": fp["model"],
                    "source": fp["source"],
                }
                self.desktop[uid] = prov
                self.msg_total += fp["n_messages"]
                self.msg_by_source["desktop"] += fp["n_messages"]
                self._w("desktop", {"event": "conversation_seen", "uuid": uid,
                                    "name": fp["name"], "model": fp["model"],
                                    "messages": fp["n_messages"],
                                    "summary_present": fp["has_summary"],
                                    "source": fp["source"],
                                    "also_in": conv.get("also_in") or []})
                self.log(f"desktop: + {fp['name'][:56]} ({fp['n_messages']} msgs)"
                         + (" +summary" if fp["has_summary"] else " (no summary)"), "ok")
            else:
                prov["sightings"] += 1
                if fp["n_messages"] > prov["last_n"]:
                    self.msg_total += fp["n_messages"] - prov["last_n"]
                    self.msg_by_source["desktop"] += fp["n_messages"] - prov["last_n"]
                    self._w("desktop", {"event": "conversation_grew", "uuid": uid,
                                        "name": fp["name"], "from_messages": prov["last_n"],
                                        "to_messages": fp["n_messages"], "source": fp["source"]})
                    self.log(f"desktop: ~ {fp['name'][:56]} "
                             f"({prov['last_n']} -> {fp['n_messages']} msgs)", "info")
                # The summary is written by Claude a little while AFTER the tree
                # is first persisted — record exactly when it lands.
                if fp["has_summary"] and not prov["first_seen_summary"] and not prov["summary_first_at"]:
                    prov["summary_first_at"] = now_iso
                    self._w("desktop", {"event": "summary_appeared", "uuid": uid,
                                        "name": fp["name"], "first_seen_at": prov["first_seen_at"]})
                    self.log(f"desktop: SUM {fp['name'][:52]} — summary appeared "
                             f"after first sighting", "ok")
            prov["last_n"] = max(prov["last_n"], fp["n_messages"])
            prov["last_written"] = max(prov["last_written"], fp["written_at"])
            prov["has_summary"] = fp["has_summary"]
            prov["summary"] = fp["summary"]
            prov["messages"] = fp["n_messages"]
            prov["is_archived"] = fp["is_archived"]
            prov["thinking_mode"] = fp["thinking_mode"]

            # Emit per-response timing + stop classification only when THIS
            # conversation actually changed, so the stream stays a change-log.
            # The key MUST include the uuid: without it every conversation
            # shares one namespace and all but the first are silently dropped.
            # It also carries n_branches + leaf, so an EDIT (which can leave the
            # message count unchanged) still re-emits.
            emitted_key = (f"{uid}:{fp['n_messages']}:{fp['written_at']}:"
                           f"{fp.get('n_branches', 0)}:{fp.get('leaf', '')}")
            if emitted_key not in self._desktop_emitted:
                self._desktop_emitted.add(emitted_key)
                rows = dl.conversation_rows(conv)
                for r in rows:
                    if r.get("sender") != "assistant":
                        continue
                    # `was` is the true stop classification; `branch` is orthogonal
                    # (active / superseded). A superseded hard_refusal is still a
                    # hard_refusal in the census.
                    mode = r.get("was") or r.get("stop_mode") or "normal"
                    self._desktop_stop_modes[mode] = self._desktop_stop_modes.get(mode, 0) + 1
                    if mode != "normal" or r.get("ttft_ms") is not None:
                        self._w("desktop", {"event": "response", "uuid": uid,
                                            "name": fp["name"], "model": fp["model"],
                                            "index": r.get("index"), "stop_mode": mode,
                                            "stop_reason": r.get("stop_reason"),
                                            "branch": r.get("branch") or "",
                                            "is_active": r.get("is_active", True),
                                            "message_uuid": r.get("message_uuid") or "",
                                            "replaced_by": r.get("replaced_by") or "",
                                            "replaces": r.get("replaces") or "",
                                            "replaces_created_at": r.get("replaces_created_at") or "",
                                            "superseded_by_created_at": r.get("superseded_by_created_at") or "",
                                            "input_mode": r.get("input_mode"),
                                            "ttft_ms": r.get("ttft_ms"),
                                            "thinking_ms": r.get("thinking_ms"),
                                            "first_tool_ms": r.get("first_tool_ms"),
                                            "tool_wall_ms": r.get("tool_wall_ms"),
                                            "stream_gaps_ms": r.get("stream_gaps_ms"),
                                            "response_ms": r.get("response_ms"),
                                            "n_text": r.get("n_text"), "n_thinking": r.get("n_thinking"),
                                            "n_tool_use": r.get("n_tool_use")})
            # Cache the conversation for the session report.
            self.desktop_convs[uid] = conv

    def desktop_stats(self) -> dict:
        """Summary of Desktop capture for the session report."""
        provs = list(self.desktop.values())
        modes: dict[str, int] = dict(self._desktop_stop_modes)
        models: dict[str, int] = {}
        for p in provs:
            models[p.get("model") or "—"] = models.get(p.get("model") or "—", 0) + 1
        return {
            "conversations": len(provs),
            "no_summary_at_first_sighting": sum(1 for p in provs if not p["first_seen_summary"]),
            "summary_arrived_later": sum(1 for p in provs if p.get("summary_first_at")),
            "stop_modes": modes,
            "models": models,
            "archived": sum(1 for p in provs if p.get("is_archived")),
            "stores": len(self._desktop_live.indexeddb_stores()) if self.desktop_ok else 0,
        }

    # -- scheduler ---------------------------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()
        self._drain_otlp()
        for name, fn in (("transcripts", self.poll_tails), ("attach", self.scan),
                         ("usage", self.poll_usage), ("bytes", self.poll_bytes),
                         ("desktop", self.poll_desktop)):
            if now >= self._next_due.get(name, 0.0):
                self._next_due[name] = now + POLL_S[name]
                try:
                    fn()
                except Exception as exc:
                    self._err(name, f"{type(exc).__name__}: {exc}")
        self._flush()

    def close(self) -> None:
        if self.receiver:
            self.receiver.stop()
        for t in self.tails.values():
            try:
                t["fh"].close()
            except Exception:
                pass
        self.tails.clear()
        self._flush()
        for fh in self._fh.values():
            try:
                fh.close()
            except Exception:
                pass
        self._fh.clear()

    def counter_snapshot(self) -> dict:
        snap = dict(self.counts)
        snap.update(tails=len(self.tails), sessions=len(self.session_ids), uptime_s=int((now_ms() - self.started_ms) / 1000),
                    last_in_flight_ms=self.last_in_flight_ms, last_in_flight_model=self.last_in_flight_model,
                    last_ttft_ms=self.last_ttft_ms, otlp_received=self.receiver.received if self.receiver else 0,
                    otlp_port=self.receiver.port if self.receiver else None, live=self.live,
                    desktop_conversations=len(self.desktop),
                    desktop_reader=self.desktop_ok,
                    messages=self.msg_total,
                    messages_transcript=self.msg_by_source["transcript"],
                    messages_desktop=self.msg_by_source["desktop"])
        return snap

    # -- session report --------------------------------------------------------------------------
    def write_session_md(self, path: Path) -> Path:
        from .report import dur
        fl = self.events["flight"]
        first = [e for e in fl if e.get("event") == "first_block"]
        otel = [e for e in fl if e.get("event") == "llm_request" and e.get("ttft_ms") is not None]
        done = [e for e in fl if e.get("event") == "response_done"]
        L = [f"# Cantata live session — {wall_iso(self.started_ms)[:16]}", "",
             f"- **Mode**: {'LIVE (streams written)' if self.live else 'AUDIT (dry run, nothing written)'}",
             f"- **Duration**: {dur(now_ms() - self.started_ms)} · **transcripts tailed**: {len(self.tails)} · **sessions seen**: {len(self.session_ids)}",
            f"- **Events**: " + ", ".join(f"{k} {v}" for k, v in self.counts.items() if v), "",
             f"- **Messages captured**: {self.msg_total:,} "
             f"({self.msg_by_source['transcript']:,} from Code-tab transcripts · "
             f"{self.msg_by_source['desktop']:,} from Claude Desktop conversations)", ""]
        if first:
            s = summarize(e["in_flight_line_ms"] for e in first)
            s2 = summarize(e["in_flight_seen_ms"] for e in first)
            L += ["## Time in flight (transcript tail)", "",
                  f"- {len(first)} request(s) · line-timestamp p50 **{dur(int(s['p50']))}** p90 {dur(int(s['p90']))} max {dur(int(s['max']))}"
                  if s.get("n") else "- no measurable requests", ""]
            if s2.get("n"):
                L += [f"- wall-clock observed p50 {dur(int(s2['p50']))} p90 {dur(int(s2['p90']))} "
                      "(includes Cantata's own polling latency ≤ 80 ms)", ""]
        if self.tails and not self.msg_by_source["transcript"]:
            L += ["## Why the transcript streams are empty", "",
                  f"Cantata attached to **{len(self.tails)}** Claude Code transcript file(s) but none wrote a "
                  "line while LIVE was running. That is expected, not a fault:",
                  "",
                  "- Claude Code appends to `~/.claude/projects/<project>/<session>.jsonl` **when a turn "
                  "completes**, not while the model is streaming. A session that was idle for the whole "
                  "capture window produces no lines.",
                  "- **Claude Desktop's Code tab (CCD) does not write JSONL to disk at all on this install.** "
                  "The only local trace of a Code-tab session is its metadata wrapper under "
                  "`local-agent-mode-sessions/` and `claude-code-sessions/` (session id, title, cwd, model, "
                  "permission mode) — no message content, no timestamps. Cantata cannot reconstruct those "
                  "conversations from disk.",
                  "- Desktop **chat** conversations are unaffected and are captured via IndexedDB — see the "
                  "Claude Desktop section below.",
                  "",
                  "To get exact timings for a Code-tab session, start Claude Code with the OTLP environment "
                  "below so the telemetry receiver sees `claude_code.llm_request` spans.", ""]
        if otel:
            s = summarize(e["ttft_ms"] for e in otel)
            L += ["## Exact TTFT (OpenTelemetry)", "", f"- {len(otel)} `llm_request` span(s): p50 **{dur(int(s['p50']))}** p90 {dur(int(s['p90']))} "
                  f"max {dur(int(s['max']))}", ""]
        if done:
            tps = [e["tps"] for e in done if e.get("tps")]
            if tps:
                L += [f"## Generation speed\n\n- median **{sorted(tps)[len(tps) // 2]:.0f} tok/s** over {len(tps)} multi-block response(s)", ""]
        tools = [e for e in self.events["tools"] if e.get("event") == "tool_result" and e.get("wall_line_ms") is not None]
        if tools:
            agg: dict[str, list] = defaultdict(list)
            for e in tools:
                agg[e["name"]].append(e["wall_line_ms"])
            L += ["## Tool wall time", "", "| Tool | Calls | p50 | max |", "|---|---|---|---|"]
            for name, v in sorted(agg.items(), key=lambda kv: -max(kv[1])):
                s = summarize(v)
                L.append(f"| `{name}` | {s['n']} | {dur(int(s['p50']))} | {dur(int(s['max']))} |")
            L.append("")
        toks = self.events["tokens"]
        if toks:
            L += ["## Tokens", "", f"- {len(toks)} response(s): in {sum(int(e.get('input_tokens') or 0) for e in toks):,} · "
                  f"out {sum(int(e.get('output_tokens') or 0) for e in toks):,} · "
                  f"cache-read {sum(int(e.get('cache_read_tokens') or 0) for e in toks):,}", ""]

        # -- Claude Desktop (IndexedDB) ------------------------------------------
        if self.desktop:
            st = self.desktop_stats()
            L += ["## Claude Desktop conversations", "",
                  f"- **{st['conversations']}** conversation(s) captured across **{st['stores']}** store(s) "
                  f"(Desktop + browser profiles)",
                  f"- **{st['no_summary_at_first_sighting']}** had no summary at first sighting; "
                  f"**{st['summary_arrived_later']}** received one while LIVE was running",
                  f"- Archived: {st['archived']}", ""]
            if st["models"]:
                L += ["| Model | Conversations |", "|---|---|"]
                for m, n in sorted(st["models"].items(), key=lambda kv: -kv[1]):
                    L.append(f"| `{m}` | {n} |")
                L.append("")
            if st["stop_modes"]:
                L += ["### Stop modes across captured conversations", "",
                      "`stop_reason` alone cannot classify a stop — two of these carry a normal "
                      "`end_turn`. See `desktop_live.STOP_MODES`.", "",
                      "| Stop mode | Responses |", "|---|---|"]
                for m, n in sorted(st["stop_modes"].items(), key=lambda kv: -kv[1]):
                    L.append(f"| `{m}` | {n} |")
                L.append("")
            late = [p for p in self.desktop.values() if p.get("summary_first_at")]
            if late:
                L += ["### Summary provenance", "",
                      "Claude writes the conversation summary a little while AFTER the tree is first "
                      "persisted. These were seen first with no summary, then gained one:", "",
                      "| Conversation | First seen | Summary appeared | Messages at first sighting |",
                      "|---|---|---|---|"]
                for p in late[:40]:
                    L.append(f"| {p['name'][:44]} | {p['first_seen_at'][11:19]} | "
                             f"{p['summary_first_at'][11:19]} | {p['messages_first_seen']} |")
                L.append("")
            L += ["| Conversation | Model | Msgs | Summary | Store |", "|---|---|---|---|---|"]
            for p in sorted(self.desktop.values(), key=lambda x: -(x.get("messages") or 0))[:60]:
                L.append(f"| {p['name'][:44]} | `{p.get('model') or '—'}` | {p.get('messages')} | "
                         f"{'yes' if p.get('has_summary') else '**no**'} | {p.get('source', '')} |")
            L.append("")
        elif not self.desktop_ok and self.desktop_error:
            L += ["## Claude Desktop conversations", "",
                  f"- **Not captured** — IndexedDB reader unavailable: `{self.desktop_error}`. "
                  "Install with `pip install \"git+https://github.com/cclgroupltd/ccl_chromium_reader.git\"`.", ""]

        L += ["_Streams were written to the output directory as JSONL; this report contains no message text._", ""]
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(L), encoding="utf-8")
        return path


def run_capture_daemon(config_dir: Path, out_dir: Path, *, live: bool = True, otlp_port: Optional[int] = None,
                       duration: Optional[float] = None, printer: Callable[[str], None] = print) -> Optional[Path]:
    """Blocking CLI loop. Ctrl+C (or ``duration`` seconds) stops it and writes the session report."""
    def log(msg: str, level: str = "info") -> None:
        printer(f"[{time.strftime('%H:%M:%S')}] {'!' if level == 'warn' else '*'} {msg}")

    eng = CaptureEngine(out_dir, live, log, config_dir=config_dir, otlp_port=otlp_port)
    printer(f"Capture {'LIVE' if live else 'AUDIT (dry run)'} on {config_dir} — Ctrl+C to stop")
    if otlp_port is not None:
        printer("Start Claude Code with:\n" + env_snippet(eng.receiver.port if eng.receiver else otlp_port, "powershell" if sys.platform == "win32" else "bash"))
    t_end = time.monotonic() + duration if duration else None
    last_print = ""
    try:
        while t_end is None or time.monotonic() < t_end:
            eng.tick()
            snap = eng.counter_snapshot()
            line = (f"flight {snap['flight']} tokens {snap['tokens']} tools {snap['tools']} usage {snap['usage']} "
                    f"otel-spans {snap['otlp_received']} in-flight {snap['last_in_flight_ms']}ms ttft {snap['last_ttft_ms']}ms")
            if line != last_print and int(time.time()) % 2 == 0:
                last_print = line
                printer(f"  {line}")
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        eng.close()
    if live:
        report = Path(out_dir) / f"session-{time.strftime('%Y%m%d-%H%M%S')}.md"
        eng.write_session_md(report)
        printer(f"Session report: {report}")
        return report
    return None
