"""Time in flight (derived), Live capture engine, OTLP receiver and the soft gate."""

from __future__ import annotations

import gzip
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cantata.core import capture, flight, gate, recovery
from cantata.core.models import RecoveryOptions
from cantata.core.redaction import Redactor
from cantata.core.transcripts import parse_transcript


# --------------------------------------------------------------------------- derived time in flight


@pytest.fixture(scope="module")
def bundle(fx):
    return recovery.collect_bundle(fx["claude_path"], RecoveryOptions(include_desktop=False, include_logs=False), lambda *a: None, lambda *a: None, lambda: False)


def test_request_timings_match_the_fixture(fx):
    e = fx["expected"]
    main = parse_transcript(Path(fx["paths"]["main"]))
    r1, r2 = main.requests[0], main.requests[1]
    assert (r1.in_flight_ms, r1.stream_ms, r1.total_ms) == (e["req1_in_flight_ms"], e["req1_stream_ms"], e["req1_total_ms"])
    assert r2.in_flight_ms == e["req2_in_flight_ms"] and r1.blocks == 3 and r1.tool_uses == 1 and r2.speed == "fast"
    tools = {tc.name: tc for t in main.turns for m in t.messages for tc in m.tool_calls}
    assert tools["Bash"].wall_ms == e["tool_bash_wall_ms"] and tools["Task"].wall_ms == e["task_wall_ms"] and tools["Task"].reported_ms == 6400
    assert main.turns[0].reported_ms == e["turn1_reported_ms"] and main.turns[0].wall_ms >= 19_000


def test_compute_flight_aggregates(bundle):
    fr = flight.compute_flight(bundle["sessions"], global_info=bundle["global"])
    h = fr.headline()
    assert h["requests"] == 13 and h["tool_calls"] == 6 and h["sessions"] == 6                      # orphaned copy excluded
    assert {"claude-opus-4-5-20251101", "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"} == set(fr.by_model)
    assert fr.by_tool["Task"]["wall"]["max"] == 7000 and fr.by_tool["Bash"]["wall"]["p50"] == 2000
    assert fr.project_stats and fr.project_stats[0]["api_ms"] == 41234
    turn = next(t for t in fr.turns if t["turn"] == 1 and t["reported_ms"] == 19600)
    assert turn["model_ms"] > 0 and turn["tool_ms"] > 0 and turn["other_ms"] >= 0


def test_percentiles():
    assert flight.pct([1, 2, 3, 4], 0.5) == 2.5 and flight.pct([], 0.5) is None
    s = flight.summarize([10, None, 30])
    assert s["n"] == 2 and s["p50"] == 20 and flight.summarize([])["n"] == 0


def test_summarize_survives_junk_from_transcripts():
    """Transcript JSONL is hand-editable: one non-numeric or NaN value used to raise
    ValueError (blanking the whole Flight page) or silently poison every statistic."""
    nan = float("nan")
    s = flight.summarize([1.0, "abc", "", None, nan, float("inf"), True, 3.0])
    assert s["n"] == 2 and s["p50"] == 2.0 and s["mean"] == 2.0     # only 1.0 and 3.0 survive
    assert flight.summarize(["abc", ""])["n"] == 0
    assert flight.summarize([nan, float("inf")])["n"] == 0


def test_flight_renderers(bundle):
    fr = flight.compute_flight(bundle["sessions"], global_info=bundle["global"])
    md = flight.render_markdown(fr, Redactor("standard"))
    assert md.startswith("## Time in Flight") and "upper bound" in md and "### Per model" in md and "### Tools" in md and "longest" in md.lower()
    assert "/home/alice" not in md
    txt = flight.format_text(fr)
    assert "p50" in txt and "Exact TTFT requires" in txt
    assert json.loads(flight.to_json(fr))["headline"]["requests"] == 13


# --------------------------------------------------------------------------- OTLP receiver


def _post(port, path, body, ctype="application/json", encoding=None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"Content-Type": ctype}
    if encoding:
        headers["Content-Encoding"] = encoding
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    return urllib.request.urlopen(req, timeout=5)


