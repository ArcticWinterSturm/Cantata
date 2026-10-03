"""Claude Code transcript parser (``projects/**/*.jsonl``, also Cowork's copies).

Streams line by line (progress every ~2 MB), never loads a file whole, counts corrupt lines
instead of failing, and is tolerant of the schema drifting between releases: unknown line
types are *counted* (``SessionRecord.type_counts``), unknown fields are ignored.

The two things that trip up naive readers are handled here:

* one API response is written as **several lines** (one per content block) that all repeat the
  same ``message.id`` *and the same ``usage``* - tokens are merged per ``message.id``;
* a ``user`` line is either a typed prompt **or** a batch of tool results - they are told apart
  by content blocks, and tool results are attached to the originating ``tool_use``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import paths
from .models import (MAX_TEXT, MAX_TOOL_OUTPUT, ApiRequest, Message, RecoveryCancelled,
                     SessionRecord, TokenUsage, ToolCall, Turn)
from .scanner import TranscriptFile

PROGRESS_EVERY = 2 * 1024 * 1024
_FRAC = re.compile(r"\.(\d+)")
_CMD_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_CMD_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
_ENTRYPOINT = {
    "cli": "cli", "claude-vscode": "vscode", "vscode": "vscode", "claude-desktop": "desktop-code",
    "desktop": "desktop-code", "sdk-cli": "sdk", "sdk-ts": "sdk", "sdk-py": "sdk", "mcp": "mcp",
    "claude-jetbrains": "jetbrains", "jetbrains": "jetbrains", "local-agent": "desktop-cowork",
}


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #

def parse_ts(value: Any) -> Optional[float]:
    """ISO-8601 (``Z`` or offset) or epoch seconds/milliseconds -> epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    try:
        s2 = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        s2 = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s2, count=1)
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        try:
            return parse_ts(float(s))
        except ValueError:
            return None


def ms_between(a: Any, b: Any) -> Optional[int]:
    ta, tb = parse_ts(a), parse_ts(b)
    if ta is None or tb is None or tb < ta:
        return None
    return int(round((tb - ta) * 1000))


def iso_from_epoch(value: Any) -> str:
    t = parse_ts(value)
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if t else ""


# --------------------------------------------------------------------------- #
# content helpers
# --------------------------------------------------------------------------- #

def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n… [{len(text) - limit:,} more characters truncated]", True


