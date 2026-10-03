"""Readers for Claude Code's *other* on-disk state (everything that is not a transcript).

Every reader returns plain JSON-safe data, is tolerant of missing / malformed files, never follows
secrets (IDE lock ``authToken`` and similar are reported as *present*, not read out), and never writes.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from . import paths
from .redaction import Redactor


def _load_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _iter_jsonl(path: Path, limit: Optional[int] = None):
    n = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
                    n += 1
                    if limit and n >= limit:
                        return
    except OSError:
        return


def _dir_stats(path: Path) -> dict:
    files = size = 0
    newest = 0.0
    if path.is_dir():
        for root, _d, names in os.walk(path):
            for n in names:
                try:
                    st = os.stat(os.path.join(root, n))
                except OSError:
                    continue
                files += 1
                size += st.st_size
                newest = max(newest, st.st_mtime)
    return {"files": files, "bytes": size, "newest": newest}


# --------------------------------------------------------------------------- #
# history / stats / indexes
# --------------------------------------------------------------------------- #

def read_history(config_dir: Path, limit: Optional[int] = None) -> dict:
    p = Path(config_dir) / "history.jsonl"
    out: dict = {"exists": p.is_file(), "total": 0, "first_ms": None, "last_ms": None, "entries": [],
                 "by_project": Counter(), "session_ids": set(), "pasted": 0}
    if not out["exists"]:
        return out
    for obj in _iter_jsonl(p):
        out["total"] += 1
        ts = obj.get("timestamp")
        if isinstance(ts, (int, float)):
            out["first_ms"] = ts if out["first_ms"] is None else min(out["first_ms"], ts)
            out["last_ms"] = ts if out["last_ms"] is None else max(out["last_ms"], ts)
        if obj.get("project"):
            out["by_project"][str(obj["project"])] += 1
        if obj.get("sessionId"):
            out["session_ids"].add(str(obj["sessionId"]))
        if obj.get("pastedContents"):
            out["pasted"] += 1
        out["entries"].append({"display": str(obj.get("display") or ""), "timestamp": ts,
                               "project": str(obj.get("project") or ""), "sessionId": str(obj.get("sessionId") or ""),
                               "pasted": len(obj.get("pastedContents") or {})})
    if limit:
        out["entries"] = out["entries"][-limit:]
    return out


def read_stats_cache(config_dir: Path) -> Optional[dict]:
    data = _load_json(Path(config_dir) / "stats-cache.json")
    return data if isinstance(data, dict) else None


def read_sessions_indexes(projects_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return out
    for d in sorted(projects_dir.iterdir()):
        data = _load_json(d / "sessions-index.json") if d.is_dir() else None
        if isinstance(data, dict):
            out[d.name] = {"original_path": data.get("originalPath", ""),
                           "entries": [e for e in data.get("entries", []) if isinstance(e, dict)]}
    return out


def index_only_sessions(history: dict, indexes: dict[str, dict], stats_cache: Optional[dict],
                        transcript_ids: Iterable[str]) -> list[dict]:
    """Sessions that Claude remembers (history / index) but whose transcript is gone."""
    have = set(transcript_ids)
    found: dict[str, dict] = {}
    for proj_dir, idx in indexes.items():
        for e in idx["entries"]:
            sid = str(e.get("sessionId") or "")
            if sid and sid not in have:
                found[sid] = {"session_id": sid, "source": "sessions-index.json", "project": e.get("projectPath") or idx["original_path"],
                              "first_prompt": e.get("firstPrompt", ""), "summary": e.get("summary", ""),
                              "messages": e.get("messageCount"), "created": e.get("created", ""), "modified": e.get("modified", ""),
                              "branch": e.get("gitBranch", "")}
    by_sid: dict[str, list[dict]] = {}
    for h in history.get("entries", []):
        if h["sessionId"]:
            by_sid.setdefault(h["sessionId"], []).append(h)
    for sid, items in by_sid.items():
        if sid in have:
            continue
        rec = found.setdefault(sid, {"session_id": sid, "source": "history.jsonl", "project": items[0]["project"],
                                     "first_prompt": "", "summary": "", "messages": None, "created": "", "modified": "", "branch": ""})
        rec["first_prompt"] = rec["first_prompt"] or items[0]["display"]
        rec["prompts_in_history"] = len(items)
        rec["source"] = rec["source"] if rec["source"] == "sessions-index.json" else "history.jsonl"
        if rec["source"] == "sessions-index.json":
            rec["source"] = "sessions-index.json + history.jsonl"
    return sorted(found.values(), key=lambda r: r.get("modified") or r.get("created") or "")


# --------------------------------------------------------------------------- #
# tasks / plans / memory / file-history
# --------------------------------------------------------------------------- #

def read_todos(config_dir: Path) -> list[dict]:
    out = []
    d = Path(config_dir) / "todos"
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            data = _load_json(f)
            if isinstance(data, list) and data:
                out.append({"file": f.name, "session": f.name.split("-agent-")[0],
                            "items": [{"content": str(t.get("content", "")), "status": str(t.get("status", ""))}
                                      for t in data if isinstance(t, dict)]})
    return out


def read_tasks(config_dir: Path) -> list[dict]:
    out = []
    d = Path(config_dir) / "tasks"
    if d.is_dir():
        for lst in sorted(p for p in d.iterdir() if p.is_dir()):
            items = []
            for f in sorted(lst.glob("*.json")):
                data = _load_json(f)
                if isinstance(data, dict):
                    items.append({"id": str(data.get("id", f.stem)), "subject": str(data.get("subject") or data.get("title") or ""),
                                  "status": str(data.get("status", ""))})
            out.append({"list": lst.name, "items": items})
    return out


def read_plans(config_dir: Path) -> list[dict]:
    out = []
    d = Path(config_dir) / "plans"
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                st = f.stat()
            except OSError:
                continue
            head = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
            out.append({"name": f.name, "bytes": st.st_size, "mtime": st.st_mtime, "title": head[:120], "text": text})
    return out


def read_memory(config_dir: Path, project_paths: Iterable[str] = ()) -> dict:
    config_dir = Path(config_dir)
    out: dict = {"claude_md": [], "auto_memory": [], "agent_memory": []}
    cands = [("user", config_dir / "CLAUDE.md")]
    for pp in project_paths:
        if pp and os.path.isabs(pp):
            for rel in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
                cands.append((f"project:{paths.project_label(pp)}", Path(pp) / rel))
    for scope, p in cands:
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                out["claude_md"].append({"scope": scope, "path": str(p), "bytes": len(text.encode("utf-8")), "text": text})
        except OSError:
            continue
    proj = config_dir / "projects"
    if proj.is_dir():
        for d in sorted(proj.iterdir()):
            mem = d / "memory"
            if mem.is_dir():
                files = []
                for f in sorted(mem.rglob("*.md")):
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    files.append({"name": str(f.relative_to(mem)), "bytes": len(text.encode("utf-8")), "text": text})
                out["auto_memory"].append({"project_dir": d.name, "files": files})
    am = config_dir / "agent-memory"
    if am.is_dir():
        for d in sorted(p for p in am.iterdir() if p.is_dir()):
            out["agent_memory"].append({"agent": d.name, "files": [f.name for f in sorted(d.rglob("*.md"))]})
    return out


def read_file_history(config_dir: Path) -> list[dict]:
    out = []
    d = Path(config_dir) / "file-history"
    if d.is_dir():
        for s in sorted(p for p in d.iterdir() if p.is_dir()):
            st = _dir_stats(s)
            out.append({"session": s.name, "files": st["files"], "bytes": st["bytes"]})
    return out


# --------------------------------------------------------------------------- #
# extensions / IDE / plugins / misc dirs
# --------------------------------------------------------------------------- #

def read_extensions(config_dir: Path) -> dict[str, list[str]]:
    config_dir = Path(config_dir)
    out: dict[str, list[str]] = {}
    for name in ("agents", "commands", "rules", "output-styles", "workflows"):
        d = config_dir / name
        out[name] = sorted(p.name for p in d.rglob("*") if p.is_file()) if d.is_dir() else []
    sk = config_dir / "skills"
    out["skills"] = sorted(p.parent.name for p in sk.rglob("SKILL.md")) if sk.is_dir() else []
    return out


def read_plugins(config_dir: Path) -> list[dict]:
    data = _load_json(Path(config_dir) / "plugins" / "installed_plugins.json")
    out = []
    if isinstance(data, dict):
        plugins = data.get("plugins", data)
        if isinstance(plugins, dict):
            for name, inst in plugins.items():
                ver = ""
                if isinstance(inst, list) and inst and isinstance(inst[0], dict):
                    ver = str(inst[0].get("version", ""))
                elif isinstance(inst, dict):
                    ver = str(inst.get("version", ""))
                out.append({"name": name, "version": ver})
    return out


def read_ide_locks(config_dir: Path) -> list[dict]:
    out = []
    d = Path(config_dir) / "ide"
    if d.is_dir():
        for f in sorted(d.glob("*.lock")):
            data = _load_json(f)
            if isinstance(data, dict):
                out.append({"file": f.name, "ide": str(data.get("ideName", "")), "pid": data.get("pid"),
                            "workspaces": [str(w) for w in data.get("workspaceFolders", []) if w],
                            "has_auth_token": bool(data.get("authToken")), "transport": str(data.get("transport", ""))})
    return out


MISC_DIRS = ("shell-snapshots", "paste-cache", "image-cache", "uploads", "debug", "telemetry", "backups",
             "usage-data", "session-env", "statsig", "downloads", "feedback-bundles", "plans", "tasks", "todos")


def read_misc(config_dir: Path) -> dict[str, dict]:
    """Per-directory file count / size / newest mtime (standalone; ``scanner.quick_stats`` computes the same in its one walk)."""
    config_dir = Path(config_dir)
    out: dict[str, dict] = {}
    for name in MISC_DIRS:
        d = config_dir / name
        if d.is_dir():
            out[name] = _dir_stats(d)
    return out


def read_telemetry_events(config_dir: Path, limit: int = 200) -> dict:
    """Names only of failed-upload telemetry events (payloads may hold paths / ids)."""
    d = Path(config_dir) / "telemetry"
    names: Counter = Counter()
    files = 0
    if d.is_dir():
        for f in sorted(d.glob("*.json"))[:limit]:
            files += 1
            data = _load_json(f)
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict):
                    ed = it.get("event_data") if isinstance(it.get("event_data"), dict) else {}
                    names[str(ed.get("event_name") or it.get("event_type") or "?")] += 1
    return {"files": files, "events": dict(names)}


def read_mcp_logs(cache_dirs: Iterable[Path], limit: int = 60) -> list[dict]:
    out = []
    for base in cache_dirs:
        base = Path(base)
        if not base.is_dir():
            continue
        for slug in sorted(p for p in base.iterdir() if p.is_dir()):
            for sub in sorted(p for p in slug.iterdir() if p.is_dir()):
                st = _dir_stats(sub)
                out.append({"project": slug.name, "kind": sub.name, "files": st["files"], "bytes": st["bytes"],
                            "newest": st["newest"]})
                if len(out) >= limit:
                    return out
    return out


def read_debug_tail(config_dir: Path, redactor: Redactor, limit: int = 1000) -> list[str]:
    """Most recent ``limit`` lines across the newest debug logs (redacted)."""
    d = Path(config_dir) / "debug"
    if not d.is_dir():
        return []
    files = sorted((f for f in d.glob("*.txt") if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
    lines: list[str] = []
    for f in files[:8]:
        try:
            chunk = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        lines = [f"[{f.stem[:8]}] {redactor.apply(ln)[:400]}" for ln in chunk[-limit:]] + lines
        if len(lines) >= limit:
            break
    return lines[-limit:]


def inventory(config_dir: Path) -> list[tuple[str, bool, str]]:
    """(name, present, description) rows for the dashboard inventory list."""
    config_dir = Path(config_dir)
    rows = []
    desc = {
        "projects": "Session transcripts (JSONL) - the conversations",
        "history.jsonl": "Prompt history (kept until you delete it)",
        "stats-cache.json": "Usage aggregates behind /usage",
        "settings.json": "User settings, permissions, hooks",
        "CLAUDE.md": "User memory file",
        ".credentials.json": "OAuth tokens (secrets - never read, presence only)",
        "file-history": "Pre-edit file snapshots (checkpoints)",
        "plans": "Plan-mode documents",
        "tasks": "Task lists",
        "todos": "Legacy TodoWrite state",
        "debug": "Per-session debug logs",
        "shell-snapshots": "Shell environment captures",
        "paste-cache": "Large pastes",
        "telemetry": "Failed-upload telemetry events",
        "ide": "IDE integration lock files (auth token inside)",
        "plugins": "Installed plugins & marketplaces",
        "agents": "Custom subagent definitions",
        "skills": "Skills",
        "commands": "Slash commands",
        "backups": "Rotating ~/.claude.json backups",
        "usage-data": "/insights reports & per-session analysis",
    }
    for name, text in desc.items():
        rows.append((name, (config_dir / name).exists(), text))
    rows.append((".claude.json", paths.claude_global_json(config_dir) is not None,
                 "Global app state (OAuth metadata, per-project stats, MCP servers)"))
    return rows
