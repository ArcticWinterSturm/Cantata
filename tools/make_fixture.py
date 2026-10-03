"""Synthetic ``~/.claude`` generator - fake data with planted PII and secrets.

Shaped like a real Claude Code install (docs/CLAUDE_STORAGE.md) so the parsers, the redactor
and the reports can be tested end to end without touching anyone's real data::

    python tools/make_fixture.py /tmp/fake_home
    python run.py cli recover --source /tmp/fake_home/.claude -o /tmp/report.md

``build_fixture`` returns a dict describing what was planted (secrets that must never reach a
report, PII that must vanish at *standard*, expected counts and derived timings).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- planted values ------------------------------------------------------------------------
SECRETS = {
    "anthropic_key": "sk-ant-api03-PLANTEDKEYabcdefghijklmnopqrstuvwxyz0123456789",
    "oauth_access": "sk-ant-oat01-PLANTEDACCESSabcdefghijklmnop0123456789",
    "oauth_refresh": "sk-ant-ort01-PLANTEDREFRESHabcdefghijklmnop0123456789",
    "github_pat": "ghp_PLANTEDgithubtoken0123456789abcdefghijkl",
    "aws_key": "AKIAPLANTEDAWSKEY123",
    "ide_auth": "9f8e7d6c-PLANTED-ide-auth-token-5544",
    "db_password": "PlantedDbPassw0rd!",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwbGFudGVkIn0.c2lnbmF0dXJlUExBTlRFRA",
    "account_uuid": "aaaaaaaa-1111-4222-8333-444444444444",
    "org_uuid": "bbbbbbbb-2222-4333-8444-555555555555",
    "user_id": "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00",
}
PII = {
    "email": "alice.planted@example.com",
    "user": "alice",
    "home": "/home/alice",
    "win_home": "C:\\Users\\Alice",
    "phone": "415-555-0142",
    "ip": "203.0.113.77",
    "org_name": "Planted Industries",
}

SID_MAIN = "11111111-aaaa-4bbb-8ccc-000000000001"      # rich session (today)
SID_OLD = "22222222-aaaa-4bbb-8ccc-000000000002"       # VS Code session, 28 days old
SID_WIN = "33333333-aaaa-4bbb-8ccc-000000000003"       # Windows-style project
SID_LEGACY = "44444444-aaaa-4bbb-8ccc-000000000004"    # inline sidechains, old layout
SID_GONE = "55555555-aaaa-4bbb-8ccc-000000000005"      # index-only: transcript aged out
AGENT_NEW = "a1b2c3d"                                   # subagents/agent-a1b2c3d.jsonl
AGENT_LEGACY = "deadbee"                                # inline sidechain, legacy

_counter = [0]


def _uid() -> str:
    _counter[0] += 1
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cantata-fixture-{_counter[0]}"))


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class _Chain:
    """Builds a linked list of transcript lines with realistic envelopes."""

    def __init__(self, sid: str, cwd: str, t0: datetime, *, version="2.1.280", entry="cli",
                 branch="main", slug="snug-fluttering-lollipop", sidechain=False, agent_id=None):
        self.sid, self.cwd, self.t, self.version, self.entry = sid, cwd, t0, version, entry
        self.branch, self.slug, self.side, self.agent = branch, slug, sidechain, agent_id
        self.parent: str | None = None
        self.lines: list[dict] = []

    def at(self, seconds: float) -> datetime:
        return self.t + timedelta(seconds=seconds)

    def _env(self, typ: str, when: datetime, **extra) -> dict:
        uid = _uid()
        rec = {"parentUuid": self.parent, "isSidechain": self.side, "userType": "external",
               "entrypoint": self.entry, "cwd": self.cwd, "sessionId": self.sid, "version": self.version,
               "gitBranch": self.branch, "type": typ, "uuid": uid, "timestamp": _z(when)}
        if typ == "assistant":
            rec["slug"] = self.slug
        if self.agent:
            rec["agentId"] = self.agent
        rec.update(extra)
        self.parent = uid
        self.lines.append(rec)
        return rec

    def prompt(self, text, s, **kw):
        return self._env("user", self.at(s), message={"role": "user", "content": text},
                         promptId=_uid(), permissionMode="default", **kw)

    def assistant(self, mid, blocks, s, *, usage=None, stop=None, model="claude-opus-4-5-20251101", **kw):
        msg = {"id": mid, "type": "message", "role": "assistant", "model": model, "content": blocks,
               "stop_reason": stop, "stop_sequence": None,
               "usage": usage or {"input_tokens": 120, "cache_creation_input_tokens": 500,
                                  "cache_read_input_tokens": 4000, "output_tokens": 60,
                                  "service_tier": "standard", "speed": "standard"}}
        return self._env("assistant", self.at(s), message=msg, requestId="req_" + mid[-8:], **kw)

    def tool_result(self, tool_id, content, s, tur=None, is_error=False):
        return self._env("user", self.at(s), message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}]},
            toolUseResult=tur if tur is not None else {"stdout": content, "stderr": "", "interrupted": False})

    def system(self, subtype, s, **kw):
        return self._env("system", self.at(s), subtype=subtype, level="info", **kw)

    def raw(self, rec: dict):
        self.lines.append(rec)

    def write(self, path: Path, corrupt_line: str | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for i, rec in enumerate(self.lines):
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if corrupt_line and i == len(self.lines) // 2:
                    fh.write(corrupt_line + "\n")


def _w(path: Path, text: str | dict | list, mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if isinstance(text, str) else json.dumps(text, indent=2), encoding="utf-8")
    if mtime:
        os.utime(path, (mtime, mtime))


def build_fixture(root: Path, now: datetime | None = None) -> dict:
    """Create ``<root>/.claude`` + ``<root>/.claude.json``. Returns planted/expected data."""
    root = Path(root)
    _counter[0] = 0
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    claude = root / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    S = SECRETS
    home = PII["home"]
    proj = f"{home}/projects/webapp"
    proj_dir = "-home-alice-projects-webapp"
    t_main = now - timedelta(hours=3)
    day = 86400

    # ======================= main session (multi-block responses, tools, subagent, compaction) =======
    c = _Chain(SID_MAIN, proj, t_main)
    c.raw({"type": "permission-mode", "permissionMode": "default", "sessionId": SID_MAIN})
    c.raw({"type": "file-history-snapshot", "messageId": _uid(), "isSnapshotUpdate": False,
           "snapshot": {"messageId": _uid(), "timestamp": _z(c.at(0)),
                        "trackedFileBackups": {f"{proj}/src/app.py": {"backupFileName": "abc123@v1", "version": 1}}}})
    c.prompt(f"Please fix the login bug. My email is {PII['email']}, call me on {PII['phone']}. "
             f"The server is {PII['ip']} and ANTHROPIC_API_KEY={S['anthropic_key']}", 0)
    # response 1: thinking + text + Bash tool_use  => THREE lines, same message.id, same usage
    m1 = "msg_01AAAAAAAAAAAAAAAAAAAAAA1"
    u1 = {"input_tokens": 200, "cache_creation_input_tokens": 800, "cache_read_input_tokens": 5000,
          "output_tokens": 150, "service_tier": "standard", "speed": "standard"}
    c.assistant(m1, [{"type": "thinking", "thinking": "User hit a login bug; inspect the auth module first.",
                      "signature": "sig"}], 2.4, usage=u1)
    c.assistant(m1, [{"type": "text", "text": "I'll start by reading the auth module."}], 3.1, usage=u1)
    c.assistant(m1, [{"type": "tool_use", "id": "toolu_01BASH0000000001", "name": "Bash",
                      "input": {"command": f"cat {home}/projects/webapp/.env && echo $DB_PASSWORD",
                                "description": "Show env"}}], 3.3, usage=u1, stop="tool_use")
    c.tool_result("toolu_01BASH0000000001",
                  f"DATABASE_URL=postgres://admin:{S['db_password']}@db.internal:5432/app\nGITHUB_TOKEN={S['github_pat']}",
                  5.3)                                                      # tool wall = 2000 ms
    # response 2: Read tool
    m2 = "msg_01BBBBBBBBBBBBBBBBBBBBBB2"
    u2 = {"input_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 6300,
          "output_tokens": 90, "service_tier": "standard", "speed": "fast"}
    c.assistant(m2, [{"type": "tool_use", "id": "toolu_01READ0000000002", "name": "Read",
                      "input": {"file_path": f"{proj}/src/auth.py"}}], 7.8, usage=u2, stop="tool_use")   # in-flight 2500
    c.tool_result("toolu_01READ0000000002", "def login(user):\n    return check(user)\n", 7.9,
                  tur={"type": "text", "file": {"filePath": f"{proj}/src/auth.py", "content": "def login...",
                                                "numLines": 2, "startLine": 1, "totalLines": 2}})
    # response 3: Task (subagent) tool_use -> result with agentId
    m3 = "msg_01CCCCCCCCCCCCCCCCCCCCCC3"
    u3 = {"input_tokens": 40, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 6500,
          "output_tokens": 70, "service_tier": "standard", "speed": "standard"}
    c.assistant(m3, [{"type": "tool_use", "id": "toolu_01TASK0000000003", "name": "Task",
                      "input": {"description": "Search for auth callers", "subagent_type": "Explore",
                                "prompt": "Find every caller of login() in the repo."}}], 9.0, usage=u3, stop="tool_use")
    c.tool_result("toolu_01TASK0000000003", "Found 3 callers: a.py, b.py, c.py", 16.0,
                  tur={"status": "completed", "agentId": AGENT_NEW, "totalDurationMs": 6400, "totalTokens": 9000,
                       "totalToolUseCount": 4, "prompt": "Find every caller of login()"})
    m4 = "msg_01DDDDDDDDDDDDDDDDDDDDDD4"
    c.assistant(m4, [{"type": "text", "text": f"Fixed. The bug was in {proj}/src/auth.py — token compare was inverted. "
                                              f"JWT seen: {S['jwt']}"}], 19.5,
                usage={"input_tokens": 30, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 7000,
                       "output_tokens": 120, "service_tier": "standard", "speed": "standard"}, stop="end_turn")
    c.system("turn_duration", 19.6, durationMs=19600)
    # turn 2 with API error + interruption
    c.prompt("Now run the tests", 60)
    c.assistant("msg_01ERR0000000000000000000E", [{"type": "text", "text": "API Error: 529 overloaded"}], 61,
                usage={"input_tokens": 0, "output_tokens": 0}, model="<synthetic>", isApiErrorMessage=True,
                error="overloaded_error")
    c.prompt("[Request interrupted by user]", 75)
    # compaction
    c.system("compact_boundary", 90, content="Conversation compacted",
             compactMetadata={"trigger": "auto", "preTokens": 155000})
    c.raw({"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True, "sessionId": SID_MAIN,
           "uuid": _uid(), "parentUuid": c.parent, "timestamp": _z(c.at(91)), "cwd": proj,
           "message": {"role": "user", "content": "This session is being continued from a previous conversation. "
                                                  f"Summary: user fixed login bug in {proj}/src/auth.py."}})
    c.raw({"type": "queue-operation", "operation": "enqueue", "timestamp": _z(c.at(95)), "sessionId": SID_MAIN,
           "content": "also update the changelog please"})
    for text in ("also update the changelog please", "and bump the version number"):      # newer builds inject queued prompts as attachments
        c.raw({"type": "attachment", "attachment": {"type": "queued_command", "prompt": text}, "uuid": _uid(), "sessionId": SID_MAIN,
               "timestamp": _z(c.at(96))})
    c.prompt("Thanks - write the changelog entry", 120)
    c.assistant("msg_01EEEEEEEEEEEEEEEEEEEEEE5", [{"type": "text", "text": "Added a changelog entry."}], 124.0,
                usage={"input_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 2000,
                       "output_tokens": 40, "service_tier": "standard", "speed": "standard"}, stop="end_turn")
    c.raw({"type": "attachment", "attachment": {"type": "edited_text_file", "filename": "x"}, "uuid": _uid(),
           "sessionId": SID_MAIN, "timestamp": _z(c.at(125))})
    c.raw({"type": "progress", "data": {"type": "hook_progress"}, "uuid": _uid(), "sessionId": SID_MAIN,
           "timestamp": _z(c.at(125.1))})
    c.raw({"type": "brand-new-line-type-from-2027", "payload": 1, "sessionId": SID_MAIN})
    c.raw({"type": "ai-title", "aiTitle": "Fix login bug in webapp", "sessionId": SID_MAIN})
    c.raw({"type": "custom-title", "customTitle": "Login bug hunt", "sessionId": SID_MAIN})
    c.raw({"type": "pr-link", "prUrl": "https://github.com/acme/webapp/pull/42", "sessionId": SID_MAIN})
    main_path = claude / "projects" / proj_dir / f"{SID_MAIN}.jsonl"
    c.write(main_path, corrupt_line='{"type": "user", "message": {truncated-by-crash')

    # subagent transcript (new layout)
    sa = _Chain(SID_MAIN, proj, t_main + timedelta(seconds=9.5), sidechain=True, agent_id=AGENT_NEW,
                slug="sunny-hatching-neumann")
    sa.prompt("Find every caller of login() in the repo.", 0)
    sa.assistant("msg_01SUB00000000000000000001", [
        {"type": "tool_use", "id": "toolu_01GREP0000000004", "name": "Grep", "input": {"pattern": "login\\(", "path": proj}}],
        1.2, usage={"input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0},
        stop="tool_use", model="claude-haiku-4-5-20251001")
    sa.tool_result("toolu_01GREP0000000004", "a.py:1\nb.py:7\nc.py:9", 1.4,
                   tur={"mode": "content", "numFiles": 3, "filenames": ["a.py", "b.py", "c.py"], "durationMs": 180})
    sa.assistant("msg_01SUB00000000000000000002", [{"type": "text", "text": "Found 3 callers: a.py, b.py, c.py"}], 3.0,
                 usage={"input_tokens": 15, "cache_read_input_tokens": 300, "output_tokens": 30, "cache_creation_input_tokens": 0},
                 stop="end_turn", model="claude-haiku-4-5-20251001")
    sa_path = claude / "projects" / proj_dir / SID_MAIN / "subagents" / f"agent-{AGENT_NEW}.jsonl"
    sa.write(sa_path)
    _w(sa_path.with_suffix(".meta.json"), {"agentType": "Explore", "description": "Search for auth callers"})
    _w(claude / "projects" / proj_dir / SID_MAIN / "tool-results" / "toolu_01BIG.txt", "x" * 2048)
    _w(claude / "projects" / proj_dir / "memory" / "MEMORY.md",
       f"# Memory\n- Project uses pytest. Owner email {PII['email']}.\n")
    _w(claude / "projects" / proj_dir / "memory" / "testing.md", "- run `pytest -x`\n")

    # ======================= old VS Code session (28 days) + orphaned variant ========================
    t_old = now - timedelta(days=28)
    o = _Chain(SID_OLD, proj, t_old, version="2.0.55", entry="claude-vscode", branch="dev", slug="quiet-wandering-hopper")
    o.prompt("Explain the project layout", 0)
    o.assistant("msg_01OLD000000000000000000A1", [{"type": "text", "text": "It is a Flask app with src/ and tests/."}], 2.0,
                usage={"input_tokens": 300, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0,
                       "output_tokens": 80, "service_tier": "standard"}, stop="end_turn", model="claude-sonnet-4-5-20250929")
    o.raw({"type": "summary", "summary": "Project layout overview", "leafUuid": o.parent})
    old_path = claude / "projects" / proj_dir / f"{SID_OLD}.jsonl"
    o.write(old_path)
    os.utime(old_path, ((now - timedelta(days=28)).timestamp(),) * 2)
    orphan = claude / "projects" / proj_dir / f"{SID_OLD}.orphaned-1760000000000-ab12cd.jsonl"
    o.write(orphan)
    os.utime(orphan, ((now - timedelta(days=29)).timestamp(),) * 2)

    # ======================= Windows-style project ===================================================
    wcwd = PII["win_home"] + "\\work\\api"
    w = _Chain(SID_WIN, wcwd, now - timedelta(days=2), slug="calm-soaring-turing")
    w.prompt("Add a health endpoint", 0)
    w.assistant("msg_01WIN000000000000000000W1", [
        {"type": "tool_use", "id": "toolu_01WRITE00000005", "name": "Write",
         "input": {"file_path": wcwd + "\\health.py", "content": "def health():\n    return 'ok'\n"}}], 1.0,
        usage={"input_tokens": 60, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 200, "output_tokens": 30},
        stop="tool_use")
    w.tool_result("toolu_01WRITE00000005", "File created successfully", 1.3,
                  tur={"type": "create", "filePath": wcwd + "\\health.py"})
    w.assistant("msg_01WIN000000000000000000W2", [{"type": "text", "text": "Done."}], 2.0,
                usage={"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 400, "output_tokens": 5},
                stop="end_turn")
    w.write(claude / "projects" / "C--Users-Alice-work-api" / f"{SID_WIN}.jsonl")

    # ======================= legacy layout: inline sidechains + agent-<id> next to parent ===========
    lcwd = f"{home}/projects/legacy"
    L = _Chain(SID_LEGACY, lcwd, now - timedelta(days=5), version="1.0.88", slug="old-school-layout")
    L.prompt("Audit the repo", 0)
    L.assistant("msg_01LEG00000000000000000L1", [
        {"type": "tool_use", "id": "toolu_01TASKL0000006", "name": "Task",
         "input": {"description": "Scan deps", "subagent_type": "general-purpose", "prompt": "List deps"}}], 1.0,
        usage={"input_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 10},
        stop="tool_use")
    side = _Chain(SID_LEGACY, lcwd, now - timedelta(days=5) + timedelta(seconds=1.5), version="1.0.88",
                  sidechain=True, agent_id=AGENT_LEGACY)
    side.prompt("List deps", 0)
    side.assistant("msg_01LEGSIDE0000000000000S1", [{"type": "text", "text": "requests, flask"}], 1.0,
                   usage={"input_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 4},
                   stop="end_turn")
    for rec in side.lines:
        L.raw(rec)
    L.tool_result("toolu_01TASKL0000006", "requests, flask", 4.0, tur={"status": "completed", "agentId": AGENT_LEGACY})
    L.assistant("msg_01LEG00000000000000000L2", [{"type": "text", "text": "Deps: requests, flask."}], 5.0,
                usage={"input_tokens": 8, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 50, "output_tokens": 6},
                stop="end_turn")
    L.write(claude / "projects" / "-home-alice-projects-legacy" / f"{SID_LEGACY}.jsonl")

    # ======================= indexes ===================================================================
    _w(claude / "projects" / proj_dir / "sessions-index.json", {
        "version": 1, "originalPath": proj, "entries": [
            {"sessionId": SID_MAIN, "fullPath": str(main_path), "fileMtime": int(now.timestamp() * 1000),
             "firstPrompt": "Please fix the login bug", "summary": "Login bug triage", "messageCount": 14,
             "created": _z(t_main), "modified": _z(now), "gitBranch": "main", "projectPath": proj, "isSidechain": False},
            {"sessionId": SID_GONE, "fullPath": str(claude / "projects" / proj_dir / f"{SID_GONE}.jsonl"),
             "fileMtime": int((now - timedelta(days=45)).timestamp() * 1000), "firstPrompt": "Refactor the billing module",
             "summary": "Billing refactor (aged out)", "messageCount": 88, "created": _z(now - timedelta(days=45)),
             "modified": _z(now - timedelta(days=44)), "gitBranch": "billing", "projectPath": proj, "isSidechain": False}]})

    ms = lambda dt: int(dt.timestamp() * 1000)
    hist = [
        {"display": "Please fix the login bug", "pastedContents": {}, "timestamp": ms(t_main), "project": proj, "sessionId": SID_MAIN},
        {"display": "Now run the tests", "pastedContents": {}, "timestamp": ms(t_main + timedelta(seconds=60)), "project": proj, "sessionId": SID_MAIN},
        {"display": "Explain the project layout", "pastedContents": {}, "timestamp": ms(t_old), "project": proj, "sessionId": SID_OLD},
        {"display": "Refactor the billing module", "pastedContents": {}, "timestamp": ms(now - timedelta(days=45)), "project": proj, "sessionId": SID_GONE},
        {"display": f"deploy with token {S['github_pat']}", "pastedContents": {"1": {"id": 1, "type": "text", "contentHash": "abc"}},
         "timestamp": ms(now - timedelta(days=45, hours=-1)), "project": proj, "sessionId": SID_GONE},
    ]
    (claude / "history.jsonl").write_text("\n".join(json.dumps(h) for h in hist) + "\n", encoding="utf-8")

    _w(claude / "stats-cache.json", {
        "version": 1, "lastComputedDate": now.strftime("%Y-%m-%d"),
        "dailyActivity": [{"date": now.strftime("%Y-%m-%d"), "messageCount": 40, "sessionCount": 3, "toolCallCount": 9}],
        "dailyModelTokens": [{"date": now.strftime("%Y-%m-%d"), "tokensByModel": {"claude-opus-4-5-20251101": 4200}}],
        "modelUsage": {"claude-opus-4-5-20251101": {"inputTokens": 900, "outputTokens": 1500, "cacheReadInputTokens": 300000,
                                                     "cacheCreationInputTokens": 9000, "webSearchRequests": 0, "costUSD": 0, "contextWindow": 0}},
        "totalSessions": 9, "totalMessages": 210,
        "longestSession": {"sessionId": SID_MAIN, "duration": 3_600_000, "messageCount": 40, "timestamp": _z(t_main)},
        "firstSessionDate": _z(now - timedelta(days=60)), "hourCounts": {"9": 4, "14": 7}})

    # ======================= config & state ===========================================================
    _w(claude / "settings.json", {
        "cleanupPeriodDays": 30, "model": "claude-opus-4-5",
        "permissions": {"allow": ["Bash(git status)", "Read(*)"], "deny": ["Bash(rm -rf *)"], "defaultMode": "default"},
        "hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "prettier --write $FILE"}]}]},
        "env": {"ANTHROPIC_API_KEY": S["anthropic_key"], "DEBUG": "1", "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer " + S["jwt"]},
        "statusLine": {"type": "command", "command": "~/.claude/statusline.sh"}})
    _w(claude / "settings.local.json", {"permissions": {"allow": ["WebFetch(domain:docs.python.org)"]}})
    _w(claude / "CLAUDE.md", f"# Global memory\nAlways write tests. Contact: {PII['email']}\n")
    _w(claude / "agents" / "reviewer.md", "---\nname: reviewer\ndescription: Reviews diffs\ntools: Read, Grep\n---\nYou review code.\n")
    _w(claude / "skills" / "pdf" / "SKILL.md", "---\nname: pdf\ndescription: Work with PDFs\n---\nUse pdftotext.\n")
    _w(claude / "commands" / "deploy.md", "---\ndescription: Deploy\n---\nRun the deploy script.\n")
    _w(claude / "rules" / "style.md", "Use type hints.\n")
    _w(claude / "plugins" / "installed_plugins.json",
       {"version": 2, "plugins": {"review-tools@acme-marketplace": [{"scope": "user", "version": "1.2.0", "installPath": f"{home}/.claude/plugins/cache/x"}]}})
    _w(claude / ".credentials.json", {"claudeAiOauth": {
        "accessToken": S["oauth_access"], "refreshToken": S["oauth_refresh"],
        "expiresAt": ms(now + timedelta(hours=6)), "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}})
    _w(claude / "ide" / "12345.lock", {"pid": 4242, "workspaceFolders": [proj], "ideName": "Visual Studio Code",
                                        "transport": "ws", "authToken": S["ide_auth"]})
    _w(claude / "todos" / f"{SID_MAIN}-agent-{SID_MAIN}.json",
       [{"content": "Fix login", "status": "completed", "activeForm": "Fixing login"},
        {"content": "Write changelog", "status": "pending", "activeForm": "Writing changelog"}])
    _w(claude / "tasks" / "list1" / "1.json", {"id": "1", "subject": "Investigate", "status": "completed"})
    _w(claude / "plans" / "brave-tiger-dancing.md", f"# Plan: fix login\n1. Read auth module\n2. Patch compare\nOwner {PII['email']}\n")
    _w(claude / "file-history" / SID_MAIN / "abc123@v1", "def login(): pass\n")
    _w(claude / "shell-snapshots" / "snapshot-bash-1760000000-x1y2.sh", f"export SECRET_TOKEN={S['github_pat']}\nalias ll='ls -l'\n")
    _w(claude / "debug" / f"{SID_MAIN}.txt", f"[DEBUG] started\n[DEBUG] Authorization: Bearer {S['jwt']}\n[DEBUG] done\n")
    _w(claude / "paste-cache" / "abc123.txt", "pasted blob\n")
    _w(claude / "telemetry" / f"1p_failed_events.{SID_MAIN}.{_uid()}.json", [{"event_type": "ClaudeCodeInternalEvent", "event_data": {"event_name": "tengu_api_success"}}])
    _w(claude / "backups" / ".claude.json.backup.1760000000000", {"numStartups": 1})
    _w(claude / "usage-data" / "session-meta" / f"{SID_MAIN}.json", {"session_id": SID_MAIN, "project_path": proj, "duration_minutes": 3})
    _w(claude / "cache" / "changelog.md", "# Changelog\n2.1.280\n")

    global_json = {
        "numStartups": 435, "installMethod": "native", "autoUpdates": False, "theme": "dark",
        "userID": S["user_id"], "firstStartTime": _z(now - timedelta(days=60)), "hasCompletedOnboarding": True,
        "oauthAccount": {"accountUuid": S["account_uuid"], "emailAddress": PII["email"], "organizationUuid": S["org_uuid"],
                         "organizationName": PII["org_name"], "organizationRole": "admin", "displayName": "Alice P."},
        "mcpServers": {"github": {"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                                  "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": S["github_pat"]}},
                       "docs": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer " + S["jwt"]}}},
        "projects": {proj: {"allowedTools": [], "hasTrustDialogAccepted": True, "mcpServers": {}, "lastSessionId": SID_MAIN,
                            "lastCost": 1.2345, "lastAPIDuration": 41234, "lastDuration": 183000, "lastLinesAdded": 12,
                            "lastLinesRemoved": 4, "lastTotalInputTokens": 340, "lastTotalOutputTokens": 410,
                            "lastModelUsage": {"claude-opus-4-5-20251101": {"inputTokens": 340, "outputTokens": 410,
                                                                             "cacheReadInputTokens": 18000, "costUSD": 1.2345}}},
                     wcwd: {"hasTrustDialogAccepted": False}},
        "tipsHistory": {"shift-enter": 431}, "cachedGrowthBookFeatures": {"a": True, "b": False}}
    _w(root / ".claude.json", global_json)

    return {
        "root": str(root), "claude": str(claude), "global_json": str(root / ".claude.json"),
        "secrets": SECRETS, "pii": PII,
        "sessions": {"main": SID_MAIN, "old": SID_OLD, "win": SID_WIN, "legacy": SID_LEGACY, "gone": SID_GONE},
        "expected": {
            # per-response derived timings (ms) for SID_MAIN turn 1
            "req1_in_flight_ms": 2400, "req1_stream_ms": 900, "req1_total_ms": 3300,
            "req2_in_flight_ms": 2500, "tool_bash_wall_ms": 2000, "task_wall_ms": 7000, "turn1_reported_ms": 19600,
            "main_usage": {"input": 200 + 50 + 40 + 30 + 20, "output": 150 + 90 + 70 + 120 + 40,
                           "cache_read": 5000 + 6300 + 6500 + 7000 + 2000, "cache_write": 800},
            "main_turns": 4,          # 3 typed prompts + 1 compaction-summary turn
            "main_api_errors": 1, "main_compactions": 1, "corrupt_lines": 1,
            "main_sessions_files": 6,           # main, old, orphaned, win, legacy (+ main variants counted in tests)
        },
        "paths": {"main": str(main_path), "subagent": str(sa_path), "old": str(old_path), "orphan": str(orphan)},
    }


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "fake_home")
    info = build_fixture(target)
    print(json.dumps({k: v for k, v in info.items() if k in ("root", "claude", "sessions", "paths")}, indent=2))
