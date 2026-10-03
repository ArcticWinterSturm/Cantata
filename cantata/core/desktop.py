"""Claude Desktop application-data reader (Cowork, Code-tab wrappers, memory, logs, config).

Layout (docs/CLAUDE_STORAGE.md section 2):

    <root>/local-agent-mode-sessions/<account>/<org>/local_<uuid>.json      Cowork manifest
    <root>/local-agent-mode-sessions/<account>/<org>/local_<uuid>/          session working dir
        .claude/projects/-sessions-<process>/<cliSessionId>.jsonl           full transcript
        audit.jsonl                                                          HMAC-chained event log
        uploads/  outputs/
    <root>/claude-code-sessions/<account>/<org>/local_<uuid>.json           Code-tab wrapper (metadata only;
                                                                            transcript is in ~/.claude/projects)

Nothing here modifies anything, and ``.audit-key`` / encrypted ``config.json`` blobs are never read.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import configs, paths, scanner
from .transcripts import iso_from_epoch

_LOCAL_JSON = re.compile(r"^local_[0-9a-fA-F\-]{8,}\.json$")


@dataclass
class DesktopSession:
    kind: str = "cowork"                 # cowork | chat-local | code-tab
    local_id: str = ""
    cli_session_id: str = ""
    title: str = ""
    model: str = ""
    created_at: str = ""
    last_activity: str = ""
    cwd: str = ""
    folders: list[str] = field(default_factory=list)
    archived: bool = False
    error: str = ""
    initial_message: str = ""
    account_id: str = ""
    org_id: str = ""
    account_name: str = ""
    email: str = ""
    scheduled_task: str = ""
    manifest_path: Optional[Path] = None
    session_dir: Optional[Path] = None
    transcripts: list = field(default_factory=list)       # list[scanner.TranscriptFile]
    audit_path: Optional[Path] = None
    audit: dict = field(default_factory=dict)
    uploads: int = 0
    outputs: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class DesktopInfo:
    roots: list = field(default_factory=list)
    variants: list = field(default_factory=list)          # [{path, flavour, bytes?}]
    sessions: list = field(default_factory=list)          # list[DesktopSession]
    configs: list = field(default_factory=list)           # per root: configs.read_desktop_config
    logs: list = field(default_factory=list)
    memory: list = field(default_factory=list)
    extensions: list = field(default_factory=list)
    output_dirs: list = field(default_factory=list)
    vm_entries: list = field(default_factory=list)
    has_device_id: bool = False
    warnings: list = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.roots)

    def stats(self) -> dict:
        cw = [s for s in self.sessions if s.kind in ("cowork", "chat-local")]
        ct = [s for s in self.sessions if s.kind == "code-tab"]
        return {
            "roots": len(self.roots), "cowork_sessions": len(cw), "code_tab_sessions": len(ct),
            "cowork_transcripts": sum(len(s.transcripts) for s in cw),
            "audit_logs": sum(1 for s in cw if s.audit_path), "memory_files": sum(len(m["files"]) for m in self.memory),
            "logs": len(self.logs), "extensions": len(self.extensions),
            "cost_usd": round(sum(float(s.audit.get("total_cost_usd") or 0) for s in cw), 4),
            "api_ms": sum(int(s.audit.get("duration_api_ms") or 0) for s in cw),
        }


def _flavour(root: Path) -> str:
    s = str(root).replace("\\", "/")
    if "/Packages/Claude_" in s:
        return "MSIX (virtualised Roaming)"
    if root.name.endswith("-3p"):
        return "third-party inference (3P)"
    return "standard"


def _count_files(path: Path, cap: int = 10_000) -> int:
    n = 0
    if path.is_dir():
        for _r, _d, files in os.walk(path):
            n += len(files)
            if n >= cap:
                break
    return n


def read_audit(path: Path, line_cap: int = 200_000) -> dict:
    """Summarise an ``audit.jsonl``: init metadata, ``result`` cost/duration lines, rate-limit events."""
    out: dict = {"lines": 0, "types": Counter(), "hmac_fields": False, "results": 0, "total_cost_usd": 0.0,
                 "duration_ms": 0, "duration_api_ms": 0, "num_turns": 0, "model": "", "version": "", "tools": 0,
                 "mcp_servers": [], "rate_limit_events": 0, "permission_mode": "", "first": "", "last": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if out["lines"] >= line_cap:
                    break
                line = line.strip()
                if not line:
                    continue
                out["lines"] += 1
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                t = str(d.get("type") or "?")
                out["types"][t] += 1
                ts = d.get("_audit_timestamp") or d.get("timestamp")
                if ts:
                    out["first"] = out["first"] or str(ts)
                    out["last"] = str(ts)
                if any(k in d for k in ("_audit_hmac", "hmac", "_hmac", "prev_hmac", "_audit_prev")):
                    out["hmac_fields"] = True
                if t == "system" and d.get("subtype") == "init":
                    out["model"] = str(d.get("model") or out["model"])
                    out["version"] = str(d.get("claude_code_version") or d.get("version") or out["version"])
                    out["tools"] = len(d.get("tools") or [])
                    out["permission_mode"] = str(d.get("permissionMode") or d.get("permission_mode") or "")
                    out["mcp_servers"] = [str(m.get("name", m)) if isinstance(m, dict) else str(m)
                                          for m in (d.get("mcp_servers") or [])]
                elif t == "result":
                    out["results"] += 1
                    out["total_cost_usd"] += float(d.get("total_cost_usd") or 0)
                    out["duration_ms"] += int(d.get("duration_ms") or 0)
                    out["duration_api_ms"] += int(d.get("duration_api_ms") or 0)
                    out["num_turns"] += int(d.get("num_turns") or 0)
                elif t == "rate_limit_event":
                    out["rate_limit_events"] += 1
    except OSError as exc:
        out["error"] = str(exc)
    out["types"] = dict(out["types"])
    out["total_cost_usd"] = round(out["total_cost_usd"], 4)
    return out


def _session_from_manifest(manifest: Path, kind: str, acct: str, org: str, read_audits: bool) -> Optional[DesktopSession]:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    s = DesktopSession(kind=kind, manifest_path=manifest, account_id=acct, org_id=org)
    s.local_id = str(data.get("sessionId") or manifest.stem)
    s.cli_session_id = str(data.get("cliSessionId") or "")
    s.title = str(data.get("title") or "")
    s.model = str(data.get("model") or "")
    s.created_at = iso_from_epoch(data.get("createdAt"))
    s.last_activity = iso_from_epoch(data.get("lastActivityAt"))
    s.cwd = str(data.get("cwd") or data.get("originCwd") or "")
    s.folders = [str(f) for f in data.get("userSelectedFolders", []) if f]
    s.archived = bool(data.get("isArchived"))
    s.error = str(data.get("error") or "")
    s.initial_message = str(data.get("initialMessage") or "")
    s.account_name = str(data.get("accountName") or "")
    s.email = str(data.get("emailAddress") or "")
    s.scheduled_task = str(data.get("scheduledTaskId") or "")
    mode = str(data.get("sessionType") or data.get("mode") or "").lower()
    if kind == "cowork" and mode == "chat":
        s.kind = "chat-local"
    s.extra = {
        "process_name": data.get("processName"), "permission_mode": data.get("permissionMode"),
        "effort": data.get("effort"), "completed_turns": data.get("completedTurns"),
        "worktree": data.get("worktreePath"), "source_branch": data.get("sourceBranch"), "branch": data.get("branch"),
        "mcp_tools_enabled": sum(1 for v in (data.get("enabledMcpTools") or {}).values() if v) if isinstance(data.get("enabledMcpTools"), dict) else 0,
        "remote_mcp_servers": len(data.get("remoteMcpServersConfig") or []),
        "egress": data.get("egressAllowedDomains"), "system_prompt_chars": len(str(data.get("systemPrompt") or "")),
        "chrome_mode": data.get("chromePermissionMode"), "title_source": data.get("titleSource"),
        "manifest_keys": sorted(data.keys()),
    }
    if kind == "cowork":
        sd = manifest.with_suffix("")
        if sd.is_dir():
            s.session_dir = sd
            s.transcripts = scanner.discover_transcripts(sd / ".claude" / "projects", cowork=True)
            audit = sd / "audit.jsonl"
            if audit.is_file():
                s.audit_path = audit
                if read_audits:
                    s.audit = read_audit(audit)
            s.uploads = _count_files(sd / "uploads")
            s.outputs = _count_files(sd / "outputs")
    return s


def scan_desktop(roots: Optional[list] = None, *, read_audits: bool = True,
                 log: Optional[Callable[[str, str], None]] = None) -> DesktopInfo:
    """Inventory every Desktop data dir (defaults to auto-detected ones)."""
    log = log or (lambda m, lvl="info": None)
    roots = paths.desktop_roots() if roots is None else [Path(r) for r in roots]        # [] = deliberately none
    info = DesktopInfo(roots=[r for r in roots if r.is_dir()])
    info.output_dirs = [str(p) for p in paths.desktop_output_dirs()]
    for root in info.roots:
        info.variants.append({"path": str(root), "flavour": _flavour(root)})
        log(f"Claude Desktop data: {root} [{_flavour(root)}]", "info")
        info.configs.append({"root": str(root), **configs.read_desktop_config(root)})
        info.has_device_id = info.has_device_id or (root / "ant-did").is_file()

        for dirname, kind in (("local-agent-mode-sessions", "cowork"), ("claude-code-sessions", "code-tab")):
            base = root / dirname
            if not base.is_dir():
                continue
            for r, dirs, files in os.walk(base):
                # session working dirs (local_<uuid>/: .claude, outputs, uploads ...) are reached through their manifests;
                # descending into them would walk thousands of user files for nothing
                dirs[:] = [d for d in dirs if not d.startswith("local_")]
                parts = Path(r).relative_to(base).parts
                if len(parts) > 5:
                    dirs[:] = []
                    continue
                if len(parts) <= 3:
                    for name in files:
                        if _LOCAL_JSON.match(name):
                            acct = parts[0] if len(parts) > 0 else ""
                            org = parts[1] if len(parts) > 1 else ""
                            sess = _session_from_manifest(Path(r) / name, kind, acct, org, read_audits)
                            if sess:
                                info.sessions.append(sess)
                # Cowork memory: <acct>/<org>/memory and <acct>/<org>/spaces/<projectId>/memory (markdown notes)
                if Path(r).name == "memory" and kind == "cowork":
                    notes = [str(p.relative_to(r)) for p in sorted(Path(r).rglob("*.md"))]
                    if notes:
                        info.memory.append({"path": str(r), "files": notes, "scope": "project" if "spaces" in Path(r).parts else "account"})
                    dirs[:] = []
        logs = root / "logs"
        if logs.is_dir():
            for f in sorted(logs.glob("*")):
                if f.is_file():
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    info.logs.append({"name": f.name, "bytes": st.st_size, "mtime": st.st_mtime, "path": str(f)})
        for extdir in ("Claude Extensions", "Claude Extensions Settings"):
            d = root / extdir
            if d.is_dir():
                info.extensions += [f"{extdir}/{p.name}" for p in sorted(d.iterdir())][:50]
        for vm in ("vm_bundles", "claude-code-vm", "claude-code"):
            d = root / vm
            if d.is_dir():
                try:
                    size = sum(p.stat().st_size for p in d.iterdir() if p.is_file())
                except OSError:
                    size = 0
                info.vm_entries.append({"name": vm, "entries": sum(1 for _ in d.iterdir()), "bytes": size})
    for logdir in paths.desktop_log_dirs():
        for f in sorted(logdir.glob("*")):
            if f.is_file():
                info.logs.append({"name": f.name, "bytes": f.stat().st_size, "mtime": f.stat().st_mtime, "path": str(f)})
    info.sessions.sort(key=lambda s: s.last_activity or s.created_at, reverse=True)
    if not info.roots:
        info.warnings.append("No Claude Desktop data directory found on this machine.")
    return info


def tail_log(path: str, redactor, limit: int = 200) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()[-limit:]
    except OSError:
        return []
    return [redactor.apply(ln)[:400] for ln in lines]