def content_text(content: Any) -> str:
    """Flatten a message ``content`` (string or block list) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                bt = b.get("type")
                if bt == "text":
                    parts.append(str(b.get("text", "")))
                elif bt == "image":
                    parts.append("[image]")
                elif bt == "document":
                    parts.append(f"[document: {(b.get('title') or (b.get('source') or {}).get('media_type') or '')}]")
                elif bt == "tool_reference":
                    parts.append(f"[tool: {b.get('tool_name', '')}]")
                elif bt in ("tool_use", "tool_result", "thinking", "redacted_thinking"):
                    continue
                elif "text" in b:
                    parts.append(str(b["text"]))
    elif isinstance(content, dict):
        return str(content.get("text", ""))
    return "\n\n".join(p for p in parts if p)


def summarize_tool(name: str, inp: Any) -> tuple[str, str]:
    """(one-line summary, fuller input text) for a tool invocation."""
    if not isinstance(inp, dict):
        s = str(inp)
        return s[:160].replace("\n", " "), s
    n = name or ""

    def g(key: str) -> str:
        v = inp.get(key)
        return "" if v is None else str(v)

    if n == "Bash" or n == "PowerShell":
        cmd = g("command")
        desc = g("description")
        return (cmd.split("\n")[0][:160] or desc), cmd + (f"\n# {desc}" if desc else "")
    if n == "Read":
        rng = f" [{g('offset')}+{g('limit')}]" if inp.get("offset") or inp.get("limit") else ""
        return g("file_path") + rng, g("file_path") + rng
    if n in ("Edit", "MultiEdit"):
        if n == "MultiEdit":
            edits = inp.get("edits") or []
            text = "\n".join(f"- {str(e.get('old_string', ''))[:300]!r}\n+ {str(e.get('new_string', ''))[:300]!r}"
                             for e in edits if isinstance(e, dict))
            return f"{g('file_path')} ({len(edits)} edits)", g("file_path") + "\n" + text
        old, new = g("old_string"), g("new_string")
        diff = "".join(f"- {ln}\n" for ln in old.splitlines()[:40]) + "".join(f"+ {ln}\n" for ln in new.splitlines()[:40])
        return g("file_path"), g("file_path") + "\n" + diff
    if n == "Write":
        content = g("content")
        return f"{g('file_path')} ({len(content):,} chars)", g("file_path") + "\n" + content[:4000]
    if n in ("Glob", "Grep"):
        s = g("pattern") + (f"  in {g('path')}" if inp.get("path") else "")
        return s, json.dumps(inp, ensure_ascii=False)[:1000]
    if n in ("Task", "Agent"):
        kind = g("subagent_type") or "agent"
        return f"{kind}: {g('description')}", g("prompt")
    if n == "TodoWrite":
        todos = inp.get("todos") or []
        text = "\n".join(f"[{t.get('status', '?')}] {t.get('content', '')}" for t in todos if isinstance(t, dict))
        return f"{len(todos)} todos", text
    if n == "WebFetch":
        return g("url"), g("url") + ("\n" + g("prompt") if inp.get("prompt") else "")
    if n == "WebSearch":
        return g("query"), g("query")
    if n == "NotebookEdit":
        return g("notebook_path"), json.dumps(inp, ensure_ascii=False)[:1500]
    if n == "ExitPlanMode":
        return "plan", g("plan")
    if n == "Skill":
        return g("skill") or g("name"), json.dumps(inp, ensure_ascii=False)[:500]
    text = json.dumps(inp, ensure_ascii=False, default=str)
    return text[:160], text


def _origin(entrypoint: str) -> str:
    return _ENTRYPOINT.get(entrypoint, entrypoint)


def _add_unique(lst: list, value: Any) -> None:
    if value and value not in lst:
        lst.append(value)


# --------------------------------------------------------------------------- #
# the builder
# --------------------------------------------------------------------------- #

class _Builder:
    def __init__(self, rec: SessionRecord, *, foreign_ok: bool = False, route_sidechains: bool = False):
        self.rec = rec
        self.foreign_ok = foreign_ok
        self.route = route_sidechains
        self.cur: Optional[Turn] = None
        self.msgs: dict[str, Message] = {}
        self.calls: dict[str, ToolCall] = {}
        self.usage: dict[str, TokenUsage] = {}
        self.model_of: dict[str, str] = {}
        self.day_of: dict[str, str] = {}
        self.reqs: dict[str, ApiRequest] = {}
        self.req_turn: dict[str, Turn] = {}
        self.msg_turn: dict[str, Turn] = {}
        self.stop_seen: set[str] = set()
        self.error_seen: set[str] = set()
        self.last_user_ts = ""
        self.children: dict[str, "_Builder"] = {}
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.snapshots: set[str] = set()
        self.summaries: list[str] = []

    # ---------------------------------------------------------------- feed ---
    def feed(self, d: dict) -> None:
        rec = self.rec
        t = d.get("type")
        if not isinstance(t, str):
            t = "unknown"

        # route legacy inline sidechains (main file, isSidechain + agentId) to a child builder
        if self.route and d.get("isSidechain") and d.get("agentId") and t in ("user", "assistant", "system"):
            aid = str(d["agentId"])
            child = self.children.get(aid)
            if child is None:
                crec = SessionRecord(session_id=f"agent-{aid}", kind="subagent", agent_id=aid,
                                     parent_session_id=rec.session_id, project_dir=rec.project_dir,
                                     source_path=rec.source_path)
                child = self.children[aid] = _Builder(crec, foreign_ok=True)
            child.feed(d)
            return

        rec.type_counts[t] += 1
        ts = d.get("timestamp") or d.get("_audit_timestamp") or ""
        pts = parse_ts(ts)
        if pts is not None:
            if self.first_ts is None or pts < self.first_ts:
                self.first_ts, rec.created_at = pts, ts
            if self.last_ts is None or pts > self.last_ts:
                self.last_ts, rec.updated_at = pts, ts

        sid = d.get("sessionId")
        if sid:
            if rec.is_subagent:
                if not rec.parent_session_id:
                    rec.parent_session_id = str(sid)       # subagent lines inherit the parent id
            elif not rec.session_id:
                rec.session_id = str(sid)
            elif str(sid) != rec.session_id and not self.foreign_ok:
                rec.foreign_sessions[str(sid)] += 1
        if d.get("cwd") and not rec.project_path:
            rec.project_path = str(d["cwd"])
        _add_unique(rec.git_branches, d.get("gitBranch"))
        _add_unique(rec.versions, d.get("version"))
        if d.get("entrypoint"):
            _add_unique(rec.entrypoints, str(d["entrypoint"]))
        if d.get("permissionMode"):
            _add_unique(rec.permission_modes, str(d["permissionMode"]))
        if d.get("slug") and not rec.slug:
            rec.slug = str(d["slug"])
        if d.get("agentId") and not rec.agent_id:
            rec.agent_id = str(d["agentId"])

        handler = getattr(self, f"_t_{t.replace('-', '_')}", None)
        if handler:
            handler(d, ts)

    # ---------------------------------------------------------- line types ---
    def _turn(self, ts: str = "") -> Turn:
        if self.cur is None:
            self.cur = Turn(index=len(self.rec.turns) + 1, started_at=ts)
            self.rec.turns.append(self.cur)
        return self.cur

    def _new_turn(self, prompt: Message, ts: str, compact: bool = False) -> Turn:
        turn = Turn(index=len(self.rec.turns) + 1, prompt=prompt, started_at=ts, is_compact=compact)
        self.rec.turns.append(turn)
        self.cur = turn
        return turn

    def _t_user(self, d: dict, ts: str) -> None:
        msg = d.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else msg
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            self._tool_results(d, content, ts)
            self.last_user_ts = ts or self.last_user_ts
            return

        text = content_text(content)
        text, trunc = _cap(text, MAX_TEXT)
        m = Message(uuid=str(d.get("uuid") or ""), parent_uuid=str(d.get("parentUuid") or ""), role="user",
                    kind="prompt", text=text, timestamp=ts, last_timestamp=ts, truncated=trunc)
        if d.get("isSidechain"):
            m.flags.add("sidechain")

        stripped = text.lstrip()
        if stripped.startswith("[Request interrupted by user"):
            self._turn(ts).interrupted = True
            m.kind, m.role = "meta", "system"
            m.text = stripped
            self._turn(ts).messages.append(m)
            self.last_user_ts = ts or self.last_user_ts
            return

        if d.get("isCompactSummary"):
            m.kind = "summary"
            m.flags.add("compact_summary")
            self._new_turn(m, ts, compact=True)
        elif d.get("isMeta") or stripped.startswith(("<local-command-stdout>", "<local-command-stderr>",
                                                     "<local-command-caveat>", "<system-reminder>")):
            m.kind, m.role = "meta", "system"
            m.flags.add("meta")
            self._turn(ts).messages.append(m)
        else:
            cmd = _CMD_NAME.search(text)
            if cmd:
                args = _CMD_ARGS.search(text)
                m.kind = "command"
                m.text = (cmd.group(1) + (" " + args.group(1) if args and args.group(1) else "")).strip()
            self._new_turn(m, ts)
            if not self.rec.first_prompt and m.text.strip():
                self.rec.first_prompt = " ".join(m.text.split())[:300]
        self.last_user_ts = ts or self.last_user_ts

    def _tool_results(self, d: dict, content: list, ts: str) -> None:
        blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        tur = d.get("toolUseResult")
        for b in blocks:
            call = self.calls.get(str(b.get("tool_use_id") or ""))
            if call is None:
                continue
            out = content_text(b.get("content"))
            if not out and isinstance(tur, dict) and len(blocks) == 1:
                out = "\n".join(str(tur.get(k)) for k in ("stdout", "stderr", "result") if tur.get(k))
            if not out and isinstance(tur, str) and len(blocks) == 1:
                out = tur
            call.output, call.truncated = _cap(out, MAX_TOOL_OUTPUT)
            call.is_error = bool(b.get("is_error"))
            call.finished_at = ts
            call.wall_ms = ms_between(call.started_at, ts)
            if isinstance(tur, dict) and len(blocks) == 1:
                for key, mult in (("durationMs", 1), ("totalDurationMs", 1), ("durationSeconds", 1000)):
                    val = tur.get(key)
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        call.reported_ms = int(val * mult)
                        break
                aid = tur.get("agentId")
                if aid:
                    call.agent_id = str(aid)
                    _add_unique(self.rec.child_ids, f"agent-{aid}")
                if tur.get("interrupted"):
                    call.is_error = call.is_error or False

    def _t_assistant(self, d: dict, ts: str) -> None:
        msg = d.get("message") or {}
        if not isinstance(msg, dict):
            return
        mid = str(msg.get("id") or d.get("requestId") or d.get("uuid") or f"line{self.rec.type_counts['assistant']}")
        model = str(msg.get("model") or "")
        m = self.msgs.get(mid)
        turn = self._turn(ts)
        if m is None:
            m = Message(uuid=str(d.get("uuid") or ""), parent_uuid=str(d.get("parentUuid") or ""),
                        role="assistant", kind="text", timestamp=ts, model=model, message_id=mid,
                        request_id=str(d.get("requestId") or ""))
            self.msgs[mid] = m
            turn.messages.append(m)
            self.msg_turn[mid] = turn
            if d.get("isSidechain"):
                m.flags.add("sidechain")
            req = ApiRequest(message_id=mid, request_id=m.request_id, model=model, sent_at=self.last_user_ts,
                             first_at=ts, turn_index=turn.index)
            self.reqs[mid] = req
            self.req_turn[mid] = turn
            turn.requests.append(req)
        m.last_timestamp = ts or m.last_timestamp
        if model and not m.model:
            m.model = model

        blocks = msg.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        req = self.reqs[mid]
        req.last_at = ts or req.last_at
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            req.blocks += 1
            bt = b.get("type")
            if bt == "text":
                m.text = _join(m.text, str(b.get("text", "")))
            elif bt == "thinking":
                m.thinking = _join(m.thinking, str(b.get("thinking") or b.get("text") or ""))
            elif bt == "redacted_thinking":
                m.flags.add("redacted_thinking")
            elif bt in ("tool_use", "server_tool_use", "mcp_tool_use"):
                name = str(b.get("name") or "")
                summary, text = summarize_tool(name, b.get("input"))
                tc = ToolCall(call_id=str(b.get("id") or ""), name=name, summary=summary,
                              input_text=_cap(text, MAX_TOOL_OUTPUT)[0], started_at=ts)
                m.tool_calls.append(tc)
                if tc.call_id:
                    self.calls[tc.call_id] = tc
                self.rec.tool_counts[name or "(unnamed)"] += 1
                req.tool_uses += 1
            elif bt and bt.endswith("_tool_result"):            # web_search_tool_result etc.
                call = self.calls.get(str(b.get("tool_use_id") or ""))
                if call is not None:
                    call.output, call.truncated = _cap(content_text(b.get("content"))
                                                       or json.dumps(b.get("content"), default=str), MAX_TOOL_OUTPUT)
                    call.finished_at = ts
                    call.wall_ms = ms_between(call.started_at, ts)
        if m.text:
            m.text, m.truncated = _cap(m.text, MAX_TEXT)

        # usage: the same on every line of one response -> merge, never add
        u = TokenUsage.from_api(msg.get("usage"))
        cur = self.usage.setdefault(mid, TokenUsage())
        cur.merge_max(u)
        self.model_of[mid] = m.model or model
        self.day_of.setdefault(mid, (ts or "")[:10])
        if isinstance(msg.get("usage"), dict):
            speed = msg["usage"].get("speed")
            if speed:
                req.speed = str(speed)
        if msg.get("stop_reason"):
            m.stop_reason = req.stop_reason = str(msg["stop_reason"])
            if mid not in self.stop_seen:
                self.stop_seen.add(mid)
                self.rec.stop_reasons[m.stop_reason] += 1
        if (d.get("isApiErrorMessage") or model == "<synthetic>") and mid not in self.error_seen:
            self.error_seen.add(mid)
            m.flags.add("api_error")
            self.rec.api_errors += 1
            if d.get("error"):
                m.subtype = str(d["error"])[:200]

    def _t_system(self, d: dict, ts: str) -> None:
        st = str(d.get("subtype") or "")
        self.rec.type_counts[f"system/{st or '-'}"] += 1
        content = d.get("content")
        text = content_text(content) if not isinstance(content, str) else content
        if st == "turn_duration":
            dur = d.get("durationMs")
            if isinstance(dur, (int, float)):
                self._turn(ts).reported_ms = int(dur)
            return
        if st in ("stop_hook_summary", ""):
            return
        if st in ("compact_boundary", "microcompact_boundary"):
            meta = d.get("compactMetadata") or d.get("microcompactMetadata") or {}
            self.rec.compactions.append({"timestamp": ts, "trigger": meta.get("trigger", ""),
                                         "pre_tokens": meta.get("preTokens"), "subtype": st})
            text = text or f"Conversation compacted ({meta.get('trigger', 'auto')}, " \
                           f"{meta.get('preTokens', '?')} tokens before)"
        elif st == "api_error":
            self.rec.api_errors += 1
            err = d.get("error") or d.get("cause") or {}
            text = text or (json.dumps(err, default=str)[:300] if err else "API error")
        m = Message(uuid=str(d.get("uuid") or ""), role="system", kind="system", subtype=st,
                    text=_cap(text or st, 4000)[0], timestamp=ts, last_timestamp=ts)
        if st in ("api_error",):
            m.flags.add("api_error")
        self._turn(ts).messages.append(m)

    def _t_summary(self, d: dict, ts: str) -> None:
        s = str(d.get("summary") or "")
        if s:
            self.summaries.append(s)
            self.rec.summary = s

    def _t_custom_title(self, d: dict, ts: str) -> None:
        self.rec.custom_title = str(d.get("customTitle") or d.get("title") or self.rec.custom_title)

    def _t_ai_title(self, d: dict, ts: str) -> None:
        self.rec.ai_title = str(d.get("aiTitle") or d.get("title") or self.rec.ai_title)

    def _t_agent_name(self, d: dict, ts: str) -> None:
        if d.get("agentName"):
            self.rec.extra["agent_name"] = str(d["agentName"])

    def _t_last_prompt(self, d: dict, ts: str) -> None:
        if d.get("lastPrompt"):
            self.rec.extra["last_prompt"] = _cap(str(d["lastPrompt"]), 2000)[0]

    def _t_permission_mode(self, d: dict, ts: str) -> None:
        _add_unique(self.rec.permission_modes, d.get("permissionMode"))

    def _t_pr_link(self, d: dict, ts: str) -> None:
        _add_unique(self.rec.pr_links, str(d.get("prUrl") or d.get("url") or ""))

    def _t_queue_operation(self, d: dict, ts: str) -> None:
        if d.get("operation") == "enqueue" and d.get("content"):
            self.rec.queued_prompts.append(_cap(content_text(d["content"]), 2000)[0])

    def _t_file_history_snapshot(self, d: dict, ts: str) -> None:
        snap = d.get("snapshot") or {}
        tracked = snap.get("trackedFileBackups") if isinstance(snap, dict) else None
        if isinstance(tracked, dict):
            for p in tracked:
                if p not in self.snapshots:
                    self.snapshots.add(p)
                    self.rec.tracked_files.append(p)

    def _t_attachment(self, d: dict, ts: str) -> None:
        att = d.get("attachment")
        if isinstance(att, dict) and att.get("type"):
            self.rec.type_counts[f"attachment/{att['type']}"] += 1
            if att["type"] == "queued_command":          # a prompt typed while Claude was busy, injected on its next step
                text = _cap(content_text(att.get("prompt") or att.get("content") or ""), 2000)[0]
                if text and text not in self.rec.queued_prompts:
                    self.rec.queued_prompts.append(text)

    # -------------------------------------------------------------- finish ---
    def finish(self) -> SessionRecord:
        rec = self.rec
        # tokens (per API response, once) ---------------------------------------
        for mid, u in self.usage.items():
            model = self.model_of.get(mid, "")
            if model == "<synthetic>":
                continue
            rec.usage.add(u)
            rec.usage_by_model.setdefault(model or "(unknown)", TokenUsage()).add(u)
            day = self.day_of.get(mid, "")
            if day:
                rec.usage_by_day.setdefault(day, TokenUsage()).add(u)
            turn = self.msg_turn.get(mid)
            if turn is not None:
                turn.usage.add(u)
            if model and model != "<synthetic>":
                _add_unique(rec.models, model)
                if turn is not None:
                    turn.models.add(model)
            req = self.reqs.get(mid)
            if req is not None:
                req.input_tokens, req.output_tokens, req.cache_read_tokens = \
                    u.input_tokens, u.output_tokens, u.cache_read_tokens
        # timings -----------------------------------------------------------------
        for req in self.reqs.values():
            req.in_flight_ms = ms_between(req.sent_at, req.first_at)
            req.stream_ms = ms_between(req.first_at, req.last_at)
            req.total_ms = ms_between(req.sent_at, req.last_at)
            rec.requests.append(req)
        for turn in rec.turns:
            stamps = [turn.prompt.timestamp] if turn.prompt and turn.prompt.timestamp else []
            for m in turn.messages:
                stamps += [s for s in (m.timestamp, m.last_timestamp) if s]
                for tc in m.tool_calls:
                    if tc.finished_at:
                        stamps.append(tc.finished_at)
            stamps = [s for s in stamps if parse_ts(s) is not None]
            if stamps:
                stamps.sort(key=lambda s: parse_ts(s))
                turn.started_at = turn.started_at or stamps[0]
                turn.ended_at = stamps[-1]
                turn.wall_ms = ms_between(stamps[0], stamps[-1])
        if not rec.title:
            rec.title = rec.custom_title or rec.ai_title or rec.summary
        if rec.foreign_sessions:
            others = ", ".join(f"{k[:8]}×{v}" for k, v in rec.foreign_sessions.most_common(3))
            rec.warnings.append(f"{sum(rec.foreign_sessions.values())} lines carry a different session id ({others}) - "
                                "resumed/forked history or Claude Code's known cross-session write bug; "
                                "lines kept and attributed to this file.")
        if rec.corrupt_lines:
            rec.warnings.append(f"{rec.corrupt_lines} unreadable line(s) skipped.")
        rec.origin = rec.origin or (_origin(rec.entrypoints[0]) if rec.entrypoints else "")
        return rec


def _join(a: str, b: str) -> str:
    return b if not a else (a + "\n\n" + b if b else a)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def parse_transcript(tf: TranscriptFile | Path, *, on_bytes: Optional[Callable[[int], None]] = None,
                     cancel: Optional[Callable[[], bool]] = None) -> SessionRecord:
    """Parse one transcript file into a :class:`SessionRecord`.

    Legacy inline sidechains (``isSidechain`` + ``agentId`` inside a main file) are returned as
    ``rec.extra['inline_children']`` (already finished ``SessionRecord`` objects).
    """
    if not isinstance(tf, TranscriptFile):
        tf = TranscriptFile(path=Path(tf), session_id=Path(tf).stem)
    path = Path(tf.path)
    is_sub = tf.kind in ("subagent", "cowork-subagent")
    rec = SessionRecord(
        session_id="" if not is_sub else tf.session_id, kind=tf.kind, project_dir=tf.project_dir,
        source_path=str(path), size_bytes=tf.size or _size(path), agent_id=tf.agent_id,
        parent_session_id=tf.parent_session_id, origin=tf.origin_hint)
    file_sid = tf.session_id if not is_sub else ""
    builder = _Builder(rec, foreign_ok=is_sub, route_sidechains=not is_sub)
    if file_sid:
        rec.session_id = file_sid

    done = 0
    since = 0
    with open(paths.long_path(path), "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            done += len(raw)
            since += len(raw)
            if since >= PROGRESS_EVERY:
                since = 0
                if on_bytes:
                    on_bytes(done)
                if cancel and cancel():
                    raise RecoveryCancelled()
            raw = raw.strip()
            if not raw:
                continue
            rec.line_count += 1
            try:
                d = json.loads(raw)
            except ValueError:
                rec.corrupt_lines += 1
                continue
            if isinstance(d, dict):
                builder.feed(d)
            else:
                rec.corrupt_lines += 1
    if on_bytes:
        on_bytes(done)
    if not rec.session_id:
        rec.session_id = tf.session_id or path.stem
    if not rec.project_path and tf.project_dir:
        rec.project_path = ""
    out = builder.finish()
    if is_sub and not out.parent_session_id:
        pass
    if tf.meta_path:
        _read_agent_meta(out, tf.meta_path)
    children = [b.finish() for b in builder.children.values()]
    for c in children:
        c.project_path = c.project_path or out.project_path
        c.origin = c.origin or out.origin
        _add_unique(out.child_ids, c.session_id)
    if children:
        out.extra["inline_children"] = children
    return out


def _read_agent_meta(rec: SessionRecord, meta_path: Path) -> None:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return
    if isinstance(meta, dict):
        rec.agent_type = str(meta.get("agentType") or meta.get("subagent_type") or meta.get("type") or "")
        rec.agent_description = str(meta.get("description") or meta.get("name") or "")
        rec.extra["meta"] = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}


def _size(path: Path) -> int:
    try:
        return os.stat(paths.long_path(path)).st_size
    except OSError:
        return 0


def link_subagents(sessions: list[SessionRecord]) -> None:
    """Fill ``parent_session_id`` / ``child_ids`` using sessionId, Task results and agent ids."""
    by_sid = {s.session_id: s for s in sessions if s.kind in ("main", "cowork")}
    by_agent = {s.agent_id: s for s in sessions if s.is_subagent and s.agent_id}
    for parent in sessions:
        for turn in parent.turns:
            for m in turn.messages:
                for tc in m.tool_calls:
                    if tc.agent_id and tc.agent_id in by_agent:
                        child = by_agent[tc.agent_id]
                        child.parent_session_id = child.parent_session_id or parent.session_id
                        child.parent_call_id = child.parent_call_id or tc.call_id
                        child.agent_description = child.agent_description or tc.summary
                        _add_unique(parent.child_ids, child.session_id)
    for s in sessions:
        if s.is_subagent and s.parent_session_id in by_sid:
            _add_unique(by_sid[s.parent_session_id].child_ids, s.session_id)


def real_project_path(rec: SessionRecord) -> str:
    return rec.project_path or paths.decode_project_dir(rec.project_dir)
