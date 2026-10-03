"""Markdown renderer: combined report + per-session files.

Everything user-visible goes through the :class:`Redactor`; the renderer itself never decides what is
secret. Time-in-flight tables are rendered only when ``options.include_flight`` is set (the GUI sets it
only when the experimental gate is unlocked).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import paths
from .models import Message, RecoveryOptions, SessionRecord, ToolCall, Turn
from .redaction import Redactor
from .transcripts import parse_ts

EMBED_LIMIT_BYTES = 200 * 1024 * 1024       # above this, the combined report links to per-session files
TOOL_OUT_REPORT = 6000
TOOL_IN_REPORT = 4000
MSG_LABELS = {"user": "**You**", "assistant": "**Claude**", "system": "_system_"}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def clip(text: Any, limit: int = 70) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def n(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def dt(ts: Any, seconds: bool = False) -> str:
    t = parse_ts(ts)
    if t is None:
        return "-"
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def dur(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms} ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f} s"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m} m {sec:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} m"


def human_bytes(v: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(v) < 1024 or unit == "TB":
            return f"{int(v)} B" if unit == "B" else f"{v:,.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def table(header: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        cells = [str(c).replace("|", "\\|").replace("\n", " ") for c in row]
        out.append("| " + " | ".join(cells) + " |")
    return out


def fence(text: str, lang: str = "text") -> list[str]:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return [f"{ticks}{lang}", text.rstrip("\n"), ticks]


def slugify(text: str, limit: int = 100) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return (s[:limit].strip("-")) or "session"


def session_filename(index: int, rec: SessionRecord, red: Redactor) -> str:
    base = f"{index:02d}_{slugify(red.apply(rec.display_title))}"
    return base[:116] + ".md"


def tokens_line(u) -> str:
    return (f"in {n(u.input_tokens)} · out {n(u.output_tokens)} · cache-read {n(u.cache_read_tokens)} · "
            f"cache-write {n(u.cache_write_tokens)}")


def project_key(rec: SessionRecord) -> str:
    return rec.project_path or paths.decode_project_dir(rec.project_dir) or "(unknown)"


def order_sessions(sessions: Iterable[SessionRecord]) -> list[SessionRecord]:
    """Newest first - shared by the combined report and the per-session file numbering."""
    return sorted(sessions, key=lambda s: (s.updated_at or s.created_at or ""), reverse=True)


def counted(sessions: Iterable[SessionRecord]) -> list[SessionRecord]:
    """Sessions that count toward totals (orphaned/superseded files are earlier copies of a session)."""
    return [s for s in sessions if s.kind not in ("orphaned", "superseded")]


def disp_id(rec: SessionRecord, red: Redactor) -> str:
    return "↳" + red.session_id(rec.agent_id or rec.session_id) if rec.is_subagent else red.session_id(rec.session_id)


def badges(rec: SessionRecord) -> str:
    out = []
    if rec.child_ids:
        out.append(f"🧩{len(rec.child_ids)}")
    if rec.compactions:
        out.append(f"🗜{len(rec.compactions)}")
    if rec.api_errors:
        out.append(f"⚠{rec.api_errors}")
    if rec.kind in ("orphaned", "superseded"):
        out.append("🗃")
    if rec.is_subagent:
        out.append("↳")
    return " ".join(out) or "-"


# --------------------------------------------------------------------------- #
# per-session rendering
# --------------------------------------------------------------------------- #

def render_tool(tc: ToolCall, red: Redactor, opts: RecoveryOptions, L: list[str], indent: str = "") -> None:
    title = f"🔧 {tc.name or 'tool'} — {clip(red.apply(tc.summary), 110)}" if tc.summary else f"🔧 {tc.name or 'tool'}"
    flag = " ❌" if tc.is_error else ""
    L.append(f"{indent}<details><summary>{_esc(title)}{flag}</summary>")
    L.append("")
    if tc.input_text:
        body = red.apply(tc.input_text)[:TOOL_IN_REPORT]
        L += [indent + ln for ln in fence(body)]
        L.append("")
    if tc.output:
        L.append(f"{indent}_Output{' (error)' if tc.is_error else ''}:_")
        body = red.apply(tc.output)
        cut = len(body) > TOOL_OUT_REPORT
        L += [indent + ln for ln in fence(body[:TOOL_OUT_REPORT])]
        if cut or tc.truncated:
            L.append(f"{indent}_… output truncated_")
        L.append("")
    if opts.include_flight and (tc.wall_ms is not None or tc.reported_ms is not None):
        L.append(f"{indent}_wall {dur(tc.wall_ms)} · reported {dur(tc.reported_ms)}_")
        L.append("")
    L.append(f"{indent}</details>")
    L.append("")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote(text: str) -> list[str]:
    return ["> " + ln if ln.strip() else ">" for ln in text.splitlines()] or [">"]


def render_turns(rec: SessionRecord, red: Redactor, opts: RecoveryOptions, L: list[str],
                 lookup: dict[str, SessionRecord], depth: int = 0) -> None:
    hl = "#" * min(6, 4 + depth)
    for turn in rec.turns:
        when = dt(turn.started_at, seconds=True)
        label = "Compacted context" if turn.is_compact else f"Turn {turn.index}"
        extra = ""
        if opts.include_tokens and turn.usage.total:
            extra += f" · {n(turn.usage.output_tokens)} out"
        if turn.interrupted:
            extra += " · ⏹ interrupted"
        L.append(f"{hl} {label} — {when}{extra}")
        L.append("")
        if turn.prompt is not None:
            text = red.apply(turn.prompt.text)
            who = "**Claude Code (summary of earlier conversation)**" if turn.is_compact else "**You**"
            if turn.prompt.kind == "command":
                who = "**You** (command)"
            L.append(who + ":")
            L.append("")
            L += _quote(text)
            L.append("")
        for m in turn.messages:
            _render_message(m, red, opts, L, lookup, depth)
        if opts.include_flight and turn.reported_ms is not None:
            L.append(f"_turn duration (app-reported): {dur(turn.reported_ms)}_")
            L.append("")


def _render_message(m: Message, red: Redactor, opts: RecoveryOptions, L: list[str],
                    lookup: dict[str, SessionRecord], depth: int) -> None:
    if m.kind in ("meta",):
        if m.text.strip().startswith("[Request interrupted"):
            L.append("_⏹ " + clip(m.text, 120) + "_")
            L.append("")
        return                                              # other meta lines are noise
    if m.kind == "system":
        icon = "⚠" if "api_error" in m.flags else "⚙"
        L.append(f"_{icon} {clip(red.apply(m.text), 240)}_")
        L.append("")
        return
    if m.kind in ("text",) or m.role == "assistant":
        head = MSG_LABELS["assistant"]
        if m.model and opts.include_tokens:
            head += f" _({m.model.replace('<', '').replace('>', '')})_"
        if "api_error" in m.flags:
            head += " ⚠ API error"
        if m.text or m.thinking or "redacted_thinking" in m.flags:
            L.append(head + ":")
            L.append("")
        if m.thinking and opts.include_thinking:
            L.append("<details><summary>💭 thinking</summary>")
            L.append("")
            L += _quote(red.apply(m.thinking))
            L.append("")
            L.append("</details>")
            L.append("")
        elif "redacted_thinking" in m.flags and opts.include_thinking:
            L.append("_💭 (encrypted thinking block - not recoverable)_")
            L.append("")
        if m.text:
            L.append(red.apply(m.text))
            L.append("")
    if opts.include_tools:
        for tc in m.tool_calls:
            render_tool(tc, red, opts, L)
            if tc.agent_id:
                child = lookup.get(f"agent-{tc.agent_id}")
                if child is not None and opts.include_subagents:
                    if opts.flatten_subagents:
                        L.append(f"> ↳ **Subagent** `{red.session_id(tc.agent_id)}` — {clip(red.apply(child.display_title), 90)}")
                        L.append("")
                        sub: list[str] = []
                        render_turns(child, red, opts, sub, lookup, depth + 1)
                        L += ["> " + s if s else ">" for s in sub]
                        L.append("")
                    else:
                        L.append(f"> ↳ Subagent `{red.session_id(tc.agent_id)}` — "
                                 f"{clip(red.apply(child.display_title), 90)} ({len(child.turns)} turns, see its section)")
                        L.append("")
    elif m.tool_calls:
        L.append(f"_🔧 {len(m.tool_calls)} tool call(s): " + ", ".join(sorted({t.name for t in m.tool_calls})) + "_")
        L.append("")


def render_session_header(rec: SessionRecord, red: Redactor, opts: RecoveryOptions, index: int) -> list[str]:
    rows = [
        ["Session", f"`{disp_id(rec, red)}`" + (f" (agent `{red.session_id(rec.agent_id)}`)" if rec.agent_id and not rec.is_subagent else "")],
        ["Kind", rec.kind + (f" of `{red.session_id(rec.parent_session_id)}`" if rec.parent_session_id else "")],
        ["Project", f"`{red.path(project_key(rec))}`"],
        ["Origin", (rec.origin or "-") + (f" · Claude Code {', '.join(rec.versions[:3])}" if rec.versions else "")],
        ["Started → updated", f"{dt(rec.created_at)} → {dt(rec.updated_at)}"],
        ["Turns · prompts · tool calls", f"{len(rec.turns)} · {rec.prompt_count} · {rec.tool_count}"],
    ]
    if rec.models:
        rows.append(["Models", ", ".join(rec.models)])
    if rec.git_branches:
        rows.append(["Git branch", ", ".join(f"`{red.apply(b)}`" for b in rec.git_branches[:4])])
    if opts.include_tokens:
        rows.append(["Tokens (per API response, once)", tokens_line(rec.usage)])
    if rec.agent_type or rec.agent_description:
        rows.append(["Subagent", clip(red.apply(f"{rec.agent_type} {rec.agent_description}"), 120)])
    if rec.compactions:
        rows.append(["Compactions", ", ".join(f"{c.get('trigger') or '?'} @ {n(c.get('pre_tokens'))} tok" for c in rec.compactions)])
    if rec.api_errors:
        rows.append(["API errors", str(rec.api_errors)])
    if rec.pr_links:
        rows.append(["Pull requests", ", ".join(red.url(u) for u in rec.pr_links)])
    if rec.permission_modes:
        rows.append(["Permission modes", ", ".join(rec.permission_modes)])
    rows.append(["Source", f"`{red.path(rec.source_path)}` ({human_bytes(rec.size_bytes)}, {n(rec.line_count)} lines)"])
    L = table(["Field", "Value"], rows) + [""]
    if rec.summary:
        L += ["> **Summary:** " + clip(red.apply(rec.summary), 400), ""]
    for w in rec.warnings:
        L.append(f"> ⚠ {red.apply(w)}")
    if rec.warnings:
        L.append("")
    return L


def render_session(rec: SessionRecord, red: Redactor, opts: RecoveryOptions, index: int,
                   lookup: dict[str, SessionRecord], *, heading: str = "###", anchor: str = "") -> str:
    L: list[str] = []
    a = f'<a id="{anchor}"></a>' if anchor else ""
    L.append(f"{heading} {a}{index}. {red.apply(rec.display_title)}")
    L.append("")
    L += render_session_header(rec, red, opts, index)
    if opts.include_transcripts:
        render_turns(rec, red, opts, L, lookup)
    if rec.queued_prompts:
        L += ["**Queued prompts** (typed while Claude was busy):", ""]
        L += [f"- {clip(red.apply(q), 200)}" for q in rec.queued_prompts[:20]] + [""]
    if rec.tracked_files and opts.include_app_state:
        L += [f"<details><summary>📁 {len(rec.tracked_files)} file(s) tracked for checkpoint restore</summary>", ""]
        L += [f"- `{red.path(p)}`" for p in rec.tracked_files[:60]]
        L += ["", "</details>", ""]
    return "\n".join(L)


def render_session_file(rec: SessionRecord, red: Redactor, opts: RecoveryOptions, index: int,
                        lookup: dict[str, SessionRecord], report_name: str, version: str) -> str:
    L = [f"# {red.apply(rec.display_title)}", "",
         f"_Generated {stamp()} by Cantata {version} · redaction `{red.level}` · "
         f"[← back to the full report](../{report_name}#sessions)_", ""]
    body = render_session(rec, red, opts, index, lookup, heading="##")
    body = "\n".join(body.splitlines()[2:])          # drop duplicate heading
    return "\n".join(L) + "\n" + body.rstrip() + "\n"


# --------------------------------------------------------------------------- #
# combined report
# --------------------------------------------------------------------------- #

def build_report(bundle: dict, red: Redactor, opts: RecoveryOptions, version: str = "",
                 session_files: Optional[dict[int, str]] = None, embed: bool = True) -> str:
    sessions: list[SessionRecord] = bundle["sessions"]
    stats = bundle.get("stats", {})
    vinfo = bundle["version_info"]
    lookup = {s.session_id: s for s in sessions}
    mains = [s for s in sessions if not s.is_subagent]
    subs = [s for s in sessions if s.is_subagent]
    L: list[str] = []

    L += ["# Claude Local Recovery Report", ""]
    L += [f"**Generated**: {stamp()}", f"**Tool**: Cantata {version}",
          f"**Source**: `{red.path(str(bundle['source']))}`", f"**Redaction level**: `{red.level}`", ""]
    L += ["> Reconstructed from what Claude Code (and optionally Claude Desktop) leave on this machine. "
          "Claude.ai *Chat* conversations live on Anthropic's servers — see the **Chat & Cowork** report for "
          "that footprint. Claude Code **deletes transcripts after `cleanupPeriodDays` (default 30)**, so this "
          "report can only contain what has not yet aged out.", ""]
    L += ["## Contents", ""]
    toc = ["Overview", "Detected Versions", "Retention & Coverage", "Sessions", "Projects"]
    if opts.include_tokens:
        toc.append("Usage")
    if opts.include_app_state:
        toc.append("Application State")
    if bundle.get("desktop") is not None and opts.include_desktop:
        toc.append("Claude Desktop")
    if opts.include_flight and bundle.get("flight_md"):
        toc.append("Time in Flight")
    if opts.include_logs:
        toc.append("Logs")
    toc += ["Warnings", "Session Transcripts"]
    L += [f"- [{t}](#{slugify(t)})" for t in toc] + ["", "---", ""]

    # -- overview ----------------------------------------------------------------- #
    total_tokens = sum(s.usage.total for s in counted(sessions))
    L += ['<a id="overview"></a>', "## Overview", ""]
    rows = [
        ["Sessions recovered", f"{len(mains)} ({sum(1 for s in mains if s.kind in ('orphaned', 'superseded'))} orphaned/superseded)"],
        ["Subagent transcripts", str(len(subs))],
        ["Projects", str(len({project_key(s) for s in sessions}))],
        ["Turns", n(sum(len(s.turns) for s in sessions))],
        ["Tool calls", n(sum(s.tool_count for s in sessions))],
        ["Transcript lines", f"{n(sum(s.line_count for s in sessions))} ({n(sum(s.corrupt_lines for s in sessions))} unreadable)"],
        ["Transcript data", human_bytes(sum(s.size_bytes for s in sessions))],
    ]
    if opts.include_tokens:
        rows.append(["Tokens (all four kinds, de-duplicated, excl. orphaned copies)", n(total_tokens)])
    lo = min((s.created_at for s in sessions if s.created_at), default="")
    hi = max((s.updated_at for s in sessions if s.updated_at), default="")
    rows.append(["Date range", f"{dt(lo)} → {dt(hi)}" if lo else "-"])
    rows.append(["Prompts in history.jsonl", n(bundle.get("history", {}).get("total", 0))])
    L += table(["Metric", "Value"], rows) + ["", "---", ""]

    # -- versions ------------------------------------------------------------------ #
    L += ['<a id="detected-versions"></a>', "## Detected Versions", ""]
    ver_counts: dict[str, list[SessionRecord]] = defaultdict(list)
    for s in sessions:
        for v in s.versions:
            ver_counts[v].append(s)
    if ver_counts:
        vrows = []
        for v in vinfo.claude_code_versions:
            ss = ver_counts.get(v, [])
            vrows.append([f"`{v}`", str(len(ss)), dt(min((s.created_at for s in ss if s.created_at), default="")),
                          dt(max((s.updated_at for s in ss if s.updated_at), default=""))])
        L += table(["Claude Code", "Sessions", "First seen", "Last seen"], vrows) + [""]
    else:
        L += ["_(no version information in the transcripts)_", ""]
    meta = []
    if vinfo.entrypoints:
        meta.append("**Entrypoints**: " + ", ".join(vinfo.entrypoints))
    if vinfo.install_method:
        meta.append(f"**Install method**: {vinfo.install_method}")
    if vinfo.desktop_version:
        meta.append(f"**Claude Desktop**: {vinfo.desktop_version}")
    L += [" · ".join(meta), ""] if meta else []
    L += ["---", ""]

    # -- retention ------------------------------------------------------------------ #
    L += ['<a id="retention-coverage"></a>', "## Retention & Coverage", ""]
    eff = stats.get("retention_effective", 30)
    L += [f"- **Retention window**: {eff} days" + ("" if stats.get("retention_days") else " (default `cleanupPeriodDays`)"),
          f"- **Oldest transcript on disk**: {stats.get('oldest_age_days', '-')} days old",
          f"- **Expiring within 7 days**: {stats.get('expiring_soon', 0)}",
          f"- **Transcripts on disk**: {stats.get('active_transcripts', len(mains))} active, "
          f"{stats.get('orphaned_transcripts', 0)} orphaned/superseded, {stats.get('subagents', len(subs))} subagent"]
    sc = bundle.get("stats_cache")
    if sc:
        L.append(f"- **stats-cache.json** remembers {n(sc.get('totalSessions'))} sessions / {n(sc.get('totalMessages'))} messages "
                 f"since {dt(sc.get('firstSessionDate'))}")
    L.append("")
    gone = bundle.get("index_only", [])
    if gone:
        L += [f"### Index-only sessions ({len(gone)}) — remembered, transcript gone", "",
              "_Known from `sessions-index.json` / `history.jsonl` but the transcript was swept or deleted. "
              "Prompts typed in them survive in `history.jsonl`._", ""]
        L += table(["Session", "Project", "First prompt", "Msgs", "Last seen", "Source"],
                   [[f"`{red.session_id(g['session_id'])}`", f"`{red.path(g.get('project', ''))}`",
                     clip(red.apply(g.get("first_prompt") or g.get("summary")), 60), n(g.get("messages")),
                     dt(g.get("modified") or g.get("created")), g.get("source", "")] for g in gone]) + [""]
    L += ["---", ""]

    # -- sessions index ------------------------------------------------------------------ #
    L += ['<a id="sessions"></a>', "## Sessions", ""]
    L += ["_🧩 subagents · 🗜 compactions · ⚠ API errors · 🗃 orphaned/superseded copy · ↳ subagent_", ""]
    ordered = order_sessions(sessions)
    numbering = {id(s): i for i, s in enumerate(ordered, 1)}
    rows = []
    for s in ordered:
        i = numbering[id(s)]
        title = f"[{clip(red.apply(s.display_title), 62)}](#s{i})" if embed else clip(red.apply(s.display_title), 62)
        if session_files and id(s) in session_files:
            title = f"[{clip(red.apply(s.display_title), 62)}]({session_files[id(s)]})"
        rows.append([i, f"`{disp_id(s, red)}`", title, clip(red.path(paths.project_label(project_key(s))), 22),
                     s.origin or "-", dt(s.created_at), len(s.turns), s.tool_count,
                     n(s.usage.total) if opts.include_tokens else "-", badges(s)])
    L += table(["#", "Session", "Title", "Project", "Origin", "Started", "Turns", "Tools", "Tokens", "Flags"], rows) + ["", "---", ""]

    # -- projects -------------------------------------------------------------------------- #
    L += ['<a id="projects"></a>', "## Projects", ""]
    by_proj: dict[str, list[SessionRecord]] = defaultdict(list)
    for s in sessions:
        by_proj[project_key(s)].append(s)
    prow = []
    for pk, ss in sorted(by_proj.items(), key=lambda kv: max((s.updated_at for s in kv[1] if s.updated_at), default=""), reverse=True):
        prow.append([f"`{red.path(pk)}`", sum(1 for s in ss if not s.is_subagent), sum(1 for s in ss if s.is_subagent),
                     sum(len(s.turns) for s in ss), n(sum(s.usage.total for s in counted(ss))) if opts.include_tokens else "-",
                     dt(max((s.updated_at for s in ss if s.updated_at), default=""))])
    L += table(["Project", "Sessions", "Subagents", "Turns", "Tokens", "Last activity"], prow) + ["", "---", ""]

    # -- usage ------------------------------------------------------------------------------- #
    if opts.include_tokens:
        L += ['<a id="usage"></a>', "## Usage", "",
              "_Tokens are summed **once per API response** (Claude Code repeats the same usage on every content-block line)._", ""]
        by_model: dict[str, Any] = {}
        resp_count: Counter = Counter()
        for s in counted(sessions):
            for model, u in s.usage_by_model.items():
                acc = by_model.setdefault(model, u.copy().__class__())
                acc.add(u)
            for r in s.requests:
                if r.model and r.model != "<synthetic>":
                    resp_count[r.model] += 1
        mrows = [[f"`{m}`", n(resp_count.get(m, 0)), n(u.input_tokens), n(u.output_tokens), n(u.cache_read_tokens),
                  n(u.cache_write_tokens), f"{u.cache_hit_ratio * 100:.0f}%" if u.cache_hit_ratio is not None else "-"]
                 for m, u in sorted(by_model.items(), key=lambda kv: kv[1].total, reverse=True)]
        L += ["### By model", ""] + table(["Model", "Responses", "Input", "Output", "Cache read", "Cache write", "Cache hit"], mrows) + [""]
        by_day: dict[str, Any] = {}
        for s in counted(sessions):
            for day, u in s.usage_by_day.items():
                acc = by_day.setdefault(day, u.copy().__class__())
                acc.add(u)
        drows = [[day, n(u.input_tokens), n(u.output_tokens), n(u.cache_read_tokens), n(u.cache_write_tokens)]
                 for day, u in sorted(by_day.items(), reverse=True)[:60]]
        L += ["### By day", ""] + table(["Day (UTC)", "Input", "Output", "Cache read", "Cache write"], drows) + [""]
        if sc and sc.get("modelUsage"):
            L += ["### stats-cache.json (what `/usage` shows)", ""]
            srows = [[f"`{m}`", n(v.get("inputTokens")), n(v.get("outputTokens")), n(v.get("cacheReadInputTokens")),
                      n(v.get("cacheCreationInputTokens")), f"${float(v.get('costUSD') or 0):.2f}" if v.get("costUSD") else "-"]
                     for m, v in sc["modelUsage"].items() if isinstance(v, dict)]
            L += table(["Model", "Input", "Output", "Cache read", "Cache write", "Cost"], srows) + [
                "", "_The cache is rebuilt from surviving transcripts, so its totals can shrink after a sweep._", ""]
        L += ["---", ""]

    # -- application state ---------------------------------------------------------------------- #
    if opts.include_app_state:
        L += _app_state(bundle, red, opts)
    # -- desktop ---------------------------------------------------------------------------------- #
    if bundle.get("desktop") is not None and opts.include_desktop:
        L += _desktop_section(bundle["desktop"], red, opts)
    if opts.include_flight and bundle.get("flight_md"):
        L += ['<a id="time-in-flight"></a>', bundle["flight_md"], "", "---", ""]
    # -- logs ---------------------------------------------------------------------------------------- #
    if opts.include_logs:
        L += ['<a id="logs"></a>', "## Logs", ""]
        mcp = bundle.get("mcp_logs") or []
        if mcp:
            L += ["### MCP / diagnostics cache (`claude-cli-nodejs`)", ""]
            L += table(["Project slug", "Kind", "Files", "Size"], [[f"`{clip(m['project'], 50)}`", m["kind"], m["files"], human_bytes(m["bytes"])] for m in mcp]) + [""]
        tail = bundle.get("debug_tail") or []
        if tail:
            L += [f"### Debug log tail (last {len(tail)} lines)", ""] + fence("\n".join(tail[-opts.log_limit:])) + [""]
        if not mcp and not tail:
            L += ["_(no debug logs or MCP logs found — debug logs exist only for sessions run with `--debug`)_", ""]
        L += ["---", ""]

    # -- warnings ------------------------------------------------------------------------------------ #
    L += ['<a id="warnings"></a>', "## Warnings", ""]
    warns = list(bundle.get("warnings", []))
    for s in sessions:
        for w in s.warnings:
            warns.append(f"{red.session_id(s.session_id)}: {w}")
    unknown = Counter()
    for s in sessions:
        for t, c in s.type_counts.items():
            if "/" not in t and t not in _KNOWN_TYPES:
                unknown[t] += c
    if unknown:
        warns.append("Unknown transcript line types (newer Claude Code?): " + ", ".join(f"`{t}`×{c}" for t, c in unknown.most_common(8)))
    L += [f"- {red.apply(w)}" for w in warns[:150]] or ["_(none)_"]
    L += ["", "---", ""]

    # -- transcripts -------------------------------------------------------------------------------------- #
    L += ['<a id="session-transcripts"></a>', "## Session Transcripts", ""]
    if not opts.include_transcripts:
        L += ["_(transcripts excluded by options)_", ""]
    elif not embed:
        L += ["_Transcripts are not embedded here because the archive is large; each session has its own file "
              "(linked in the **Sessions** table)._", ""]
    else:
        for s in ordered:
            if s.is_subagent and not opts.include_subagents:
                continue
            i = numbering[id(s)]
            L.append(render_session(s, red, opts, i, lookup, heading="###", anchor=f"s{i}"))
            L += ["---", ""]
    L += [f"_Report generated locally by Cantata {version} — no data left this machine._", ""]
    return "\n".join(L).rstrip() + "\n"


_KNOWN_TYPES = {"user", "assistant", "system", "summary", "queue-operation", "last-prompt", "progress", "attachment",
                "file-history-snapshot", "permission-mode", "custom-title", "ai-title", "agent-name", "pr-link", "result",
                "unknown"}


# --------------------------------------------------------------------------- #
def _app_state(b: dict, red: Redactor, opts: RecoveryOptions) -> list[str]:
    L = ['<a id="application-state"></a>', "## Application State", ""]
    # settings
    L += ["### Settings", ""]
    rows = []
    for s in b.get("settings", []):
        if not s.get("exists"):
            rows.append([s["scope"], f"`{red.path(s['path'])}`", "absent", "-", "-", "-"])
            continue
        p = s["permissions"]
        rows.append([s["scope"], f"`{red.path(s['path'])}`", s.get("model") or "-", f"{len(p['allow'])}/{len(p['deny'])}/{len(p['ask'])}",
                     len(s["hooks"]), ", ".join(s["env_keys"]) or "-"])
    L += table(["Scope", "File", "Model", "Allow/Deny/Ask", "Hooks", "Env keys (values hidden)"], rows) + [""]
    for s in b.get("settings", []):
        if s.get("exists") and s["hooks"]:
            L += [f"**Hooks in {s['scope']}**", ""]
            L += [f"- `{h['event']}` {('`' + h['matcher'] + '` ') if h['matcher'] else ''}→ `{clip(red.apply(h['command']), 100)}`" for h in s["hooks"]] + [""]
        if s.get("exists") and (s["permissions"]["allow"] or s["permissions"]["deny"]):
            L += [f"**Permission rules in {s['scope']}**: allow " + ", ".join(f"`{clip(red.apply(x), 50)}`" for x in s["permissions"]["allow"][:12])
                  + (" · deny " + ", ".join(f"`{clip(red.apply(x), 50)}`" for x in s["permissions"]["deny"][:12]) if s["permissions"]["deny"] else ""), ""]
    # global json
    g = b.get("global") or {}
    L += ["### Global config (`~/.claude.json`)", ""]
    if g.get("exists"):
        acct = red.scrub(g.get("account", {}))
        L += [f"- **Startups**: {n(g.get('num_startups'))} · **install**: {g.get('install_method') or '-'} · **theme**: {g.get('theme') or '-'}",
              f"- **Account**: " + (", ".join(f"{k}={v}" for k, v in acct.items()) or "-"),
              f"- **Feature flags cached**: {g.get('feature_flags_cached', 0)} · **tips seen**: {g.get('tips_seen', 0)}", ""]
        flight_cols = opts.include_flight
        prow = []
        for pp, info in g.get("projects", {}).items():
            row = [f"`{red.path(pp)}`", "✓" if info.get("trusted") else "-",
                   f"${info['last_cost_usd']:.2f}" if isinstance(info.get("last_cost_usd"), (int, float)) else "-"]
            if flight_cols:
                row += [dur(info.get("last_duration_ms")), dur(info.get("last_api_duration_ms"))]
            row.append(f"+{n(info.get('lines_added'))}/-{n(info.get('lines_removed'))}" if info.get("lines_added") is not None else "-")
            prow.append(row)
        if prow:
            L += ["**Per-project stats** (app-measured, last session only)", ""]
            L += table(["Project", "Trusted", "Last cost"] + (["Last duration", "Last API time"] if flight_cols else []) + ["Lines"], prow) + [""]
    else:
        L += ["_(not found)_", ""]
    cred = b.get("credentials") or {}
    L += ["### Credentials (state only — tokens are never read out)", ""]
    if cred.get("has_access_token") or cred.get("file"):
        L += [f"- `.credentials.json` present · access token: {'yes' if cred.get('has_access_token') else 'no'} · refresh token: "
              f"{'yes' if cred.get('has_refresh_token') else 'no'} · subscription: {cred.get('subscription') or '-'} · "
              f"tier: {cred.get('rate_limit_tier') or '-'} · expires {cred.get('expires_at') or '-'}", ""]
    else:
        L += ["_No `.credentials.json` (macOS keeps OAuth tokens in the Keychain; API-key users have none)._", ""]
    # MCP
    mcp = b.get("mcp") or []
    if mcp:
        L += ["### MCP servers", ""]
        L += table(["Name", "Scope", "Type", "Command / URL", "Env / header keys (values hidden)"],
                   [[f"`{m['name']}`", m["scope"], m["type"], f"`{clip(red.apply(m['command'] or m['url']), 60)}`",
                     ", ".join(m["env_keys"] + m["header_keys"]) or "-"] for m in mcp]) + [""]
    # memory
    mem = b.get("memory") or {}
    if mem.get("claude_md") or mem.get("auto_memory"):
        L += ["### Memory", ""]
        for cm in mem.get("claude_md", []):
            L += [f"<details><summary>📝 CLAUDE.md — {cm['scope']} ({human_bytes(cm['bytes'])})</summary>", ""]
            L += fence(red.apply(cm["text"])[:8000], "markdown") + ["", "</details>", ""]
        for am in mem.get("auto_memory", []):
            L += [f"<details><summary>🧠 Auto memory — `{clip(am['project_dir'], 60)}` ({len(am['files'])} files)</summary>", ""]
            for f in am["files"][:20]:
                L += [f"**{f['name']}**", ""] + fence(red.apply(f["text"])[:4000], "markdown") + [""]
            L += ["</details>", ""]
    # extensions & plugins
    ext = b.get("extensions") or {}
    if any(ext.values()) or b.get("plugins"):
        L += ["### Extensions & plugins", ""]
        L += [f"- **{k}** ({len(v)}): " + ", ".join(f"`{x}`" for x in v[:15]) for k, v in ext.items() if v]
        if b.get("plugins"):
            L.append("- **plugins** (" + str(len(b["plugins"])) + "): " + ", ".join(f"`{p['name']}` {p['version']}" for p in b["plugins"][:15]))
        L.append("")
    # plans / tasks / todos
    if b.get("plans") or b.get("todos") or b.get("tasks"):
        L += ["### Plans, tasks & todos", ""]
        for pl in b.get("plans", [])[:15]:
            L += [f"<details><summary>🗺 plan `{pl['name']}` — {clip(red.apply(pl['title']), 70)}</summary>", ""]
            L += fence(red.apply(pl["text"])[:5000], "markdown") + ["", "</details>", ""]
        for t in b.get("todos", [])[:15]:
            L += [f"- todos `{red.session_id(t['session'])}`: " + "; ".join(f"[{i['status']}] {clip(red.apply(i['content']), 50)}" for i in t["items"][:8])]
        for t in b.get("tasks", [])[:15]:
            L += [f"- task list `{t['list']}`: " + "; ".join(f"[{i['status']}] {clip(red.apply(i['subject']), 50)}" for i in t["items"][:8])]
        L.append("")
    # history
    h = b.get("history") or {}
    if h.get("exists"):
        L += ["### Prompt history (`history.jsonl`)", "",
              f"- {n(h['total'])} prompts across {len(h['by_project'])} project(s) and {len(h['session_ids'])} session(s) "
              f"({dt((h['first_ms'] or 0) / 1000 if h['first_ms'] else '')} → {dt((h['last_ms'] or 0) / 1000 if h['last_ms'] else '')}); "
              f"{h['pasted']} with pasted content", ""]
        L += table(["When", "Project", "Prompt"], [[dt((e["timestamp"] or 0) / 1000) if e["timestamp"] else "-", f"`{clip(red.path(e['project']), 30)}`",
                                                   clip(red.apply(e["display"]), 90)] for e in h["entries"][-25:]]) + [""]
    # other stores
    misc = b.get("misc") or {}
    if misc or b.get("ide") or b.get("file_history"):
        L += ["### Other stores", ""]
        rows = [[f"`{k}/`", v["files"], human_bytes(v["bytes"])] for k, v in sorted(misc.items())]
        L += table(["Directory", "Files", "Size"], rows) + [""]
        if b.get("ide"):
            L += ["**IDE lock files** (each holds an auth token — presence only):", ""]
            L += [f"- `{i['file']}` {i['ide']} pid {i['pid']} · workspaces: " + ", ".join(f"`{red.path(w)}`" for w in i["workspaces"]) for i in b["ide"]] + [""]
        if b.get("file_history"):
            fh = b["file_history"]
            L += [f"**File-history checkpoints**: {len(fh)} sessions, {sum(x['files'] for x in fh)} snapshot files, "
                  f"{human_bytes(sum(x['bytes'] for x in fh))}", ""]
        tel = b.get("telemetry") or {}
        if tel.get("files"):
            L += [f"**Failed telemetry uploads**: {tel['files']} files — " + ", ".join(f"`{k}`×{v}" for k, v in tel["events"].items()), ""]
    L += ["---", ""]
    return L


def _desktop_section(d, red: Redactor, opts: RecoveryOptions) -> list[str]:
    L = ['<a id="claude-desktop"></a>', "## Claude Desktop", ""]
    if not d.found:
        return L + ["_No Claude Desktop data directory found._", "", "---", ""]
    st = d.stats()
    L += [f"- **Data directories**: " + "; ".join(f"`{red.path(v['path'])}` ({v['flavour']})" for v in d.variants),
          f"- **Cowork / chat sessions**: {st['cowork_sessions']} ({st['cowork_transcripts']} transcripts, {st['audit_logs']} audit logs) · "
          f"**Code-tab wrappers**: {st['code_tab_sessions']} · **memory files**: {st['memory_files']}", ""]
    if d.sessions:
        rows = []
        for s in d.sessions[:80]:
            a = s.audit or {}
            rows.append([s.kind, f"`{red.session_id(s.local_id.replace('local_', ''))}`", clip(red.apply(s.title or s.initial_message), 50),
                         s.model or "-", dt(s.last_activity or s.created_at), len(s.transcripts) if s.kind != "code-tab" else "→ ~/.claude",
                         f"${a['total_cost_usd']:.2f}" if a.get("results") else "-", "✓" if s.archived else ""])
        L += table(["Kind", "Session", "Title", "Model", "Last activity", "Transcripts", "Cost", "Archived"], rows) + [""]
    cw = [s for s in d.sessions if s.audit and s.audit.get("results")]
    if cw:
        L += ["### Cowork audit logs (`audit.jsonl` result lines)", ""]
        fl = opts.include_flight
        L += table(["Session", "Turns"] + (["Wall", "API"] if fl else []) + ["Cost", "Rate-limit events", "HMAC chain fields"],
                   [[f"`{red.session_id(s.local_id.replace('local_', ''))}`", s.audit["num_turns"]]
                    + ([dur(s.audit["duration_ms"]), dur(s.audit["duration_api_ms"])] if fl else [])
                    + [f"${s.audit['total_cost_usd']:.2f}", s.audit["rate_limit_events"],
                       "yes (key is keychain-encrypted; not verified)" if s.audit["hmac_fields"] else "no"] for s in cw]) + [""]
    for c in d.configs:
        if c.get("mcp_servers"):
            L += [f"**MCP servers in `claude_desktop_config.json`**: " + ", ".join(f"`{m['name']}`" for m in c["mcp_servers"])
                  + " (env values hidden)", ""]
    if d.memory:
        L += ["**Cowork memory notes**: " + "; ".join(f"{m['scope']} ({len(m['files'])} files)" for m in d.memory), ""]
    if d.logs:
        L += ["**Logs**: " + ", ".join(f"`{l['name']}` ({human_bytes(l['bytes'])})" for l in d.logs[:14]), ""]
    if d.vm_entries:
        L += ["**VM / binaries**: " + ", ".join(f"`{v['name']}` {v['entries']} entries" for v in d.vm_entries), ""]
    L += ["---", ""]
    return L
