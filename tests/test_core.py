"""Core engine: path encoding, redaction, parser, recovery, scanner, stores, audit, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cantata.cli import main as cli_main
from cantata.core import audit, configs, paths, report, scanner, stores
from cantata.core.models import RecoveryCancelled, RecoveryOptions
from cantata.core.recovery import collect_bundle, run_recovery
from cantata.core.redaction import Redactor, count_markers
from cantata.core.transcripts import link_subagents, ms_between, parse_transcript, parse_ts
from conftest import all_text

# --------------------------------------------------------------------------- paths


def test_project_dir_encoding_matches_claude_code():
    assert paths.encode_project_dir("/home/me/my_app") == "-home-me-my-app"
    assert paths.encode_project_dir("C:\\Users\\me\\proj") == "C--Users-me-proj"
    assert paths.encode_project_dir("/Users/foo/local.tmp/proj") == "-Users-foo-local-tmp-proj"
    assert paths.encode_project_dir("/tmp/x \U0001F600 y") == "-tmp-x----y"          # emoji = two UTF-16 units


def test_long_paths_get_hash_suffix():
    p = "/" + "a" * 260
    enc = paths.encode_project_dir(p)
    assert len(enc) == 207 and enc[:200] == paths._sanitize(p)[:200] and enc[200] == "-"
    assert paths.java_hash32("abc") == 96354 and paths.java_hash32("hello world") == 1794106052


def test_decode_fallback_and_label():
    assert paths.decode_project_dir("-home-me-proj") == "/home/me/proj"
    assert paths.decode_project_dir("C--Users-me-proj") == "C:\\Users\\me\\proj"
    assert paths.project_label("/home/me/proj") == "proj" and paths.project_label("C:\\Users\\me\\api") == "api"


def test_config_dir_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    assert paths.claude_config_dir() == tmp_path / "custom"


# --------------------------------------------------------------------------- redaction


@pytest.mark.parametrize("level", ["standard", "paranoid", "none"])
def test_hard_secrets_redacted_at_every_level(level, fx):
    r = Redactor(level, home_dirs=["/home/alice"])
    blob = " ".join(fx["secrets"][k] for k in ("anthropic_key", "github_pat", "aws_key", "jwt"))
    blob += f' postgres://admin:{fx["secrets"]["db_password"]}@db:5432/x "accessToken": "{fx["secrets"]["oauth_access"]}"'
    out = r.apply(blob)
    for k in ("anthropic_key", "github_pat", "aws_key", "jwt", "db_password", "oauth_access"):
        assert fx["secrets"][k] not in out, (level, k)


def test_level_matrix(fx):
    text = f"mail {fx['pii']['email']} ip {fx['pii']['ip']} tel {fx['pii']['phone']}. file /home/alice/p/x.py"
    std = Redactor("standard", home_dirs=["/home/alice"]).apply(text)
    par = Redactor("paranoid", home_dirs=["/home/alice"]).apply(text)
    non = Redactor("none").apply(text)
    assert fx["pii"]["email"] not in std and "~/p/x.py" in std and fx["pii"]["ip"] in std       # standard: email + home only
    assert fx["pii"]["ip"] not in par and fx["pii"]["phone"] not in par and "[PATH]/x.py" in par
    assert fx["pii"]["email"] in non and "/home/alice/p/x.py" in non


def test_false_positives_and_allowlist():
    r = Redactor("standard")
    assert "max_tokens: 100000" in r.apply("max_tokens: 100000") and "total_tokens=123456" in r.apply("total_tokens=123456")
    assert "noreply@anthropic.com" in r.apply("Co-Authored-By: Claude <noreply@anthropic.com>")
    assert "[REDACTED]" in r.apply("password: hunter2hunter2")


def test_id_trimming_and_scrub():
    assert Redactor("standard").session_id("11111111-aaaa") == "11111111" and Redactor("paranoid").session_id("11111111-aaaa") == "1111"
    assert Redactor("none").session_id("11111111-aaaa") == "11111111-aaaa"
    assert Redactor("standard").call_id("toolu_01ABCDEFGHIJKLMN") == "toolu_01ABCD"
    s = Redactor("standard").scrub({"oauthAccount": {"emailAddress": "a@b.com", "accountUuid": "x"}, "env": {"GITHUB_TOKEN": "ghp_" + "a" * 36, "DEBUG": "1"}})
    assert s["oauthAccount"] == {"emailAddress": "[EMAIL]", "accountUuid": "[ID]"} and s["env"] == {"GITHUB_TOKEN": "[REDACTED]", "DEBUG": "1"}


def test_count_markers_counts_once():
    c = count_markers("a@b.com c@d.org sk-ant-api03-ABCDEFGHIJKLMNOPQRST 10.0.0.1")
    assert c["email"] == 2 and c["anthropic-key"] == 1 and "api-key" not in c and c["ipv4"] == 1


# --------------------------------------------------------------------------- parser


def test_timestamp_helpers():
    assert parse_ts("2026-01-01T00:00:00.123Z") == pytest.approx(1767225600.123)
    assert parse_ts(1767225600000) == pytest.approx(1767225600.0)
    assert ms_between("2026-01-01T00:00:00.000Z", "2026-01-01T00:00:02.400Z") == 2400
    assert ms_between("2026-01-01T00:00:05Z", "2026-01-01T00:00:01Z") is None and parse_ts("garbage") is None


def test_parse_main_session(fx):
    main = parse_transcript(Path(fx["paths"]["main"]))
    e = fx["expected"]
    u = main.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (
        e["main_usage"]["input"], e["main_usage"]["output"], e["main_usage"]["cache_read"], e["main_usage"]["cache_write"])
    assert len(main.turns) == e["main_turns"] and main.prompt_count == 3
    assert main.api_errors == e["main_api_errors"] and len(main.compactions) == e["main_compactions"]
    assert main.corrupt_lines == e["corrupt_lines"] and main.compactions[0]["pre_tokens"] == 155000
    assert main.display_title == "Login bug hunt" and main.ai_title == "Fix login bug in webapp"     # custom > ai
    assert main.queued_prompts == ["also update the changelog please", "and bump the version number"] and main.pr_links   # queue-operation + attachment, de-duplicated
    assert main.tracked_files and main.origin == "cli" and main.type_counts["brand-new-line-type-from-2027"] == 1
    assert main.turns[1].interrupted and main.turns[2].is_compact
    assert main.stop_reasons["tool_use"] == 3 and main.stop_reasons["end_turn"] == 2
    tools = [tc for t in main.turns for m in t.messages for tc in m.tool_calls]
    assert [t.name for t in tools] == ["Bash", "Read", "Task"] and all(t.output for t in tools)
    assert tools[2].agent_id == "a1b2c3d" and tools[2].reported_ms == 6400


def test_multiblock_response_is_one_message(fx):
    main = parse_transcript(Path(fx["paths"]["main"]))
    first = main.turns[0].messages[0]
    assert first.thinking and first.text and len(first.tool_calls) == 1          # 3 lines, one message
    assert sum(1 for m in main.turns[0].messages if m.role == "assistant") == 4


def test_old_session_origin_and_summary_title(fx):
    old = parse_transcript(Path(fx["paths"]["old"]))
    assert old.origin == "vscode" and old.display_title == "Project layout overview" and old.versions == ["2.0.55"]


def test_subagent_files_and_linking(fx):
    files = scanner.discover_code_transcripts(fx["claude_path"])
    recs = [parse_transcript(f) for f in files]
    link_subagents(recs)
    main = next(r for r in recs if r.kind == "main" and r.session_id == fx["sessions"]["main"])
    sub = next(r for r in recs if r.kind == "subagent" and r.agent_id == "a1b2c3d")
    assert sub.parent_session_id == main.session_id and "agent-a1b2c3d" in main.child_ids
    assert sub.agent_type == "Explore" and sub.models == ["claude-haiku-4-5-20251001"] and sub.parent_call_id.startswith("toolu_01TASK")


def test_legacy_inline_sidechains_become_children(fx):
    legacy = parse_transcript(next(f for f in scanner.discover_code_transcripts(fx["claude_path"]) if f.session_id == fx["sessions"]["legacy"]))
    kids = legacy.extra["inline_children"]
    assert len(kids) == 1 and kids[0].session_id == "agent-deadbee" and kids[0].parent_session_id == legacy.session_id
    assert kids[0].usage.output_tokens == 4 and "agent-deadbee" in legacy.child_ids


def test_parser_survives_garbage(tmp_path):
    f = tmp_path / "projects" / "p" / "s.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text('not json\n[1,2]\n{"type":"user","message":{"content":"hi"},"timestamp":"2026-01-01T00:00:00Z"}\n\n{"type":', encoding="utf-8")
    rec = parse_transcript(f)
    assert rec.corrupt_lines == 3 and len(rec.turns) == 1 and rec.first_prompt == "hi"


# --------------------------------------------------------------------------- scanner & stores


def test_discovery_kinds(fx):
    files = scanner.discover_code_transcripts(fx["claude_path"])
    kinds = sorted(f.kind for f in files)
    assert kinds == ["main", "main", "main", "main", "orphaned", "subagent"]
    assert len(scanner.discover_code_transcripts(fx["claude_path"], include_orphaned=False)) == 5


def test_quick_stats_and_retention(fx):
    s = scanner.quick_stats(fx["claude_path"])
    assert (s["active_transcripts"], s["orphaned_transcripts"], s["subagents"], s["projects"]) == (4, 1, 1, 3)
    assert s["retention_effective"] == 30 and s["expiring_soon"] == 1 and s["oldest_age_days"] == pytest.approx(28.0, abs=0.2)
    assert s["versions"][0] == "2.1.280" and s["sessions_reported"] == 9 and s["history_entries"] == 5 and s["has_credentials"]
    assert any("aged out" in i for i in s["issues"]) and any("archive now" in i for i in s["issues"])


def test_quick_stats_missing_dir(tmp_path):
    s = scanner.quick_stats(tmp_path / "nope")
    assert not s["exists"] and s["issues"]


def test_index_only_sessions(fx):
    cd = fx["claude_path"]
    have = {f.session_id for f in scanner.discover_code_transcripts(cd)}
    gone = stores.index_only_sessions(stores.read_history(cd), stores.read_sessions_indexes(cd / "projects"), None, have)
    assert [g["session_id"] for g in gone] == [fx["sessions"]["gone"]] and "sessions-index.json" in gone[0]["source"]


def test_stores_read_everything_without_secrets(fx):
    cd = fx["claude_path"]
    blob = json.dumps([stores.read_todos(cd), stores.read_tasks(cd), stores.read_ide_locks(cd), stores.read_plugins(cd), stores.read_misc(cd),
                       stores.read_telemetry_events(cd), configs.read_credentials(cd), configs.read_global_json(paths.claude_global_json(cd)),
                       configs.read_settings(cd), stores.read_extensions(cd)], default=str)
    for name in ("anthropic_key", "oauth_access", "oauth_refresh", "github_pat", "ide_auth", "jwt"):
        assert fx["secrets"][name] not in blob, name
    ide = stores.read_ide_locks(cd)[0]
    assert ide["has_auth_token"] and "authToken" not in ide
    cred = configs.read_credentials(cd)
    assert cred["has_access_token"] and cred["subscription"] == "max" and "accessToken" not in cred
    g = configs.read_global_json(paths.claude_global_json(cd))
    assert {m["name"] for m in g["mcp_servers"]} == {"github", "docs"} and g["mcp_servers"][0]["env_keys"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]


# --------------------------------------------------------------------------- recovery end-to-end


def _recover(fx, tmp_path, **kw):
    opts = RecoveryOptions(include_desktop=False, **kw)
    out = tmp_path / "out" / "report.md"
    res = run_recovery(fx["claude_path"], out, opts)
    files = [out, *res.per_session_paths]
    return res, out, all_text(files)


@pytest.mark.parametrize("level", ["standard", "paranoid", "none"])
def test_no_secret_ever_reaches_a_report(level, fx, tmp_path):
    res, out, text = _recover(fx, tmp_path, redaction_level=level)
    for k, v in fx["secrets"].items():
        assert v not in text, f"{k} leaked at level {level}"


def test_standard_report_content(fx, tmp_path):
    res, out, text = _recover(fx, tmp_path)
    st = res.stats
    assert st["total_sessions"] == 5 and st["archived"] == 1 and st["subagents"] == 2 and st["index_only"] == 1
    assert "/home/alice" not in text and fx["pii"]["email"] not in text
    for section in ("## Overview", "## Detected Versions", "## Retention & Coverage", "## Sessions", "## Usage", "## Application State",
                    "## Warnings", "## Session Transcripts", "Index-only sessions (1)"):
        assert section in text, section
    assert "`2.1.280`" in text                                                     # the reference's dead 'Detected Versions' block works here
    assert "**Claude** _(" in text and "\n**:\n" not in text and "\n**:" not in text          # the reference's broken '**:' label
    assert len(res.per_session_paths) == 7 and (out.parent / "report_sessions").is_dir()
    assert "brand-new-line-type-from-2027" in text                                  # unknown line types are surfaced


def test_orphaned_copies_do_not_double_count(fx, tmp_path):
    with_orphans, *_ = _recover(fx, tmp_path / "a")
    without, *_ = _recover(fx, tmp_path / "b", include_orphaned=False)
    assert with_orphans.stats["total_tokens"] == without.stats["total_tokens"]
    assert without.stats["archived"] == 0


def test_paranoid_strips_paths_ip_phone(fx, tmp_path):
    _, _, text = _recover(fx, tmp_path, redaction_level="paranoid")
    assert fx["pii"]["ip"] not in text and fx["pii"]["phone"] not in text and "/home/alice" not in text and "~/projects" not in text


def test_none_keeps_email_but_not_secrets(fx, tmp_path):
    _, _, text = _recover(fx, tmp_path, redaction_level="none")
    assert fx["pii"]["email"] in text and fx["pii"]["ip"] in text


def test_option_toggles(fx, tmp_path):
    res, out, text = _recover(fx, tmp_path, include_transcripts=False, include_app_state=False, include_tokens=False, per_sessions=False)
    assert "transcripts excluded" in text and "## Application State" not in text and "## Usage" not in text and not res.per_session_paths
    _, _, nosub = _recover(fx, tmp_path / "c", include_subagents=False)
    assert "see its section" not in nosub
    _, _, flat = _recover(fx, tmp_path / "d", flatten_subagents=True)
    assert "**Subagent**" in flat
    _, _, notool = _recover(fx, tmp_path / "e", include_tools=False, include_thinking=False)
    assert "<summary>🔧" not in notool and "💭" not in notool


def test_embed_limit_switches_to_links(fx, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "EMBED_LIMIT_BYTES", 10)
    res, out, _ = _recover(fx, tmp_path)
    combined = out.read_text(encoding="utf-8")
    assert "each session has its own file" in combined and "### <a id=\"s1\">" not in combined and res.per_session_paths


def test_time_in_flight_is_gated_out_of_default_reports(fx, tmp_path):
    _, _, text = _recover(fx, tmp_path)
    assert "## Time in Flight" not in text and "Last API time" not in text and "App-reported" not in text and "wall " not in text
    _, _, flight_text = _recover(fx, tmp_path / "f", include_flight=True)
    assert "## Time in Flight" in flight_text and "Last API time" in flight_text and "upper bound" in flight_text


def test_cancel_and_bad_source(fx, tmp_path):
    with pytest.raises(RecoveryCancelled):
        run_recovery(fx["claude_path"], tmp_path / "x.md", RecoveryOptions(include_desktop=False), cancel=lambda: True)
    assert not (tmp_path / "x.md").exists()
    with pytest.raises(ValueError):
        run_recovery(tmp_path / "missing", tmp_path / "y.md")
    with pytest.raises(ValueError):
        run_recovery(fx["claude_path"], tmp_path / "y.md", RecoveryOptions(redaction_level="bogus"))


def test_progress_is_monotonic_and_finishes(fx, tmp_path):
    seen = []
    run_recovery(fx["claude_path"], tmp_path / "p.md", RecoveryOptions(include_desktop=False), progress=lambda f, label="": seen.append(f))
    assert seen[-1] == 1.0 and all(b >= a - 1e-9 for a, b in zip(seen, seen[1:]))


def test_empty_dir_still_reports(tmp_path):
    empty = tmp_path / ".claude"
    empty.mkdir()
    res = run_recovery(empty, tmp_path / "r.md", RecoveryOptions(include_desktop=False))
    assert res.stats["total_sessions"] == 0 and "No transcripts found" in (tmp_path / "r.md").read_text(encoding="utf-8")


def test_windows_long_filenames_capped(fx, tmp_path):
    red = Redactor("standard")
    rec = parse_transcript(Path(fx["paths"]["main"]))
    rec.custom_title = "x" * 400
    assert len(report.session_filename(12, rec, red)) <= 120


# --------------------------------------------------------------------------- audit & CLI


def test_audit_text_and_json(fx):
    text = audit.run_audit(fx["claude_path"])
    assert "Claude Local-State Audit" in text and "stats-cache.json" in text and "index-only" in text
    data = json.loads(audit.run_audit(fx["claude_path"], output_json=True))
    assert data["totals"]["input"] == 443 + 300 + 25 and data["stop_reasons"]["end_turn"] == 7
    assert "latency" not in text.lower() and "in flight" not in text.lower()          # time in flight lives behind the gate


def test_cli_scan_only_and_recover(fx, tmp_path, capsys):
    assert cli_main(["recover", "--scan-only", "-s", str(fx["claude_path"])]) == 0
    assert "Transcripts (active): 4" in capsys.readouterr().out
    out = tmp_path / "cli.md"
    assert cli_main(["-s", str(fx["claude_path"]), "-o", str(out), "--no-desktop", "-q"]) == 0      # bare flags default to recover
    assert out.exists() and (tmp_path / "cli_sessions").is_dir()
    assert cli_main(["recover", "-s", str(tmp_path / "nope"), "-o", str(tmp_path / "z.md")]) == 2


def test_cli_gate_semantics(fx, tmp_path, monkeypatch, capsys):
    assert cli_main(["flight", "-s", str(fx["claude_path"])]) == 3 and "locked" in capsys.readouterr().err
    assert cli_main(["capture", "--print-env"]) == 3
    assert cli_main(["recover", "--flight", "-s", str(fx["claude_path"]), "-o", str(tmp_path / "g.md")]) == 3
    assert cli_main(["gate", "--unlock"]) == 0
    assert cli_main(["gate", "--status"]) == 0 and "UNLOCKED" in capsys.readouterr().out
    assert cli_main(["capture", "--print-env", "--otlp-port", "4321"]) == 0 and "127.0.0.1:4321" in capsys.readouterr().out
    assert cli_main(["flight", "-s", str(fx["claude_path"]), "-q", "--json"]) == 0
    assert cli_main(["gate", "--lock"]) == 0
    assert cli_main(["flight", "--experimental", "-s", str(fx["claude_path"]), "-q"]) == 0      # flag opens it for one run


# --------------------------------------------------------------------------- hardening regressions


_ADVERSARIAL_TEXT_CASES = [
    ("newlines", "\n" * 60_000), ("space+newline", "  \n" * 20_000), ("auth header then blank lines", "Authorization: x\n" + "\n" * 60_000),
    ("a. pairs", "a." * 30_000), ("a- pairs", "a-" * 30_000), ("base64url", "Ab3-_xYz9" * 7_000), ("TOKEN= long", "TOKEN=" + "x" * 60_000),
    ("secret keywords", "secret secret " * 4_000), ("@ signs", "a@" * 30_000), ("deep path", "/" + "a/" * 30_000),
    ("private-key markers", "-----BEGIN PRIVATE KEY-----\n" * 2_000), ("long emails", "user.name+tag@example.co.uk " * 2_000),
]

# `ids` must be the short label only. Without it pytest derives the id from the whole
# parameter, and these payloads run to 60 000 chars — pytest then exports the id as
# $PYTEST_CURRENT_TEST, which Windows rejects at 32767 chars, erroring out every case
# before the test body ever runs.
@pytest.mark.parametrize("name,text", _ADVERSARIAL_TEXT_CASES,
                         ids=[n for n, _ in _ADVERSARIAL_TEXT_CASES])
def test_redaction_is_linear_on_adversarial_text(name, text):
    """Regression: `^\\s*` in the auth-header pattern made long blank-line runs quadratic (~100 s for 60 kB)."""
    import time
    red = Redactor("paranoid", home_dirs=["/home/alice"])
    t = time.perf_counter()
    red.apply(text)
    count_markers(text)
    assert time.perf_counter() - t < 3.0, name


def test_auth_header_still_redacted_after_hardening():
    r = Redactor("none")
    out = r.apply("GET / HTTP/1.1\nAuthorization: Basic dXNlcjpwYXNz\nX-Api-Key: abc123\nCookie: sid=1; t=2\n\nbody")
    assert "dXNlcjpwYXNz" not in out and "abc123" not in out and "sid=1" not in out and "body" in out


def test_global_json_never_falls_back_to_the_analysts_machine(tmp_path, monkeypatch):
    analyst_home = tmp_path / "analyst"
    analyst_home.mkdir()
    (analyst_home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "analyst@corp.com"}}', encoding="utf-8")
    monkeypatch.setattr(paths, "home", lambda: analyst_home)
    image = tmp_path / "image" / "claude_copy"
    image.mkdir(parents=True)
    assert paths.claude_global_json(image) is None and not paths.is_live_source(image)
    (image.parent / ".claude.json").write_text("{}", encoding="utf-8")
    assert paths.claude_global_json(image) == image.parent / ".claude.json"
    live = analyst_home / ".claude"
    live.mkdir()
    assert paths.is_live_source(live) and paths.claude_global_json(live) == analyst_home / ".claude.json"


def test_custom_config_dir_may_use_the_legacy_home_location(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "home", lambda: tmp_path)
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    custom = tmp_path / "elsewhere" / "cfg"
    custom.mkdir(parents=True)
    assert paths.claude_global_json(custom) is None
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    assert paths.claude_global_json(custom) == tmp_path / ".claude.json"


def test_forensic_image_is_not_mixed_with_this_machines_data(fx, wc, tmp_path, monkeypatch):
    """Pointing Source at a copied image must not pull the analyst's Desktop data, MCP logs or account into the report.

    Uses redaction level ``none`` on purpose: names are masked at *standard*, which would hide a leak from this test.
    """
    cache = tmp_path / "cache" / "-some-project" / "mcp-logs-machine-only-server"
    cache.mkdir(parents=True)
    (cache / "1.jsonl").write_text("{}", encoding="utf-8")
    machine_home = tmp_path / "machine_home"
    machine_home.mkdir()
    (machine_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationName": "MACHINE-ORG"},
                                                           "mcpServers": {"machine-only": {"command": "x"}}}), encoding="utf-8")
    monkeypatch.setattr(paths, "home", lambda: machine_home)
    monkeypatch.setattr(paths, "desktop_roots", lambda extra=(): [Path(wc["desktop_root"])])
    monkeypatch.setattr(paths, "cli_cache_dirs", lambda: [tmp_path / "cache"])

    logs = []
    res = run_recovery(fx["claude_path"], tmp_path / "img.md", RecoveryOptions(per_sessions=False, redaction_level="none"),
                       log=lambda m, lvl="info": logs.append(m))
    image_text = (tmp_path / "img.md").read_text(encoding="utf-8")
    assert not any(s.kind.startswith("cowork") for s in res.sessions)                # Desktop not auto-detected for an image
    assert "MACHINE-ORG" not in image_text and "machine-only" not in image_text      # analyst's ~/.claude.json not merged in
    assert any("MCP cache logs skipped" in m for m in logs) and any("auto-detection skipped" in m for m in logs)
    assert "Planted Industries" in image_text                                        # the image's OWN account is what shows

    # an explicit Desktop dir is honoured even for an image
    res2 = run_recovery(fx["claude_path"], tmp_path / "img2.md", RecoveryOptions(per_sessions=False, desktop_dirs=(wc["desktop_root"],)))
    assert any(s.kind == "cowork" for s in res2.sessions)

    # ...and this machine's *live* Claude dir does get auto-detection, its MCP cache logs and its own ~/.claude.json
    live = machine_home / ".claude"
    (live / "projects").mkdir(parents=True)
    monkeypatch.setattr(paths, "claude_config_dir", lambda: live)
    res3 = run_recovery(live, tmp_path / "live.md", RecoveryOptions(per_sessions=False, redaction_level="none"))
    live_text = (tmp_path / "live.md").read_text(encoding="utf-8")
    assert "MACHINE-ORG" in live_text and "`machine-only`" in live_text
    assert "mcp-logs-machine-only-server" in live_text
    assert "## Claude Desktop" in live_text and "Cowork audit logs" in live_text
    assert any(s.kind == "cowork" for s in res3.sessions)


def test_desktop_empty_list_means_none_and_none_means_autodetect(wc, monkeypatch):
    from cantata.core import desktop
    monkeypatch.setattr(paths, "desktop_roots", lambda extra=(): [Path(wc["desktop_root"])])
    assert not desktop.scan_desktop([]).found
    assert desktop.scan_desktop(None).found and desktop.scan_desktop([wc["desktop_root"]]).found


def test_quick_stats_single_walk_also_provides_dir_and_file_history_stats(fx):
    s = scanner.quick_stats(fx["claude_path"])
    assert s["file_history"] == [{"session": fx["sessions"]["main"], "files": 1, "bytes": 19}]
    assert s["dir_stats"]["debug"]["files"] == 1 and s["dir_stats"]["projects"]["files"] >= 8
    assert set(stores.read_misc(fx["claude_path"])) <= set(stores.MISC_DIRS) and s["dir_stats"]["telemetry"]["files"] == 1
    bundle = collect_bundle(fx["claude_path"], RecoveryOptions(include_desktop=False), lambda *a: None, lambda *a: None, lambda: False)
    assert bundle["file_history"] == s["file_history"] and bundle["misc"]["debug"]["files"] == 1 and set(bundle["misc"]) <= set(stores.MISC_DIRS)


def test_long_path_prefix_logic_for_windows(monkeypatch):
    import ntpath
    import types
    monkeypatch.setattr(paths, "os", types.SimpleNamespace(name="nt", path=ntpath))
    short = "C:\\Users\\me\\.claude\\projects\\p\\s.jsonl"
    long_ = "C:\\Users\\me\\AppData\\Local\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\Claude\\" + "\\".join(["a" * 36] * 6) + "\\x.jsonl"
    unc = "\\\\server\\share\\" + "d" * 250
    assert paths.long_path(short) == short
    assert paths.long_path(long_) == "\\\\?\\" + long_ and paths.long_path(unc) == "\\\\?\\UNC\\server\\share\\" + "d" * 250
    assert paths.long_path(paths.long_path(long_)) == paths.long_path(long_)             # idempotent
    monkeypatch.undo()
    if paths.os.name != "nt":                                                          # identity off Windows
        assert paths.long_path("a" * 300) == "a" * 300


def test_unreadable_transcripts_are_reported_not_silently_dropped(fx, monkeypatch, tmp_path):
    import os
    real = os.stat

    def flaky(path, *a, **k):
        if str(path).endswith(f"{fx['sessions']['old']}.jsonl"):
            raise OSError(36, "File name too long", str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(os, "stat", flaky)
    errs: list = []
    files = scanner.discover_code_transcripts(fx["claude_path"], errors=errs)
    assert len(files) == 5 and len(errs) == 1 and "too long" in errs[0]
    res = run_recovery(fx["claude_path"], tmp_path / "w.md", RecoveryOptions(include_desktop=False, per_sessions=False))
    assert any("260-character" in w for w in res.warnings) and "260-character" in (tmp_path / "w.md").read_text(encoding="utf-8")


def test_queued_command_attachment_is_a_queued_prompt(tmp_path):
    f = tmp_path / "projects" / "p" / "s.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "sessionId": "s", "message": {"content": "go"}},
        {"type": "queue-operation", "operation": "enqueue", "content": "then run lint", "sessionId": "s"},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "then run lint"}, "sessionId": "s"},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": [{"type": "text", "text": "and deploy"}]}, "sessionId": "s"},
        {"type": "attachment", "attachment": {"type": "edited_text_file"}, "sessionId": "s"}]), encoding="utf-8")
    rec = parse_transcript(f)
    assert rec.queued_prompts == ["then run lint", "and deploy"] and rec.type_counts["attachment/queued_command"] == 2
