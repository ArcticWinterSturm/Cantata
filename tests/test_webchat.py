"""Chat & Cowork: Desktop catalog, Cowork audit, browser footprint, export triage + reconstruction."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from cantata.core import desktop, paths, reflect, webchat
from cantata.core.redaction import Redactor
from cantata.core.webchat_report import build_webchat_report
from conftest import all_text


def _run(fx, wc, tmp_path, **kw):
    kw.setdefault("scan_indexeddb", False)
    return webchat.run_webchat(fx["claude_path"], [Path(wc["zip"]), Path(wc["single"])], desktop_dirs=[wc["desktop_root"]],
                               browsers=[("Chromium", Path(wc["browser_root"]))], extract_dir=tmp_path / "chats", **kw)


# --------------------------------------------------------------------------- streaming JSON array


@pytest.mark.parametrize("chunk", [1, 7, 64, 1 << 20])
def test_iter_json_array_any_chunking(chunk):
    objs = [{"a": "]["}, {"b": [1, 2, {"c": "x,y]"}]}, {"n": None}, {"s": "é\u2028\"q\""}]
    text = "  \ufeff" + json.dumps(objs, indent=2)
    assert list(webchat.iter_json_array(io.StringIO(text), chunk=chunk)) == objs


def test_iter_json_array_edge_cases():
    assert list(webchat.iter_json_array(io.StringIO("[]"))) == []
    assert list(webchat.iter_json_array(io.StringIO(""))) == []
    assert list(webchat.iter_json_array(io.StringIO('{"not": "an array"}'))) == []
    assert list(webchat.iter_json_array(io.StringIO('[{"a":1},'))) == [{"a": 1}]              # truncated file: keep what parsed


# --------------------------------------------------------------------------- desktop


def test_desktop_scan_inventory(wc):
    info = desktop.scan_desktop([wc["desktop_root"]])
    st = info.stats()
    assert info.found and st["cowork_sessions"] == 2 and st["code_tab_sessions"] == 2 and st["audit_logs"] == 1
    assert st["cowork_transcripts"] == 2 and st["memory_files"] == 3 and st["cost_usd"] == pytest.approx(8.64)
    assert {m["scope"] for m in info.memory} == {"account", "project"}                # incl. spaces/<projectId>/memory
    cw = next(s for s in info.sessions if s.local_id == wc["ids"]["cowork_local"])
    assert cw.cli_session_id == wc["ids"]["cowork_cli"] and cw.audit["results"] == 1 and cw.audit["hmac_fields"] and cw.audit["rate_limit_events"] == 1
    assert cw.audit["duration_api_ms"] == 512000 and cw.audit["num_turns"] == 88 and cw.model == "claude-opus-4-5" and cw.uploads == 1 and cw.outputs == 1
    assert {tf.kind for tf in cw.transcripts} == {"cowork", "cowork-subagent"}
    assert cw.extra["system_prompt_chars"] > 5000 and cw.scheduled_task == "etsy-daily-metrics"
    assert info.has_device_id and any(v["name"] == "vm_bundles" for v in info.vm_entries)


def test_desktop_flavours():
    assert desktop._flavour(Path("/x/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude")).startswith("MSIX")
    assert "3P" in desktop._flavour(Path("/x/Claude-3p")) and desktop._flavour(Path("/x/Claude")) == "standard"


def test_audit_key_is_never_read(wc, monkeypatch):
    opened = []
    real_open = open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    desktop.scan_desktop([wc["desktop_root"]])
    assert not any(".audit-key" in p for p in opened)


def test_missing_desktop_is_not_an_error(tmp_path):
    info = desktop.scan_desktop([tmp_path / "nothing"])
    assert not info.found and info.warnings


def test_cowork_transcripts_are_recovered_by_recovery(fx, wc, tmp_path):
    from cantata.core.models import RecoveryOptions
    from cantata.core.recovery import run_recovery
    res = run_recovery(fx["claude_path"], tmp_path / "r.md", RecoveryOptions(desktop_dirs=(wc["desktop_root"],), per_sessions=False))
    cow = [s for s in res.sessions if s.kind.startswith("cowork")]
    assert {s.kind for s in cow} == {"cowork", "cowork-subagent"} and res.stats["desktop_sessions"] == 2
    main = next(s for s in cow if s.kind == "cowork")
    assert main.origin == "desktop-cowork" and main.display_title.startswith("Review customer persona") and main.project_path == "/sessions/charming-adoring-darwin"
    code = next(s for s in res.sessions if s.session_id == fx["sessions"]["main"])
    assert code.origin == "desktop-code" and code.extra["wrapper"]["title"] == "Login bug (desktop)"         # wrapper linked to ~/.claude transcript
    text = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "## Claude Desktop" in text and "Cowork audit logs" in text and wc["pii"]["email"] not in text and "Last API time" not in text


# --------------------------------------------------------------------------- exports


def test_zip_export_triage(wc, tmp_path):
    art = webchat.triage_export(Path(wc["zip"]), red=Redactor("standard"), extract_dir=tmp_path / "x")
    assert art.kind == "claude-export-zip" and art.conversations == 2 and art.messages == 4 and art.projects == 1 and art.project_docs == 1
    assert art.attachments == 1 and art.tool_uses == 1 and art.models["claude-opus-4-5"] == 1 and len(art.sha256) == 64
    assert art.pii_hits["email"] >= 3 and art.pii_hits["anthropic-key"] == 1 and "api-key" not in art.pii_hits and art.pii_hits["phone"] >= 1
    assert set(art.chat_thread_refs) == {wc["ids"]["chat_a"], wc["ids"]["chat_b"]} and art.first < art.last
    assert art.extracted == 3 and "conversations.json" in art.entries


def test_extraction_is_redacted_and_faithful(wc, tmp_path):
    webchat.triage_export(Path(wc["zip"]), red=Redactor("standard"), extract_dir=tmp_path / "x")
    files = sorted((tmp_path / "x").rglob("*.md"))
    assert [f.parent.name for f in files].count("conversations") == 2 and any("research-notes" in str(f) for f in files)
    text = all_text(files)
    for v in (wc["pii"]["email"], wc["secrets"]["anthropic_key"], "[email protected]"):
        assert v not in text
    assert "Quarterly planning" in text and "Consider budget constraints first." in text and "Q4 plan" in text and "budget.csv" in text and "Dinner ideas using lentils?" in text
    webchat.triage_export(Path(wc["zip"]), red=Redactor("none"), extract_dir=tmp_path / "y")
    raw = all_text(sorted((tmp_path / "y").rglob("*.md")))
    assert wc["pii"]["email"] in raw and wc["secrets"]["anthropic_key"] not in raw                           # hard secret hidden even at 'none'


def test_thinking_can_be_excluded(wc, tmp_path):
    webchat.triage_export(Path(wc["zip"]), extract_dir=tmp_path / "x", include_thinking=False)
    assert "Consider budget" not in all_text(sorted((tmp_path / "x").rglob("*.md")))


def test_single_json_markdown_and_bad_exports(wc, tmp_path):
    single = webchat.triage_export(Path(wc["single"]))
    assert single.kind == "single-conversation-json" and single.conversations == 1 and single.messages == 2
    md = tmp_path / "chat.md"
    md.write_text("# Human\nhi a@b.com\n# Claude\nhello " + wc["ids"]["chat_a"], encoding="utf-8")
    m = webchat.triage_export(md)
    assert m.kind == "markdown-export" and m.messages == 2 and m.pii_hits["email"] == 1 and m.chat_thread_refs == [wc["ids"]["chat_a"]]
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04 definitely not a zip")
    assert webchat.triage_export(bad).error
    nz = tmp_path / "other.zip"
    with zipfile.ZipFile(nz, "w") as z:
        z.writestr("readme.txt", "x")
    assert "not a Claude export" in webchat.triage_export(nz).kind
    assert webchat.triage_export(tmp_path / "missing.zip").error


def test_large_export_streams(tmp_path):
    convs = [{"uuid": f"{i:08x}-0000-4000-8000-000000000000", "name": f"c{i}", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
              "chat_messages": [{"uuid": "m", "sender": "human", "text": "x" * 500, "content": [], "created_at": "2026-01-01T00:00:00Z"}] * 3} for i in range(400)]
    z = tmp_path / "big.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", json.dumps(convs))
    art = webchat.triage_export(z)
    assert art.conversations == 400 and art.messages == 1200


# --------------------------------------------------------------------------- browser


def test_browser_scan(wc):
    prof = Path(wc["profile"])
    hist = prof / "History"
    before = (hist.stat().st_size, hist.stat().st_mtime_ns, hist.read_bytes())
    bp = webchat.scan_browser_profile("Chromium", prof)
    after = (hist.stat().st_size, hist.stat().st_mtime_ns, hist.read_bytes())
    assert before == after                                                     # History is copied, never opened in place
    assert bp.history_hits == 27 and bp.kinds["chat"] == 10 and bp.kinds["project"] == 2 and bp.kinds["new"] == 11
    assert set(bp.thread_ids) == {wc["ids"]["chat_a"], wc["ids"]["chat_c"], "d1111111-2222-4333-8444-555555555555"}
    assert bp.thread_ids[wc["ids"]["chat_a"]]["title"] == "Quarterly planning" and bp.thread_ids[wc["ids"]["chat_a"]]["visits"] == 7
    assert bp.indexeddb_bytes > 2000 and bp.local_storage_hit and bp.wal_uuids == 2 and bp.extension_bytes > 0 and bp.last_visit


def test_extension_tokens_are_never_extracted(fx, wc, tmp_path):
    rep = _run(fx, wc, tmp_path)
    md = build_webchat_report(rep, Redactor("none"), "t")
    assert wc["secrets"]["oauth_access"] not in md and wc["secrets"]["oauth_refresh"] not in md and "unencrypted" in md


def test_browser_profile_discovery(wc):
    assert [p.name for p in paths.browser_profiles(Path(wc["browser_root"]))] == ["Default"]
    assert paths.browser_profiles(Path(wc["browser_root"]) / "missing") == []


def test_locked_or_corrupt_history_is_survivable(tmp_path):
    prof = tmp_path / "Default"
    prof.mkdir()
    (prof / "History").write_bytes(b"not sqlite at all")
    bp = webchat.scan_browser_profile("Chromium", prof)
    assert bp.error and bp.history_hits == 0


# --------------------------------------------------------------------------- orchestration & report


def test_thread_catalog_merges_sources(fx, wc, tmp_path):
    rep = _run(fx, wc, tmp_path)
    st = rep.stats()
    assert st["cowork"] == 2 and st["code_tab"] == 2 and st["export_conversations"] == 2 and st["exports"] == 2
    assert st["browser_visits"] == 27 and st["extension_stores"] == 1 and st["desktop_found"] and st["cowork_cost_usd"] == pytest.approx(8.64)
    by = {(t.source_kind, t.thread_id): t for t in rep.threads}
    a = by[("export", wc["ids"]["chat_a"])]
    assert a.in_export and a.browser_visits == 7 and a.body_location == "export archive"                 # export ⟷ history by uuid
    c = by[("browser", wc["ids"]["chat_c"])]
    assert c.catalog_only and c.body_location == "server only" and c.title == "Trip itinerary ideas"
    cow = next(t for t in rep.threads if t.source_kind == "cowork" and t.local_transcript)
    assert cow.turns == 2 and cow.messages >= 4 and cow.cost_usd == pytest.approx(8.64)
    ok = next(t for t in rep.threads if t.source_kind == "code-tab" and t.cli_session_id == fx["sessions"]["main"])
    assert ok.local_transcript.endswith(f"{fx['sessions']['main']}.jsonl") and ok.turns == 4
    gone = next(t for t in rep.threads if t.source_kind == "code-tab" and t.cli_session_id.startswith("99999999"))
    assert gone.catalog_only and any("not on disk" in w for w in rep.warnings)
    failed = next(t for t in rep.threads if t.title == "Failed start")
    assert "virtiofs" in failed.error and failed.catalog_only


def test_report_is_redacted_and_complete(fx, wc, tmp_path):
    rep = _run(fx, wc, tmp_path)
    md = build_webchat_report(rep, Redactor("standard", home_dirs=[Path.home()]), "0.1.0")
    for v in (wc["pii"]["email"], "Alice Planted", wc["secrets"]["github_pat"], wc["secrets"]["jwt"]):
        assert v not in md, v
    for section in ("## Headlines", "## How Claude marks data locally", "## Thread Catalog", "## Conversations Recoverable Locally", "## Claude Desktop",
                    "## Browser Traces", "## Export Archives", "## Warnings"):
        assert section in md, section
    assert "SHA-256" in md and "PII / secret markers" in md and "Reconstructed" in md and "🤝 Cowork" in md and "☁" not in md


def test_scan_can_skip_browser_and_transcripts(fx, wc, tmp_path):
    rep = webchat.run_webchat(fx["claude_path"], [], desktop_dirs=[wc["desktop_root"]], browsers=[], scan_browser=False, scan_transcripts=False)
    assert not rep.browsers and all(t.turns == 0 for t in rep.threads) and rep.stats()["browser_visits"] == 0


def test_cancel_during_export(fx, wc, tmp_path):
    from cantata.core.models import RecoveryCancelled
    with pytest.raises(RecoveryCancelled):
        webchat.run_webchat(fx["claude_path"], [Path(wc["zip"])], desktop_dirs=[wc["desktop_root"]], browsers=[], cancel=lambda: True)


def test_cli_chatcowork(fx, wc, tmp_path, capsys):
    from cantata.cli import main
    out = tmp_path / "cc.md"
    rc = main(["chatcowork", "-s", str(fx["claude_path"]), "-e", wc["zip"], "--desktop-dir", wc["desktop_root"], "--extract", str(tmp_path / "chats"),
               "-o", str(out), "--no-browser", "-q"])
    assert rc == 0 and out.exists() and list((tmp_path / "chats").rglob("*.md"))
    assert wc["pii"]["email"] not in out.read_text(encoding="utf-8")


def test_desktop_walk_does_not_descend_into_session_working_dirs(tmp_path):
    """A manifest-shaped file buried in a session's outputs/ must not be mistaken for a session (and is never walked)."""
    base = tmp_path / "Claude" / "local-agent-mode-sessions" / "acct" / "org"
    (base / "local_aaaaaaaa-0000-4000-8000-000000000001" / "outputs").mkdir(parents=True)
    (base / "local_aaaaaaaa-0000-4000-8000-000000000001.json").write_text(json.dumps({"sessionId": "local_aaaaaaaa-0000-4000-8000-000000000001", "title": "real"}),
                                                                             encoding="utf-8")
    (base / "local_aaaaaaaa-0000-4000-8000-000000000001" / "outputs" / "local_bbbbbbbb-0000-4000-8000-000000000002.json").write_text(
        json.dumps({"sessionId": "local_decoy", "title": "decoy"}), encoding="utf-8")
    info = desktop.scan_desktop([tmp_path / "Claude"])
    assert [s.title for s in info.sessions] == ["real"]