SPAN = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
                           "scopeSpans": [{"spans": [{"name": "claude_code.llm_request", "spanId": "b" * 16, "traceId": "a" * 32,
                                                      "startTimeUnixNano": "1000000000", "endTimeUnixNano": "4200000000",
                                                      "attributes": [{"key": "ttft_ms", "value": {"intValue": "812"}},
                                                                     {"key": "duration_ms", "value": {"intValue": "3200"}},
                                                                     {"key": "model", "value": {"stringValue": "claude-opus-4-5"}},
                                                                     {"key": "session.id", "value": {"stringValue": "abcdef123456"}},
                                                                     {"key": "success", "value": {"boolValue": True}},
                                                                     {"key": "speed", "value": {"stringValue": "normal"}}]},
                                                     {"name": "claude_code.tool.execution", "attributes": [
                                                         {"key": "tool_name", "value": {"stringValue": "Bash"}}, {"key": "duration_ms", "value": {"intValue": "1500"}}]}]}]}]}


@pytest.fixture()
def engine(tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "projects" / "-tmp-p").mkdir(parents=True)
    eng = capture.CaptureEngine(tmp_path / "out", True, lambda *a: None, config_dir=cfg, otlp_port=0)
    yield eng
    eng.close()


def _read(out: Path, stream: str):
    return [json.loads(line) for f in sorted(out.glob(f"{stream}-*.jsonl")) for line in f.read_text(encoding="utf-8").splitlines()]


def test_receiver_ingests_exact_ttft(engine, tmp_path):
    assert _post(engine.receiver.port, "/v1/traces", SPAN).status == 200
    engine.tick()
    flights = _read(tmp_path / "out", "flight")
    req = next(e for e in flights if e["event"] == "llm_request")
    assert req["ttft_ms"] == 812 and req["duration_ms"] == 3200 and req["source"] == "otel" and req["model"] == "claude-opus-4-5" and req["success"] is True
    assert engine.last_ttft_ms == 812 and engine.counter_snapshot()["otlp_received"] == 1
    assert _read(tmp_path / "out", "tools")[0]["duration_ms"] == 1500


def test_receiver_handles_gzip_and_logs_drop_personal_attributes(engine, tmp_path):
    logs = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"timeUnixNano": "1700000000000000000", "attributes": [
        {"key": "event.name", "value": {"stringValue": "api_request"}}, {"key": "duration_ms", "value": {"intValue": "3100"}},
        {"key": "user.email", "value": {"stringValue": "secret@example.com"}}, {"key": "prompt", "value": {"stringValue": "my secret prompt"}}]}]}]}]}
    assert _post(engine.receiver.port, "/v1/logs", gzip.compress(json.dumps(logs).encode()), encoding="gzip").status == 200
    engine.tick()
    raw = json.dumps(_read(tmp_path / "out", "otel"))
    assert "api_request" in raw and "3100" in raw and "secret@example.com" not in raw and "my secret prompt" not in raw


def test_receiver_rejects_protobuf_unknown_paths_and_bad_json(engine):
    port = engine.receiver.port
    with pytest.raises(urllib.error.HTTPError) as e1:
        _post(port, "/v1/traces", b"\x0a\x00", ctype="application/x-protobuf")
    assert e1.value.code == 415 and b"http/json" in e1.value.read()
    with pytest.raises(urllib.error.HTTPError) as e2:
        _post(port, "/v1/unknown", {})
    assert e2.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e3:
        _post(port, "/v1/traces", b"{not json")
    assert e3.value.code == 400
    assert engine.receiver.unsupported == 1


