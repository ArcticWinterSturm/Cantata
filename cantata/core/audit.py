"""Usage audit - per-session, per-response and per-model analytics from the transcripts.

The CLI ``audit`` command. Latency / time-in-flight is deliberately *not* here (see ``flight.py``, gated).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from . import recovery, report, scanner
from .models import RecoveryOptions, SessionRecord, TokenUsage
from .redaction import Redactor


def _human(nbytes: float) -> str:
    return report.human_bytes(nbytes)


class UsageAudit:
    def __init__(self, source: Path, include_desktop: bool = False, redaction: str = "standard"):
        self.source = Path(source)
        self.include_desktop = include_desktop
        self.red = Redactor(redaction)

    def run(self) -> dict:
        opts = RecoveryOptions(include_desktop=self.include_desktop, include_app_state=False, include_logs=False)
        bundle = recovery.collect_bundle(self.source, opts, lambda *a: None, lambda *a: None, lambda: False)
        sessions: list[SessionRecord] = report.counted(bundle["sessions"])
        self.red = Redactor(self.red.level, home_dirs=recovery.derive_homes(sessions, self.source))
        stats = bundle["stats"]

        summaries = []
        for s in sorted(sessions, key=lambda x: x.created_at or ""):
            u = s.usage
            summaries.append({
                "session_id": s.session_id, "kind": s.kind, "title": s.display_title, "project": report.project_key(s),
                "models": s.models, "turns": len(s.turns), "prompts": s.prompt_count, "responses": len([r for r in s.requests if r.model != "<synthetic>"]),
                "tool_calls": s.tool_count, "input": u.input_tokens, "output": u.output_tokens, "cache_read": u.cache_read_tokens,
                "cache_write": u.cache_write_tokens, "cache_hit": u.cache_hit_ratio, "bytes": s.size_bytes, "lines": s.line_count,
                "compactions": len(s.compactions), "api_errors": s.api_errors, "stop_reasons": dict(s.stop_reasons),
                "subagents": len(s.child_ids), "parent": s.parent_session_id, "agent_id": s.agent_id, "created": s.created_at, "updated": s.updated_at,
                "versions": s.versions, "origin": s.origin,
            })
        ledger = []
        for s in sessions:
            for r in s.requests:
                if r.model and r.model != "<synthetic>":
                    ledger.append({"session_id": s.session_id, "model": r.model, "at": r.first_at, "input": r.input_tokens,
                                   "output": r.output_tokens, "cache_read": r.cache_read_tokens, "stop": r.stop_reason,
                                   "tool_uses": r.tool_uses, "speed": r.speed})
        tools: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0})
        for s in sessions:
            for t in s.turns:
                for m in t.messages:
                    for tc in m.tool_calls:
                        d = tools[tc.name or "(unnamed)"]
                        d["calls"] += 1
                        d["errors"] += 1 if tc.is_error else 0
        by_model: dict[str, TokenUsage] = {}
        for s in sessions:
            for model, u in s.usage_by_model.items():
                by_model.setdefault(model, TokenUsage()).add(u)
        stops = Counter()
        for s in sessions:
            stops.update(s.stop_reasons)
        sc = bundle.get("stats_cache") or {}
        cross = []
        for model, u in by_model.items():
            ref = (sc.get("modelUsage") or {}).get(model) or {}
            cross.append({"model": model, "transcripts_in": u.input_tokens, "transcripts_out": u.output_tokens,
                          "cache_in": ref.get("inputTokens"), "cache_out": ref.get("outputTokens"),
                          "transcripts_cache_read": u.cache_read_tokens, "cache_cache_read": ref.get("cacheReadInputTokens")})
        return {"source": str(self.source), "stats": {k: stats[k] for k in ("active_transcripts", "orphaned_transcripts", "subagents",
                                                                           "projects", "retention_effective", "oldest_age_days",
                                                                           "expiring_soon", "history_entries", "sessions_reported")},
                "totals": {"sessions": len(sessions), "bytes": sum(s.size_bytes for s in sessions),
                           "input": sum(s.usage.input_tokens for s in sessions), "output": sum(s.usage.output_tokens for s in sessions),
                           "cache_read": sum(s.usage.cache_read_tokens for s in sessions),
                           "cache_write": sum(s.usage.cache_write_tokens for s in sessions)},
                "sessions": summaries, "ledger": ledger, "tools": dict(tools), "by_model": {m: vars(u) for m, u in by_model.items()},
                "stop_reasons": dict(stops), "cross_check": cross, "index_only": bundle.get("index_only", []),
                "warnings": bundle.get("warnings", [])}

    def format_text(self, data: dict, ledger_rows: int = 6) -> str:
        red = self.red
        L = ["# Claude Local-State Audit", f"  source: {red.path(data['source'])}",
             f"  transcripts: {data['totals']['sessions']} sessions ({_human(data['totals']['bytes'])}) · "
             f"{data['stats']['subagents']} subagent files · {data['stats']['orphaned_transcripts']} orphaned copies · "
             f"{data['stats']['projects']} projects", ""]
        st = data["stats"]
        L += ["## Retention", f"  window {st['retention_effective']} d · oldest transcript {st['oldest_age_days']} d old · "
              f"{st['expiring_soon']} expire within a week · stats-cache remembers {st['sessions_reported']} sessions · "
              f"{len(data['index_only'])} index-only (transcript gone)", ""]
        L.append("## 1. Sessions: tokens (per API response, once)")
        for s in data["sessions"]:
            hit = f"{s['cache_hit'] * 100:3.0f}%" if s["cache_hit"] is not None else " n/a"
            tag = "↳" if s["kind"].endswith("subagent") else " "
            L.append(f" {tag}{red.session_id(s['agent_id'] or s['session_id']):<9} {report.clip(red.apply(s['title']), 26):<26} {','.join(m.replace('claude-', '')[:14] for m in s['models'])[:16]:<16} "
                     f"turns={s['turns']:<3} resp={s['responses']:<3} in={s['input']:<7} out={s['output']:<7} "
                     f"cache-r={s['cache_read']:<9} cache-w={s['cache_write']:<8} hit={hit}"
                     + (f" 🗜{s['compactions']}" if s["compactions"] else "") + (f" ⚠{s['api_errors']}" if s["api_errors"] else ""))
        L += ["", "## 2. Per-response ledger (first responses of the largest sessions)"]
        by_sess: dict[str, list] = defaultdict(list)
        for r in data["ledger"]:
            by_sess[r["session_id"]].append(r)
        for sid, rows in sorted(by_sess.items(), key=lambda kv: -len(kv[1]))[:5]:
            L.append(f"  {red.session_id(sid)}  ({len(rows)} response{'s' if len(rows) != 1 else ''})")
            for i, r in enumerate(rows[:ledger_rows], 1):
                L.append(f"    #{i:<3} {r['model'].replace('claude-', '')[:22]:<22} in={r['input']:<7} out={r['output']:<6} cache-r={r['cache_read']:<8} "
                         f"stop={r['stop'] or '-':<9} tools={r['tool_uses']} {r['speed'] or ''}")
        L += ["", "## 3. By model"]
        for m, u in sorted(data["by_model"].items(), key=lambda kv: -(kv[1]["input_tokens"] + kv[1]["output_tokens"])):
            L.append(f"  {m:<32} in={u['input_tokens']:<9} out={u['output_tokens']:<9} cache-r={u['cache_read_tokens']:<11} cache-w={u['cache_write_tokens']}")
        L += ["", "## 4. Stop reasons", "  " + (", ".join(f"{k}={v}" for k, v in sorted(data["stop_reasons"].items(), key=lambda kv: -kv[1])) or "-"), "",
              "## 5. Tools"]
        for name, t in sorted(data["tools"].items(), key=lambda kv: -kv[1]["calls"])[:15]:
            L.append(f"  {name:<24} calls={t['calls']:<5} errors={t['errors']}")
        L += ["", "## 6. Cross-check vs stats-cache.json (what /usage shows)"]
        for c in data["cross_check"]:
            L.append(f"  {c['model']:<32} transcripts in/out = {c['transcripts_in']}/{c['transcripts_out']}  "
                     f"stats-cache = {c['cache_in'] if c['cache_in'] is not None else '-'}/{c['cache_out'] if c['cache_out'] is not None else '-'}")
        L.append("  (differences are expected: the cache is rebuilt from surviving transcripts and counts differently)")
        for g in data["index_only"][:10]:
            L.append(f"  index-only: {red.session_id(g['session_id'])}  {report.clip(red.apply(g.get('first_prompt') or g.get('summary')), 50)}")
        return "\n".join(L) + "\n"


def run_audit(source, *, output_json: bool = False, include_desktop: bool = False, redaction: str = "standard") -> str:
    audit = UsageAudit(Path(source), include_desktop, redaction)
    data = audit.run()
    if output_json:
        for s in data["sessions"]:
            s["title"] = audit.red.apply(s["title"])
            s["project"] = audit.red.path(s["project"])
        data["source"] = audit.red.path(data["source"])
        return json.dumps(data, indent=2, default=str)
    return audit.format_text(data)