# --------------------------------------------------------------------------- evicted IndexedDB blobs


class _FakeStore:
    """Mimics ccl_chromium_indexeddb's object store: an evicted blob raises *out of* the generator.

    That is the exact shape of the live bug - Chromium GCs the blob file while the
    LevelDB record survives, and without a bad_deserializer_data_handler the reader
    re-raises, killing the iteration and silently dropping every later record.
    """

    def __init__(self, records, boom_at):
        self._records, self._boom_at = records, boom_at
        self.handler = None

    def iterate_records(self, *, errors_to_stdout=False, bad_deserializer_data_handler=None):
        self.handler = bad_deserializer_data_handler
        for i, r in enumerate(self._records):
            if i == self._boom_at:
                # The library only calls the handler when one was supplied; otherwise it re-raises.
                if bad_deserializer_data_handler is None:
                    raise FileNotFoundError("blob evicted by the browser")
                bad_deserializer_data_handler(r, b"")
                continue
            yield r


def test_evicted_blob_does_not_drop_later_records():
    records = [{"conversationUuid": f"u{i}", "tree": {"chat_messages": [1]}} for i in range(6)]
    seen = [r["conversationUuid"] for r in webchat._iter_store_records(_FakeStore(records, boom_at=2))]
    assert seen == ["u0", "u1", "u3", "u4", "u5"]          # only the evicted record is lost