def test_receiver_error_paths_never_reset_the_connection(engine):
    """Regression: an error branch that returned *without reading the request body* left
    unread bytes in the socket, so close() sent a TCP RST and the client raised
    ConnectionAbortedError (WinError 10053) instead of seeing the status. Observed on
    ~15% of requests to an unknown path before the body was drained first."""
    port = engine.receiver.port
    cases = [("/v1/traces", b"\x0a\x00", "application/x-protobuf", 415),
             ("/v1/unknown", b"{}", "application/json", 404),
             ("/v1/traces", b"{not json", "application/json", 400)]
    for _ in range(25):                       # it is a race, so repeat: one pass proves nothing
        for path, body, ctype, want in cases:
            try:
                _post(port, path, body, ctype=ctype)
                raise AssertionError(f"{path} should have returned {want}")
            except urllib.error.HTTPError as e:
                assert e.code == want
                assert e.read() is not None    # must not raise ConnectionAbortedError


def test_receiver_is_loopback_only():
    with pytest.raises(ValueError):
        capture.OtlpReceiver(0, host="0.0.0.0")
    with pytest.raises(ValueError):
        capture.OtlpReceiver(0, host="192.168.1.5")
    r = capture.OtlpReceiver(0)
    try:
        assert r.server.server_address[0] == "127.0.0.1"
    finally:
        r.stop()


def test_env_snippets():
    bash = capture.env_snippet(4318, "bash")
    assert "export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318" in bash and "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1" in bash and "http/json" in bash
    ps = capture.env_snippet(4400, "powershell")
    assert '$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4400"' in ps and "set " in capture.env_snippet(1, "cmd")


# --------------------------------------------------------------------------- transcript tailer


def _ts(s: float) -> str:
    d = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc) + timedelta(milliseconds=round(s * 1000))
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def _user(s, text="do it", sid="sess-1"):
    return {"type": "user", "timestamp": _ts(s), "sessionId": sid, "cwd": "/tmp/proj", "message": {"role": "user", "content": text}}


