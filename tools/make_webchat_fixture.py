"""Synthetic Claude Desktop tree + Chromium profile + claude.ai export zip (fake data, planted PII).

    python tools/make_webchat_fixture.py /tmp/wc

Builds (under ``<root>``)::

    desktop/Claude/…                 Cowork manifests + transcripts + audit.jsonl, Code-tab wrappers, memory, logs
    browser/Default/…                Chromium profile: History (sqlite), IndexedDB / Local Storage LevelDB stubs,
                                     Claude-in-Chrome extension store
    exports/claude-export.zip        conversations.json / projects.json / users.json
    exports/single-conversation.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_fixture import PII, SECRETS, SID_MAIN, _Chain, _w, _z  # noqa: E402

ACCT = "acc00000-0000-4000-8000-000000000001"
ORG = "org00000-0000-4000-8000-000000000002"
COWORK_LOCAL = "local_7876b7f6-7ea3-4d3e-a128-38fe1e18266e"
COWORK_CLI = "4368fcb8-373d-4ba7-9952-3beee2a56818"
COWORK_FAILED = "local_deadbeef-0000-4000-8000-00000000fa11"
WRAP_OK = "local_c0de0000-1111-4222-8333-000000000001"
WRAP_MISSING = "local_c0de0000-1111-4222-8333-000000000002"
CLI_GONE = "99999999-aaaa-4bbb-8ccc-000000000099"
CHAT_A = "a1111111-2222-4333-8444-555555555555"      # in export AND browser history
CHAT_B = "b1111111-2222-4333-8444-555555555555"      # export only
CHAT_C = "c1111111-2222-4333-8444-555555555555"      # browser history only (server-side)
PROJ_ID = "d1111111-2222-4333-8444-555555555555"
EXT_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"


def _chrome_us(dt: datetime) -> int:
    return int((dt.timestamp() + 11_644_473_600) * 1_000_000)


def build_webchat_fixture(root: Path, now: datetime | None = None) -> dict:
    root = Path(root)
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    ms = lambda dt: int(dt.timestamp() * 1000)

    # ================= Claude Desktop =====================================================================
    app = root / "desktop" / "Claude"
    app.mkdir(parents=True, exist_ok=True)
    _w(app / "claude_desktop_config.json", {
        "mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/alice/docs"],
                                      "env": {"API_TOKEN": SECRETS["github_pat"]}}},
        "preferences": {"menuBarEnabled": True, "quickEntryShortcut": "Alt+Space"}})
    _w(app / "config.json", {"locale": "en-US", "oauth:tokenCache": "v10ENCRYPTEDBLOBxxxxxxxxxxxxxxxx", "theme": "dark"})
    (app / "ant-did").write_text("ZGV2aWNlLWlkLXBsYW50ZWQ=", encoding="utf-8")
    _w(app / "logs" / "main.log", f"[info] app started\n[info] user {PII['email']} signed in\n[warn] Bearer {SECRETS['jwt']}\n")
    _w(app / "logs" / "mcp.log", "[info] server filesystem connected\n")
    _w(app / "Claude Extensions" / "ant.dir.gh.example.ext" / "manifest.json", {"name": "example"})
    _w(app / "vm_bundles" / "rootfs.img", "x" * 4096)

    # --- Cowork session (full transcript + audit + memory) -------------------------------------------------
    base = app / "local-agent-mode-sessions" / ACCT / ORG
    created = now - timedelta(days=2)
    _w(base / f"{COWORK_LOCAL}.json", {
        "sessionId": COWORK_LOCAL, "processName": "charming-adoring-darwin", "vmProcessName": "charming-adoring-darwin",
        "cwd": "/sessions/charming-adoring-darwin", "userSelectedFolders": ["/home/alice/Documents/reports"],
        "createdAt": ms(created), "lastActivityAt": ms(created + timedelta(minutes=14)), "model": "claude-opus-4-5",
        "isArchived": False, "title": "Review customer persona research", "initialMessage": "Please review the persona research",
        "systemPrompt": "You are Claude in Cowork. " * 400, "egressAllowedDomains": ["*"], "accountName": "Alice Planted",
        "emailAddress": PII["email"], "enabledMcpTools": {"abc:read": True, "abc:write": False}, "remoteMcpServersConfig": [{"name": "x"}],
        "cliSessionId": COWORK_CLI, "slashCommands": ["/help"], "scheduledTaskId": "etsy-daily-metrics"})
    cw_dir = base / COWORK_LOCAL
    c = _Chain(COWORK_CLI, "/sessions/charming-adoring-darwin", created, version="2.1.250", entry="sdk-cli", slug="charming-adoring-darwin")
    c.prompt(f"Please review the persona research. Contact {PII['email']}", 0)
    c.assistant("msg_01CW000000000000000000001", [{"type": "text", "text": "Reading the files now."}, ], 2.0, stop="end_turn")
    c.prompt("Summarise the top three personas", 60)
    c.assistant("msg_01CW000000000000000000002", [{"type": "tool_use", "id": "toolu_01CWREAD000001", "name": "Read",
                                                  "input": {"file_path": "/sessions/charming-adoring-darwin/mnt/reports/personas.md"}}], 63.0, stop="tool_use")
    c.tool_result("toolu_01CWREAD000001", "Persona A: developers...", 63.4)
    c.assistant("msg_01CW000000000000000000003", [{"type": "text", "text": "Top personas: A, B, C."}], 66.0, stop="end_turn")
    tdir = cw_dir / ".claude" / "projects" / "-sessions-charming-adoring-darwin"
    c.write(tdir / f"{COWORK_CLI}.jsonl")
    sa = _Chain(COWORK_CLI, "/sessions/charming-adoring-darwin", created + timedelta(seconds=64), sidechain=True, agent_id="cwa1b2c")
    sa.prompt("Extract persona names", 0)
    sa.assistant("msg_01CWSUB0000000000000000001", [{"type": "text", "text": "A, B, C"}], 1.5, stop="end_turn")
    sa.write(tdir / COWORK_CLI / "subagents" / "agent-cwa1b2c.jsonl")
    audit = [
        {"type": "system", "subtype": "init", "model": "claude-opus-4-5", "claude_code_version": "2.1.250", "tools": ["Read", "Write", "Bash"],
         "mcp_servers": [{"name": "filesystem"}], "permissionMode": "default", "_audit_timestamp": _z(created)},
        {"type": "user", "message": {"role": "user", "content": "Please review"}, "_audit_timestamp": _z(created), "_audit_hmac": "aa11"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, "_audit_timestamp": _z(created + timedelta(seconds=2)), "_audit_hmac": "bb22"},
        {"type": "rate_limit_event", "_audit_timestamp": _z(created + timedelta(seconds=30)), "_audit_hmac": "cc33"},
        {"type": "result", "subtype": "success", "total_cost_usd": 8.64, "duration_ms": 840000, "duration_api_ms": 512000, "num_turns": 88,
         "usage": {"input_tokens": 1000}, "modelUsage": {}, "_audit_timestamp": _z(created + timedelta(minutes=14)), "_audit_hmac": "dd44"},
    ]
    (cw_dir / "audit.jsonl").write_text("\n".join(json.dumps(a) for a in audit) + "\n", encoding="utf-8")
    (cw_dir / ".audit-key").write_bytes(b"ENCRYPTED-KEY-DO-NOT-READ")
    _w(cw_dir / "uploads" / "personas.md", "# personas\n")
    _w(cw_dir / "outputs" / "summary.md", "# summary\n")
    _w(base / "memory" / "CLAUDE.md", "# Cowork memory\nPrefers concise answers.\n")
    _w(base / "memory" / "memory" / "preferences.md", "- likes tables\n")
    _w(base / "spaces" / PROJ_ID / "memory" / "project_notes.md", "- project space notes\n")
    _w(base / "cowork_account_settings.json", {"displayName": "Alice", "locale": "en-US", "memoryEnabled": True})
    # failed session (no cliSessionId, no transcripts)
    _w(base / f"{COWORK_FAILED}.json", {"sessionId": COWORK_FAILED, "processName": "sad-failing-hopper", "createdAt": ms(now - timedelta(days=1)),
                                        "lastActivityAt": ms(now - timedelta(days=1)), "model": "claude-opus-4-5", "title": "Failed start",
                                        "error": "RPC error -1: failed to ensure virtiofs mount", "accountName": "Alice Planted", "emailAddress": PII["email"]})

    # --- Code-tab wrappers (metadata only) -------------------------------------------------------------------
    cbase = app / "claude-code-sessions" / ACCT / ORG
    _w(cbase / f"{WRAP_OK}.json", {"sessionId": WRAP_OK, "cliSessionId": SID_MAIN, "cwd": "/home/alice/projects/webapp", "originCwd": "/home/alice/projects/webapp",
                                   "createdAt": ms(now - timedelta(hours=3)), "lastActivityAt": ms(now - timedelta(hours=2)), "model": "claude-opus-4-5",
                                   "effort": "high", "isArchived": False, "title": "Login bug (desktop)", "titleSource": "auto",
                                   "permissionMode": "default", "completedTurns": 3, "worktreePath": "/home/alice/projects/webapp/.wt-x",
                                   "sourceBranch": "main", "branch": "claude/x"})
    _w(cbase / f"{WRAP_MISSING}.json", {"sessionId": WRAP_MISSING, "cliSessionId": CLI_GONE, "cwd": "/home/alice/projects/old", "createdAt": ms(now - timedelta(days=40)),
                                        "lastActivityAt": ms(now - timedelta(days=39)), "model": "claude-sonnet-4-5", "title": "Old refactor (transcript swept)",
                                        "isArchived": True, "completedTurns": 12})

    # ================= Chromium profile ===================================================================
    prof = root / "browser" / "Default"
    prof.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(prof / "History")
    con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, typed_count INTEGER, last_visit_time INTEGER, hidden INTEGER)")
    rows = [
        (f"https://claude.ai/chat/{CHAT_A}", "Quarterly planning - Claude", 7, _chrome_us(now - timedelta(days=1))),
        (f"https://claude.ai/chat/{CHAT_C}", "Trip itinerary ideas - Claude", 3, _chrome_us(now - timedelta(days=4))),
        (f"https://claude.ai/project/{PROJ_ID}", "Research notes - Claude", 2, _chrome_us(now - timedelta(days=2))),
        ("https://claude.ai/new", "Claude", 11, _chrome_us(now)),
        ("https://claude.ai/recents", "Chats - Claude", 4, _chrome_us(now - timedelta(days=3))),
        ("https://example.com/not-claude", "Example", 9, _chrome_us(now)),
    ]
    for i, (u, t, v, ts) in enumerate(rows, 1):
        con.execute("INSERT INTO urls VALUES (?,?,?,?,?,?,?)", (i, u, t, v, 0, ts, 0))
    con.commit()
    con.close()
    idb = prof / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"
    idb.mkdir(parents=True, exist_ok=True)
    (idb / "000003.log").write_bytes(b"\x01\x02\x03 conversation " + CHAT_A.encode() + b" \x00\x00 " + CHAT_C.encode() + b"\x04")
    (idb / "000005.ldb").write_bytes(os.urandom(2048))
    ls = prof / "Local Storage" / "leveldb"
    ls.mkdir(parents=True, exist_ok=True)
    (ls / "000003.log").write_bytes(b"\x00_https://claude.ai\x00\x01lastActiveOrg\x01" + ORG.encode())
    (prof / "Preferences").write_text("{}", encoding="utf-8")
    ext = prof / "Local Extension Settings" / EXT_ID
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "000003.log").write_bytes(b"accessToken" + SECRETS["oauth_access"].encode() + b"refreshToken" + SECRETS["oauth_refresh"].encode())

    # ================= Export ===================================================================================
    exp = root / "exports"
    exp.mkdir(parents=True, exist_ok=True)
    t0 = now - timedelta(days=10)

    def msg(uid, sender, text, dt_, content=None, attachments=None, files=None):
        return {"uuid": uid, "text": text, "content": content if content is not None else [{"type": "text", "text": text, "start_timestamp": _z(dt_),
                "stop_timestamp": _z(dt_), "citations": []}], "sender": sender, "index": 0, "created_at": _z(dt_), "updated_at": _z(dt_),
                "truncated": False, "attachments": attachments or [], "files": files or [], "sync_sources": [], "parent_message_uuid": "00000000-0000-4000-8000-000000000000"}

    conv_a = {"uuid": CHAT_A, "name": "Quarterly planning", "summary": "", "created_at": _z(t0), "updated_at": _z(t0 + timedelta(hours=1)),
              "account": {"uuid": SECRETS["account_uuid"]}, "model": "claude-opus-4-5", "chat_messages": [
                  msg("m1", "human", f"Plan Q4. Email me at {PII['email']} or call {PII['phone']}. Key {SECRETS['anthropic_key']}", t0,
                      attachments=[{"file_name": "budget.csv", "file_size": 120, "file_type": "text/csv", "extracted_content": f"owner,{PII['email']}\nq4,1000"}]),
                  msg("m2", "assistant", "Here is a plan", t0 + timedelta(seconds=9), content=[
                      {"type": "thinking", "thinking": "Consider budget constraints first.", "start_timestamp": _z(t0), "stop_timestamp": _z(t0)},
                      {"type": "text", "text": "Here is a plan for Q4:\n1. Hire\n2. Ship", "start_timestamp": _z(t0), "stop_timestamp": _z(t0), "citations": []},
                      {"type": "tool_use", "name": "artifacts", "input": {"id": "plan", "type": "text/markdown", "title": "Q4 plan", "command": "create",
                                                                          "content": "# Q4 plan\n- hire\n- ship"}}])]}
    conv_b = {"uuid": CHAT_B, "name": "Recipe ideas", "summary": "Dinner", "created_at": _z(t0 + timedelta(days=1)), "updated_at": _z(t0 + timedelta(days=1, minutes=5)),
              "account": {"uuid": SECRETS["account_uuid"]}, "chat_messages": [
                  msg("m3", "human", "Dinner ideas using lentils?", t0 + timedelta(days=1)), msg("m4", "assistant", "Try a lentil curry.", t0 + timedelta(days=1, seconds=5))]}
    projects = [{"uuid": PROJ_ID, "name": "Research notes", "description": "notes", "is_private": True, "created_at": _z(t0), "updated_at": _z(t0),
                 "creator": {"uuid": SECRETS["account_uuid"], "full_name": "Alice Planted"},
                 "docs": [{"uuid": "doc1", "filename": "notes.md", "content": f"Findings by {PII['email']}", "created_at": _z(t0)}]}]
    users = [{"uuid": SECRETS["account_uuid"], "full_name": "Alice Planted", "email_address": PII["email"], "verified_phone_number": "+14155550142"}]
    zpath = exp / "claude-export.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", json.dumps([conv_a, conv_b]))
        zf.writestr("projects.json", json.dumps(projects))
        zf.writestr("users.json", json.dumps(users))
    single = exp / "single-conversation.json"
    single.write_text(json.dumps(conv_b), encoding="utf-8")

    return {"root": str(root), "desktop_root": str(app), "browser_root": str(root / "browser"), "profile": str(prof), "zip": str(zpath),
            "single": str(single),
            "ids": {"cowork_local": COWORK_LOCAL, "cowork_cli": COWORK_CLI, "wrap_ok": WRAP_OK, "wrap_missing": WRAP_MISSING,
                    "chat_a": CHAT_A, "chat_b": CHAT_B, "chat_c": CHAT_C},
            "expected": {"cowork_sessions": 2, "code_tab": 2, "export_conversations": 2, "export_messages": 4, "browser_visits": 7 + 3 + 2 + 11 + 4,
                         "cowork_cost": 8.64, "threads_total": 2 + 2 + 2 + 1},
            "secrets": SECRETS, "pii": PII}


if __name__ == "__main__":
    out = build_webchat_fixture(Path(sys.argv[1] if len(sys.argv) > 1 else "wc_fixture"))
    print(json.dumps({k: out[k] for k in ("root", "desktop_root", "browser_root", "zip")}, indent=2))