def test_evicted_blob_still_raises_when_itself_breaks(monkeypatch):
    """A genuinely fatal error must not mask the swallow-guard."""
    def boom(self, *, errors_to_stdout=False, bad_deserializer_data_handler=None):
        raise RuntimeError("leveldb corrupt")
        yield  # pragma: no cover - generator marker
    monkeypatch.setattr(_FakeStore, "iterate_records", boom)
    with pytest.raises(RuntimeError):
        list(webchat._iter_store_records(_FakeStore([], 0)))


# --------------------------------------------------------------------------- duplicate conversation records


def test_conv_rank_prefers_the_newest_copy():
    """Dedup used to compare len(dict) (key count), which is 15-with-tree / 10-without and
    so never separated two same-shape versions - first-yielded always won. Rank by recency."""
    old = {"uuid": "x", "conversationUpdatedAt": 100, "messageCount": 2, "tree": {"name": "old"}}
    new = {"uuid": "x", "conversationUpdatedAt": 200, "messageCount": 16, "tree": {"name": "new"}}
    assert webchat._conv_rank(new) > webchat._conv_rank(old)

    # equal timestamps -> more messages wins
    a = {"uuid": "x", "conversationUpdatedAt": 100, "messageCount": 2, "tree": {}}
    b = {"uuid": "x", "conversationUpdatedAt": 100, "messageCount": 9, "tree": {}}
    assert webchat._conv_rank(b) > webchat._conv_rank(a)

    # identical recency and size -> a record carrying tree metadata wins
    c = {"uuid": "x", "conversationUpdatedAt": 100, "messageCount": 2}
    d = {"uuid": "x", "conversationUpdatedAt": 100, "messageCount": 2, "tree": {"name": "t"}}
    assert webchat._conv_rank(d) > webchat._conv_rank(c)


