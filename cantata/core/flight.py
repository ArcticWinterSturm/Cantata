"""Time in flight - latency analytics  (GATED: see ``gate.py``).

Claude Code's transcripts carry **no** time-to-first-token field (Codex's rollouts do). What *is* on disk:

* per-line timestamps  -> request-sent -> first content block -> last block  (derived; first-block time is an
  **upper bound** on TTFT because a block is logged when it *completes*, and hooks / permission prompts
  add wall time before the request is sent),
* ``system/turn_duration``, ``toolUseResult.durationMs`` / ``totalDurationMs`` (app-measured),
* Cowork ``audit.jsonl`` ``result`` lines (``duration_ms`` / ``duration_api_ms``),
* ``~/.claude.json`` per-project ``lastDuration`` / ``lastAPIDuration``.

Exact TTFT exists only in OpenTelemetry ``claude_code.llm_request`` spans; :func:`load_otel_ttft` merges a
Live-mode capture (``flight-*.jsonl``) into the report when available.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import SessionRecord
from .redaction import Redactor
from .report import clip, dur, dt, n, table

CAVEATS = [
    "**In flight (derived)** = request-sent line → first completed content block. It is an *upper bound* on true "
    "time-to-first-token: Claude Code writes a block when it finishes streaming, and permission prompts / hooks "
    "before the request inflate it.",
    "**Stream** = first block → last block of one API response; **tokens/s** = output tokens ÷ that span "
    "(only meaningful for multi-block responses).",
    "**Tool wall** = tool_use line → tool_result line, so it includes any time spent waiting for *your* permission.",
    "Exact TTFT requires OpenTelemetry capture (`claude_code.llm_request.ttft_ms`) — use Live mode's receiver.",
]


def pct(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(values: Iterable[Optional[float]]) -> dict:
    # Transcript fields are parsed from hand-editable JSONL, so a stray string ("", "n/a")
    # must not take down the whole page. Non-numeric values are dropped, and NaN/inf are
    # excluded too -- one NaN otherwise poisons p50/p90/mean for every model.
    v: list[float] = []
    for x in values:
        if x is None or isinstance(x, bool):
            continue
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            v.append(f)
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": min(v), "p50": pct(v, 0.5), "p90": pct(v, 0.9), "p99": pct(v, 0.99),
            "max": max(v), "mean": sum(v) / len(v)}


@dataclass
class FlightReport:
    requests: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    by_model: dict = field(default_factory=dict)
    by_tool: dict = field(default_factory=dict)
    cowork: list[dict] = field(default_factory=list)
    project_stats: list[dict] = field(default_factory=list)
    otel: dict = field(default_factory=dict)
    sessions: int = 0

    def headline(self) -> dict:
        allr = [r["in_flight_ms"] for r in self.requests]
        return {"requests": len(self.requests), "tool_calls": len(self.tools), "turns": len(self.turns),
                "in_flight": summarize(allr), "sessions": self.sessions}


def compute_flight(sessions: list[SessionRecord], *, desktop=None, global_info: Optional[dict] = None,
                   otel_dir: Optional[Path] = None) -> FlightReport:
    fr = FlightReport()
    seen_paths = set()
    for s in sessions:
        if s.kind in ("orphaned", "superseded"):
            continue
        fr.sessions += 1
        label = s.agent_id and f"↳{s.agent_id}" or s.session_id
        for r in s.requests:
            if not r.model or r.model == "<synthetic>":
                continue
            tps = None
            if r.stream_ms and r.stream_ms > 0 and r.output_tokens and r.blocks > 1:
                tps = r.output_tokens / (r.stream_ms / 1000)
            fr.requests.append({"session": label, "model": r.model, "sent_at": r.sent_at, "first_at": r.first_at,
                                "in_flight_ms": r.in_flight_ms, "stream_ms": r.stream_ms, "total_ms": r.total_ms,
                                "output_tokens": r.output_tokens, "input_tokens": r.input_tokens,
                                "cache_read_tokens": r.cache_read_tokens, "tps": tps, "speed": r.speed,
                                "tool_uses": r.tool_uses, "stop_reason": r.stop_reason, "turn": r.turn_index})
        for t in s.turns:
            tool_ms = 0
            for m in t.messages:
                for tc in m.tool_calls:
                    fr.tools.append({"session": label, "name": tc.name, "wall_ms": tc.wall_ms, "reported_ms": tc.reported_ms,
                                     "is_error": tc.is_error, "summary": tc.summary})
                    if tc.wall_ms and not tc.agent_id:
                        tool_ms += tc.wall_ms
            model_ms = sum(r.total_ms or 0 for r in t.requests if r.model != "<synthetic>")
            wall = t.wall_ms
            other = max(0, (wall or 0) - model_ms - tool_ms) if wall is not None else None
            if wall is not None or t.reported_ms is not None:
                fr.turns.append({"session": label, "turn": t.index, "prompt": (t.prompt.text if t.prompt else "")[:80],
                                 "wall_ms": wall, "reported_ms": t.reported_ms, "model_ms": model_ms, "tool_ms": tool_ms,
                                 "other_ms": other, "requests": len(t.requests)})
    bym: dict[str, list[dict]] = defaultdict(list)
    for r in fr.requests:
        bym[r["model"]].append(r)
    for model, rows in bym.items():
        fr.by_model[model] = {"n": len(rows), "in_flight": summarize(r["in_flight_ms"] for r in rows),
                              "stream": summarize(r["stream_ms"] for r in rows), "total": summarize(r["total_ms"] for r in rows),
                              "tps": summarize(r["tps"] for r in rows)}
    byt: dict[str, list[dict]] = defaultdict(list)
    for t in fr.tools:
        byt[t["name"] or "(unnamed)"].append(t)
    for name, rows in byt.items():
        fr.by_tool[name] = {"n": len(rows), "wall": summarize(r["wall_ms"] for r in rows),
                            "reported": summarize(r["reported_ms"] for r in rows), "errors": sum(1 for r in rows if r["is_error"])}
    if desktop is not None:
        for ds in getattr(desktop, "sessions", []):
            a = ds.audit or {}
            if a.get("results"):
                fr.cowork.append({"session": ds.local_id, "title": ds.title, "turns": a["num_turns"], "duration_ms": a["duration_ms"],
                                  "duration_api_ms": a["duration_api_ms"], "cost": a["total_cost_usd"]})
    for pp, info in (global_info or {}).get("projects", {}).items():
        if info.get("last_duration_ms") or info.get("last_api_duration_ms"):
            fr.project_stats.append({"project": pp, "duration_ms": info.get("last_duration_ms"),
                                     "api_ms": info.get("last_api_duration_ms")})
    if otel_dir:
        fr.otel = load_otel_ttft(Path(otel_dir))
    return fr


def load_otel_ttft(directory: Path) -> dict:
    """Exact TTFT from Live-mode captures (``flight-*.jsonl`` rows with ``event == 'llm_request'``)."""
    rows: list[dict] = []
    for f in sorted(Path(directory).glob("flight-*.jsonl")):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("event") == "llm_request" and d.get("ttft_ms") is not None:
                        rows.append(d)
        except OSError:
            continue
    if not rows:
        return {}
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[str(r.get("model") or "?")].append(r)
    return {"n": len(rows), "files": str(directory), "ttft": summarize(r["ttft_ms"] for r in rows),
            "duration": summarize(r.get("duration_ms") for r in rows),
            "by_model": {m: {"n": len(v), "ttft": summarize(r["ttft_ms"] for r in v),
                             "first_content": summarize(r.get("first_content_ms") for r in v)} for m, v in by_model.items()}}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _f(v: Optional[float]) -> str:
    return dur(int(v)) if v is not None else "-"


def render_markdown(fr: FlightReport, red: Redactor, top: int = 15) -> str:
    L = ["## Time in Flight", ""]
    L += ["> " + c for c in CAVEATS] + [""]
    h = fr.headline()
    L += [f"- **API responses analysed**: {n(h['requests'])} across {h['sessions']} sessions · **tool calls**: {n(h['tool_calls'])} · "
          f"**turns with timing**: {n(h['turns'])}", ""]
    if fr.by_model:
        L += ["### Per model", ""]
        rows = []
        for model, m in sorted(fr.by_model.items(), key=lambda kv: -kv[1]["n"]):
            rows.append([f"`{model}`", m["n"], _f(m["in_flight"].get("p50")), _f(m["in_flight"].get("p90")),
                         _f(m["in_flight"].get("p99")), _f(m["stream"].get("p50")), _f(m["total"].get("p50")),
                         f"{m['tps']['p50']:.0f}" if m["tps"].get("p50") else "-"])
        L += table(["Model", "Responses", "In flight p50", "p90", "p99", "Stream p50", "Total p50", "Out tok/s p50"], rows) + [""]
    if fr.otel:
        o = fr.otel
        L += ["### Exact TTFT (OpenTelemetry capture)", "",
              f"{n(o['n'])} `llm_request` spans from `{red.path(o['files'])}` — TTFT p50 {_f(o['ttft'].get('p50'))}, "
              f"p90 {_f(o['ttft'].get('p90'))}, p99 {_f(o['ttft'].get('p99'))}", ""]
        L += table(["Model", "Spans", "TTFT p50", "p90", "First content p50"],
                   [[f"`{m}`", v["n"], _f(v["ttft"].get("p50")), _f(v["ttft"].get("p90")), _f(v["first_content"].get("p50"))]
                    for m, v in o["by_model"].items()]) + [""]
    slow = sorted((r for r in fr.requests if r["in_flight_ms"] is not None), key=lambda r: -r["in_flight_ms"])[:top]
    if slow:
        L += [f"### Slowest {len(slow)} requests (by in-flight time)", ""]
        L += table(["Session", "Model", "Sent", "In flight", "Stream", "Out tok", "Tool uses", "Speed"],
                   [[f"`{red.session_id(r['session'])}`", f"`{clip(r['model'], 24)}`", dt(r["sent_at"], True), _f(r["in_flight_ms"]),
                     _f(r["stream_ms"]), n(r["output_tokens"]), r["tool_uses"], r["speed"] or "-"] for r in slow]) + [""]
    if fr.by_tool:
        L += ["### Tools", ""]
        L += table(["Tool", "Calls", "Wall p50", "Wall p90", "Wall max", "Reported p50", "Errors"],
                   [[f"`{name}`", t["n"], _f(t["wall"].get("p50")), _f(t["wall"].get("p90")), _f(t["wall"].get("max")),
                     _f(t["reported"].get("p50")), t["errors"]]
                    for name, t in sorted(fr.by_tool.items(), key=lambda kv: -(kv[1]["wall"].get("max") or 0))[:25]]) + [""]
    longest = sorted((t for t in fr.turns if (t["wall_ms"] or 0) > 0), key=lambda t: -t["wall_ms"])[:top]
    if longest:
        L += [f"### Longest {len(longest)} turns — where the time went", "",
              "_model = Σ request totals · tools = Σ tool wall (excl. subagent spawns) · other = hooks, permission waits, user think-time, overhead_", ""]
        L += table(["Session", "Turn", "Prompt", "Wall", "Model", "Tools", "Other", "App-reported", "Requests"],
                   [[f"`{red.session_id(t['session'])}`", t["turn"], clip(red.apply(t["prompt"]), 40), _f(t["wall_ms"]), _f(t["model_ms"]),
                     _f(t["tool_ms"]), _f(t["other_ms"]), _f(t["reported_ms"]), t["requests"]] for t in longest]) + [""]
    if fr.cowork:
        L += ["### Cowork sessions (audit.jsonl `result` lines)", ""]
        L += table(["Session", "Title", "Turns", "Wall", "API time", "API share", "Cost"],
                   [[f"`{red.session_id(c['session'].replace('local_', ''))}`", clip(red.apply(c["title"]), 40), c["turns"], _f(c["duration_ms"]),
                     _f(c["duration_api_ms"]), f"{100 * c['duration_api_ms'] / c['duration_ms']:.0f}%" if c["duration_ms"] else "-",
                     f"${c['cost']:.2f}"] for c in fr.cowork]) + [""]
    if fr.project_stats:
        L += ["### Last-session totals from `~/.claude.json`", ""]
        L += table(["Project", "Last duration", "Last API time", "API share"],
                   [[f"`{red.path(p['project'])}`", _f(p["duration_ms"]), _f(p["api_ms"]),
                     f"{100 * p['api_ms'] / p['duration_ms']:.0f}%" if p["duration_ms"] and p["api_ms"] else "-"] for p in fr.project_stats]) + [""]
    return "\n".join(L).rstrip()


def format_text(fr: FlightReport, red: Optional[Redactor] = None, top: int = 10) -> str:
    red = red or Redactor("standard")
    L = ["# Claude time-in-flight (derived from transcripts)", ""]
    h = fr.headline()
    ifl = h["in_flight"]
    L.append(f"  responses: {h['requests']}  tool calls: {h['tool_calls']}  turns: {h['turns']}  sessions: {h['sessions']}")
    if ifl.get("n"):
        L.append(f"  in-flight  p50 {_f(ifl['p50'])}  p90 {_f(ifl['p90'])}  p99 {_f(ifl['p99'])}  max {_f(ifl['max'])}")
    L.append("")
    L.append("## Per model")
    for m, v in sorted(fr.by_model.items(), key=lambda kv: -kv[1]["n"]):
        L.append(f"  {m:<30} n={v['n']:<5} in-flight p50={_f(v['in_flight'].get('p50')):>9} p90={_f(v['in_flight'].get('p90')):>9}  "
                 f"stream p50={_f(v['stream'].get('p50')):>9}")
    L.append("")
    L.append("## Slowest tools")
    for name, t in sorted(fr.by_tool.items(), key=lambda kv: -(kv[1]['wall'].get('max') or 0))[:top]:
        L.append(f"  {name:<24} n={t['n']:<5} p50={_f(t['wall'].get('p50')):>9} max={_f(t['wall'].get('max')):>9}")
    if fr.otel:
        L += ["", "## Exact TTFT (OpenTelemetry)",
              f"  n={fr.otel['n']} p50={_f(fr.otel['ttft'].get('p50'))} p90={_f(fr.otel['ttft'].get('p90'))} p99={_f(fr.otel['ttft'].get('p99'))}"]
    L += [""] + ["  * " + c.replace("**", "").replace("`", "") for c in CAVEATS]
    return "\n".join(L)


def to_json(fr: FlightReport) -> str:
    return json.dumps({"headline": fr.headline(), "by_model": fr.by_model, "by_tool": fr.by_tool, "turns": fr.turns[:200],
                       "requests": fr.requests[:2000], "cowork": fr.cowork, "project_stats": fr.project_stats, "otel": fr.otel},
                      indent=2, default=str)