def _asst(s, mid, blocks, stop=None, sid="sess-1", usage=None, model="claude-opus-4-5"):
    return {"type": "assistant", "timestamp": _ts(s), "sessionId": sid, "message": {"id": mid, "model": model, "role": "assistant", "content": blocks,
            "stop_reason": stop, "usage": usage or {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": 100}}}


def _result(s, tid, sid="sess-1"):
    return {"type": "user", "timestamp": _ts(s), "sessionId": sid, "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def force(eng, n=2):
    for _ in range(n):
        eng._next_due = {k: 0.0 for k in eng._next_due}
        eng.tick()


def _append(path: Path, *recs, partial: str = ""):
    with open(path, "a", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
        fh.write(partial)


def test_tail_measures_time_in_flight(tmp_path):
    cfg = tmp_path / ".claude"
    f = cfg / "projects" / "-tmp-p" / "sess-1.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(_user(0, "old history")) + "\n", encoding="utf-8")
    eng = capture.CaptureEngine(tmp_path / "out", True, lambda *a: None, config_dir=cfg)
    try:
        force(eng)
        assert eng.counts["flight"] == 0                                            # pre-existing lines are not replayed (attach at EOF)
        _append(f, _user(0), _asst(2.4, "msg_1", [{"type": "thinking", "thinking": "hmm"}]))
        tool = _asst(3.3, "msg_1", [{"type": "tool_use", "id": "toolu_A", "name": "Bash", "input": {"command": "ls"}}], stop="tool_use")
        line = json.dumps(tool)
        _append(f, partial=line[:40])                                              # writer is mid-line
        force(eng)
        assert eng.counts["tools"] == 0
        _append(f, partial=line[40:] + "\n")
        _append(f, _result(5.3, "toolu_A"), {"type": "system", "subtype": "turn_duration", "timestamp": _ts(6), "sessionId": "sess-1", "durationMs": 6000})
        force(eng)
        flights = _read(tmp_path / "out", "flight")
        first = next(e for e in flights if e["event"] == "first_block")
        assert first["in_flight_line_ms"] == 2400 and first["model"] == "claude-opus-4-5"
        done = next(e for e in flights if e["event"] == "response_done")
        assert done["total_line_ms"] == 3300 and done["stream_line_ms"] == 900 and done["tps"] == pytest.approx(55.6, abs=0.1)
        assert next(e for e in flights if e["event"] == "turn_complete")["duration_ms"] == 6000
        tools = _read(tmp_path / "out", "tools")
        assert next(e for e in tools if e["event"] == "tool_result")["wall_line_ms"] == 2000
        assert len(_read(tmp_path / "out", "tokens")) == 1                          # usage repeated on 2 lines -> emitted once
        assert _read(tmp_path / "out", "thinking")[0]["text"] == "hmm"
        assert eng.last_in_flight_ms == 2400 and eng.counter_snapshot()["sessions"] == 1
    finally:
        eng.close()


def test_new_files_are_read_from_the_start_and_truncation_reattaches(tmp_path):
    cfg = tmp_path / ".claude"
    proj = cfg / "projects" / "-tmp-p"
    proj.mkdir(parents=True)
    eng = capture.CaptureEngine(tmp_path / "out", False, lambda *a: None, config_dir=cfg)
    try:
        force(eng)
        new = proj / "sess-2.jsonl"
        _append(new, _user(0, sid="sess-2"), _asst(1.0, "msg_9", [{"type": "text", "text": "hi"}], stop="end_turn", sid="sess-2"))
        force(eng, 3)
        assert eng.counts["flight"] >= 2 and eng.counts["tokens"] == 1
        new.write_text("", encoding="utf-8")                                        # rotated / truncated
        force(eng)
        _append(new, _user(0, sid="sess-2"), _asst(0.5, "msg_10", [{"type": "text", "text": "again"}], stop="end_turn", sid="sess-2"))
        force(eng, 3)
        assert eng.counts["tokens"] == 2
    finally:
        eng.close()


def test_audit_mode_counts_but_writes_nothing(tmp_path):
    cfg = tmp_path / ".claude"
    f = cfg / "projects" / "-tmp-p" / "s.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("", encoding="utf-8")
    eng = capture.CaptureEngine(tmp_path / "out", False, lambda *a: None, config_dir=cfg)
    try:
        force(eng)
        _append(f, _user(0), _asst(1, "msg_1", [{"type": "text", "text": "x"}], stop="end_turn"))
        force(eng)
        assert eng.counts["flight"] > 0 and not (tmp_path / "out").exists()
        eng.write_session_md(tmp_path / "s.md")
        assert "AUDIT (dry run, nothing written)" in (tmp_path / "s.md").read_text(encoding="utf-8")
    finally:
        eng.close()


def test_usage_poller_reports_changes_only(tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "stats-cache.json").write_text(json.dumps({"totalSessions": 1, "modelUsage": {"m": {"inputTokens": 10, "outputTokens": 5}}}), encoding="utf-8")
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {"/tmp/p": {"lastSessionId": "s1", "lastCost": 0.1, "lastDuration": 1000}}}), encoding="utf-8")
    (cfg / "history.jsonl").write_text('{"display":"a"}\n', encoding="utf-8")
    eng = capture.CaptureEngine(tmp_path / "out", True, lambda *a: None, config_dir=cfg)
    try:
        force(eng)
        assert eng.counts["usage"] == 0                                             # baseline primed silently
        time.sleep(0.01)
        (cfg / "stats-cache.json").write_text(json.dumps({"totalSessions": 2, "modelUsage": {"m": {"inputTokens": 40, "outputTokens": 15}}}), encoding="utf-8")
        (tmp_path / ".claude.json").write_text(json.dumps({"projects": {"/tmp/p": {"lastSessionId": "s2", "lastCost": 0.4, "lastDuration": 9000}}}), encoding="utf-8")
        with open(cfg / "history.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"display":"b"}\n{"display":"c"}\n')
        import os
        for p in (cfg / "stats-cache.json", tmp_path / ".claude.json"):
            os.utime(p, (time.time() + 5, time.time() + 5))
        force(eng)
        rows = _read(tmp_path / "out", "usage")
        by = {r["source"]: r for r in rows}
        assert by["stats-cache.json"]["token_delta_by_model"] == {"m": 40} and by[".claude.json"]["last_cost_usd"] == 0.4 and by["history.jsonl"]["new_prompts"] == 2
    finally:
        eng.close()


def test_iface_bytes_never_crashes():
    v = capture.iface_bytes()
    assert v is None or (len(v) == 2 and all(isinstance(x, int) for x in v))


def test_session_report_and_streams_merge_back_into_flight(tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "projects").mkdir(parents=True)
    eng = capture.CaptureEngine(tmp_path / "out", True, lambda *a: None, config_dir=cfg, otlp_port=0)
    try:
        _post(eng.receiver.port, "/v1/traces", SPAN)
        force(eng)
        md = eng.write_session_md(tmp_path / "out" / "session.md").read_text(encoding="utf-8")
        assert "Exact TTFT (OpenTelemetry)" in md and "812 ms" in md and "no message text" in md
    finally:
        eng.close()
    merged = flight.load_otel_ttft(tmp_path / "out")
    assert merged["n"] == 1 and merged["ttft"]["p50"] == 812 and "claude-opus-4-5" in merged["by_model"]
    assert "Exact TTFT (OpenTelemetry capture)" in flight.render_markdown(flight.FlightReport(otel=merged), Redactor("standard"))


def test_daemon_runs_and_writes_report(tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "projects").mkdir(parents=True)
    lines = []
    rep = capture.run_capture_daemon(cfg, tmp_path / "out", live=True, duration=0.3, printer=lines.append)
    assert rep is not None and rep.exists() and any("Capture LIVE" in l for l in lines)
    assert capture.run_capture_daemon(cfg, tmp_path / "out2", live=False, duration=0.2, printer=lambda *_: None) is None


# --------------------------------------------------------------------------- the soft gate


def test_gate_defaults_and_persistence(tmp_path):
    assert not gate.is_unlocked()
    gate.set_unlocked(True)
    assert gate.is_unlocked() and json.loads(gate.state_file().read_text(encoding="utf-8"))[gate.STATE_KEY] is True
    gate.set_unlocked(False)
    assert not gate.is_unlocked()


@pytest.mark.parametrize("val,expected", [("1", True), ("true", True), ("ON", True), ("unlock", True), ("0", False), ("", False), ("nope", False)])
def test_gate_env_switch(monkeypatch, val, expected):
    monkeypatch.setenv(gate.ENV_VAR, val)
    assert gate.is_unlocked() is expected


def test_gate_survives_corrupt_or_readonly_state(tmp_path, monkeypatch):
    gate.state_dir().mkdir(parents=True)
    gate.state_file().write_text("{corrupt", encoding="utf-8")
    assert gate.read_state() == {} and not gate.is_unlocked()
    monkeypatch.setenv("CANTATA_HOME", str(tmp_path / "file_not_dir"))
    (tmp_path / "file_not_dir").write_text("x", encoding="utf-8")
    gate.set_unlocked(True)                                                          # must not raise
    assert not gate.is_unlocked()


def test_settings_roundtrip():
    gate.set_setting("accent", "Mint")
    assert gate.get_setting("accent") == "Mint" and gate.get_setting("missing", 7) == 7


def test_live_streams_never_contain_hard_secrets_in_thinking(tmp_path):
    from make_fixture import SECRETS
    cfg = tmp_path / ".claude"
    f = cfg / "projects" / "-tmp-p" / "s.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("", encoding="utf-8")
    eng = capture.CaptureEngine(tmp_path / "out", True, lambda *a: None, config_dir=cfg)
    try:
        force(eng)
        _append(f, _user(0), _asst(1, "msg_1", [{"type": "thinking", "thinking": f"I should use {SECRETS['anthropic_key']} and {SECRETS['github_pat']}"}], stop="end_turn"))
        force(eng)
    finally:
        eng.close()
    raw = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "out").glob("*.jsonl"))
    assert SECRETS["anthropic_key"] not in raw and SECRETS["github_pat"] not in raw and "[ANTHROPIC-KEY]" in raw