def test_conv_rank_tolerates_missing_and_float_fields():
    """Live records carry float timestamps and sometimes no tree at all."""
    for rec in ({"uuid": "x"},
                {"uuid": "x", "conversationUpdatedAt": 1790837887154.0, "tree": None},
                {"uuid": "x", "messageCount": None, "writtenAt": "not-a-number"}):
        assert isinstance(webchat._conv_rank(rec), tuple)


def test_empty_source_falls_back_to_the_detected_dir(fx, tmp_path, monkeypatch):
    """`Path("")` is `.` -- an empty source used to scan the CWD (reporting 0 recoverable)
    instead of the real Claude dir."""
    monkeypatch.setattr(webchat.scanner, "detect_default_source", lambda: fx["claude_path"])
    rep = webchat.run_webchat("", [], desktop_dirs=None, browsers=[], scan_browser=False, scan_indexeddb=False)
    assert rep.source_dir == fx["claude_path"]
    # and it is a real scan of that dir, not an empty CWD scan
    explicit = webchat.run_webchat(fx["claude_path"], [], desktop_dirs=None, browsers=[],
                                   scan_browser=False, scan_indexeddb=False)
    assert rep.stats()["threads"] == explicit.stats()["threads"] > 0


def test_reflect_empty_source_falls_back_to_the_detected_dir(fx, monkeypatch):
    """Same CWD trap as run_webchat: `Path("")` must not read the launch directory."""
    monkeypatch.setattr(reflect.scanner, "detect_default_source", lambda: fx["claude_path"])
    r = reflect.generate_reflection("")
    assert "period" in r and "activity" in r          # renders a real report, not an error
    assert r["activity"]["total_sessions"] > 0


# --------------------------------------------------------------------------- write-path integrity


def test_same_day_same_title_conversations_do_not_overwrite_each_other(tmp_path):
    """The output name is <date>_<title>_<uuid8>.md - not unique. Two records for the same
    conversation written on the same day produced the same filename and the second write
    silently clobbered the first, so files went missing while the count still included them."""

    def conv(messages, updated):
        return {"uuid": "a59a63f0-5677-4062-94a6-0d89419b8517", "name": "Padding text generation request",
                "model": "claude-sonnet-4-6", "created_at": "2026-02-19T01:05:48Z", "updated_at": updated,
                "platform": "CLAUDE_AI", "thinking_mode": "auto", "is_starred": False, "is_archived": False,
                "settings": {}, "summary": "", "stop_reasons": {}, "approxBytes": 1, "source": "Desktop:Claude",
                "messages": [{"index": i, "sender": "human", "created_at": "2026-02-19T01:05:48Z",
                              "content": [{"type": "text", "text": f"body {i}"}]} for i in range(messages)]}

    out = tmp_path / "out"
    written = webchat.write_indexeddb_conversations(
        [conv(7, "2026-02-24T11:27:42Z"), conv(8, "2026-02-24T23:59:00Z")], out)
    bodies = [p for p in written if p.name != "INDEX.md"]
    assert len(bodies) == 2, f"both same-day copies must land, got {[p.name for p in bodies]}"
    texts = [p.read_text(encoding="utf-8") for p in bodies]
    assert any("body 6" in t for t in texts) and any("body 7" in t for t in texts)   # neither clobbered
    assert len({p.name for p in bodies}) == 2


def test_alternate_versions_are_written_not_discarded(tmp_path):
    """Dedup kept only the longest copy of a uuid and dropped the rest, silently losing real
    content (a regenerated / edited conversation, or the same chat cached under two accounts).
    The dedup now hangs the losers off ``versions``; the writer must emit them."""

    def conv(n, updated, source):
        return {"uuid": "11111111-1111-4111-8111-111111111111", "name": "Two copies", "model": "m",
                "created_at": "2026-01-01T00:00:00Z", "updated_at": updated, "platform": "CLAUDE_AI",
                "thinking_mode": "off", "is_starred": False, "is_archived": False, "settings": {},
                "summary": "", "stop_reasons": {}, "approxBytes": 1, "source": source,
                "messages": [{"index": i, "sender": "human", "created_at": "2026-01-01T00:00:00Z",
                              "content": [{"type": "text", "text": f"variant {source} msg {i}"}]} for i in range(n)]}

    main = conv(9, "2026-01-02T00:00:00Z", "Brave:Default")
    main["also_in"] = ["Desktop:Claude"]
    main["versions"] = [conv(3, "2026-01-02T00:00:00Z", "Desktop:Claude")]
    out = tmp_path / "out"
    written = webchat.write_indexeddb_conversations([main], out)
    text = "\n".join(p.read_text(encoding="utf-8") for p in written)
    assert "variant Brave:Default msg 8" in text          # the longer copy, as the main file
    assert "variant Desktop:Claude msg 2" in text         # the shorter copy, preserved as an alternate
    assert any("alternate copy" in p.read_text(encoding="utf-8") for p in written)
    assert len([p for p in written if p.name != "INDEX.md"]) == 2


def test_scan_reports_per_source_cache_state(fx, wc, tmp_path, monkeypatch):
    """A store that exists but holds nothing is the signature of a signed-out / account-switched
    session. The scan must surface it per source rather than just reporting a small total."""
    from cantata.core import webchat as wc_mod

    def fake_scan(sources=None, log=None, source_stats=None, cancel=None):
        if source_stats is not None:
            source_stats.extend([("Desktop:Claude", "C:/x/leveldb", 0, 0),
                                 ("Brave:Default", "C:/y/leveldb", 4, 4)])
        return []

    monkeypatch.setattr(wc_mod.paths, "claude_ai_indexeddb_dirs", lambda browsers=None: [])
    monkeypatch.setattr(wc_mod, "scan_claude_ai_indexeddb", fake_scan)
    rep = webchat.run_webchat(fx["claude_path"], [], desktop_dirs=[wc["desktop_root"]], browsers=[],
                              scan_browser=False, scan_indexeddb=True)
    assert rep.idb_sources == [("Desktop:Claude", "C:/x/leveldb", 0, 0), ("Brave:Default", "C:/y/leveldb", 4, 4)]
    assert rep.stats()["indexeddb_records"] == 4
    md = build_webchat_report(rep, Redactor("standard"), "t")
    assert "## IndexedDB cache state" in md and "empty" in md


def test_report_states_coverage_against_what_was_opened(fx, wc, tmp_path):
    """'4 recoverable' next to 95 history visits reads like a broken extraction. The report must
    say how many opened conversations actually have a local body, and list the ones that do not."""
    rep = _run(fx, wc, tmp_path)
    cov = rep.coverage()
    assert cov["visited"] >= 1 and cov["visited_recoverable"] >= 1
    md = build_webchat_report(rep, Redactor("standard"), "t")
    assert "Coverage vs. what you opened" in md
    assert "## Opened but server-side only" in md


def test_indexeddb_scan_honours_cancel():
    """The Cancel button was enabled during the IndexedDB scan but that phase never polled it,
    so clicking Cancel did nothing until the phase finished on its own."""
    from cantata.core.models import RecoveryCancelled
    with pytest.raises(RecoveryCancelled):
        webchat.scan_claude_ai_indexeddb(cancel=lambda: True)


def test_browser_scan_honours_cancel(tmp_path):
    from cantata.core.models import RecoveryCancelled
    prof = tmp_path / "Default"
    prof.mkdir()
    (prof / "Preferences").write_text("{}", encoding="utf-8")
    with pytest.raises(RecoveryCancelled):
        webchat.scan_browsers([("Chromium", tmp_path)], cancel=lambda: True)


def test_index_lists_files_from_earlier_runs(tmp_path):
    """INDEX.md was rewritten from only this run's rows, so a second run into the same folder
    hid every file the first run wrote (408 conversations on disk, an index listing 4)."""
    def conv(uuid, name, updated, n):
        return {"uuid": uuid, "name": name, "model": "m", "created_at": "2026-01-01T00:00:00Z",
                "updated_at": updated, "platform": "CLAUDE_AI", "thinking_mode": "off",
                "is_starred": False, "is_archived": False, "settings": {}, "summary": "",
                "stop_reasons": {}, "approxBytes": 1, "source": "Desktop:Claude",
                "messages": [{"index": i, "sender": "human", "created_at": "2026-01-01T00:00:00Z",
                              "content": [{"type": "text", "text": "x"}]} for i in range(n)]}

    out = tmp_path / "out"
    webchat.write_indexeddb_conversations([conv("aaaaaaaa-0000-4000-8000-000000000001", "First run", "2026-01-02T00:00:00Z", 2)], out)
    webchat.write_indexeddb_conversations([conv("bbbbbbbb-0000-4000-8000-000000000002", "Second run", "2026-01-03T00:00:00Z", 3)], out)

    idx = (out / "INDEX.md").read_text(encoding="utf-8")
    assert "First run" in idx and "Second run" in idx, "an earlier run's files must stay indexed"
    assert "Total: **2**" in idx
    assert "1 written by this run" in idx
    assert "predate this run" in idx
