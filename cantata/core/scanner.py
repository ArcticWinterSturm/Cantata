"""Discovery: find transcripts, detect the install, compute dashboard numbers.

``quick_stats`` does a *single* ``os.walk`` over the config dir (no repeated rglob) so it stays
fast on multi-GB ``~/.claude`` trees.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import paths

DEFAULT_RETENTION_DAYS = 30
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ORPHAN_RE = re.compile(r"^(?P<sid>.+?)\.orphaned-.+\.jsonl$")
_SUPERSEDED_RE = re.compile(r"^(?P<sid>.+?)\.jsonl\.superseded-.+$")
_VERSION_RE = re.compile(r'"version"\s*:\s*"(\d+\.\d+\.\d+[^"]{0,20})"')

KNOWN_FILES = (
    "settings.json", "settings.local.json", "CLAUDE.md", "history.jsonl", "stats-cache.json",
    ".credentials.json", "keybindings.json", "remote-settings.json", "policy-limits.json",
)
KNOWN_DIRS = (
    "projects", "file-history", "plans", "tasks", "todos", "session-env", "shell-snapshots", "debug",
    "paste-cache", "image-cache", "uploads", "backups", "usage-data", "telemetry", "ide", "plugins",
    "agents", "skills", "commands", "rules", "output-styles", "statsig", "chrome", "cache",
)


@dataclass
class TranscriptFile:
    path: Path
    kind: str = "main"        # main | orphaned | superseded | subagent | cowork | cowork-subagent
    project_dir: str = ""
    session_id: str = ""      # derived from the file name
    parent_session_id: str = ""
    agent_id: str = ""
    size: int = 0
    mtime: float = 0.0
    origin_hint: str = ""
    meta_path: Optional[Path] = None


def detect_default_source() -> Path:
    return paths.claude_config_dir()


def is_claude_dir(path: Path) -> bool:
    path = Path(path)
    if not path.is_dir():
        return False
    return (path / "projects").is_dir() or (path / "history.jsonl").is_file() or \
        (path / "settings.json").is_file() or (path / "stats-cache.json").is_file()


# --------------------------------------------------------------------------- #
# transcript discovery
# --------------------------------------------------------------------------- #

def discover_transcripts(projects_dir: Path, *, cowork: bool = False, errors: Optional[list] = None) -> list[TranscriptFile]:
    """Every ``*.jsonl`` transcript under a ``projects/`` directory (Code or Cowork layout).

    Paths that cannot be listed / stat-ed (permissions, Windows' 260-character limit) are appended to ``errors`` rather than
    silently dropped.
    """
    projects_dir = Path(projects_dir)
    found: list[TranscriptFile] = []
    if not projects_dir.is_dir():
        return found

    def _onerror(exc: OSError) -> None:
        if errors is not None:
            errors.append(f"{getattr(exc, 'filename', '?')}: {getattr(exc, 'strerror', exc)}")
    base_kind = "cowork" if cowork else "main"
    sub_kind = "cowork-subagent" if cowork else "subagent"
    hint = "desktop-cowork" if cowork else ""

    for root, dirs, files in os.walk(projects_dir, onerror=_onerror):
        rel = Path(root).relative_to(projects_dir).parts
        if not rel:
            continue                        # files directly under projects/ are not transcripts
        project_dir = rel[0]
        for name in files:
            if not (name.endswith(".jsonl") or ".jsonl.superseded-" in name):
                continue
            p = Path(root) / name
            try:
                st = os.stat(paths.long_path(p))
            except OSError as exc:
                if errors is not None:
                    errors.append(f"{p}: {getattr(exc, 'strerror', None) or exc}")
                continue
            tf = TranscriptFile(path=p, project_dir=project_dir, size=st.st_size, mtime=st.st_mtime,
                                origin_hint=hint)
            if len(rel) == 1:                                   # projects/<proj>/<file>
                m_o, m_s = _ORPHAN_RE.match(name), _SUPERSEDED_RE.match(name)
                if m_o:
                    tf.kind, tf.session_id = "orphaned", m_o.group("sid")
                elif m_s:
                    tf.kind, tf.session_id = "superseded", m_s.group("sid")
                elif name.startswith("agent-"):                  # legacy subagent next to parent
                    tf.kind = sub_kind
                    tf.agent_id = name[len("agent-"):-len(".jsonl")]
                    tf.session_id = f"agent-{tf.agent_id}"
                else:
                    tf.kind, tf.session_id = base_kind, name[:-len(".jsonl")]
            elif "subagents" in rel:                             # projects/<proj>/<sid>/subagents/**
                if not name.startswith("agent-"):
                    continue
                tf.kind = sub_kind
                tf.parent_session_id = rel[1]
                tf.agent_id = name[len("agent-"):-len(".jsonl")]
                tf.session_id = f"agent-{tf.agent_id}"
                meta = Path(root) / f"agent-{tf.agent_id}.meta.json"
                tf.meta_path = meta if meta.is_file() else None
            else:
                continue                                         # tool-results/, memory/, ...
            found.append(tf)
    found.sort(key=lambda t: (t.mtime, str(t.path)))
    return found


def discover_code_transcripts(config_dir: Path, include_orphaned: bool = True, errors: Optional[list] = None) -> list[TranscriptFile]:
    files = discover_transcripts(Path(config_dir) / "projects", errors=errors)
    if not include_orphaned:
        files = [f for f in files if f.kind not in ("orphaned", "superseded")]
    return files


# --------------------------------------------------------------------------- #
# dashboard numbers
# --------------------------------------------------------------------------- #

def _count_lines(path: Path, cap: int = 5_000_000) -> int:
    n = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
                if n > cap:
                    break
    except OSError:
        return 0
    return n


def _tail_versions(path: Path, n: int = 4) -> list[str]:
    out: list[str] = []
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(16384).decode("utf-8", "replace")
            fh.seek(max(0, size - 16384))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return out
    for text in (head, tail):
        out += _VERSION_RE.findall(text)
    return out


def read_retention_days(config_dir: Path) -> Optional[int]:
    """``cleanupPeriodDays`` from user settings (``None`` -> default applies)."""
    for name in ("settings.json", "settings.local.json"):
        try:
            data = json.loads((Path(config_dir) / name).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        val = data.get("cleanupPeriodDays") if isinstance(data, dict) else None
        if isinstance(val, int) and val > 0:
            return val
    return None


def quick_stats(config_dir: Path) -> dict:
    config_dir = Path(config_dir)
    stats: dict = {
        "config_dir": str(config_dir), "exists": config_dir.is_dir(), "looks_like_claude": False,
        "projects": 0, "active_transcripts": 0, "orphaned_transcripts": 0, "subagents": 0,
        "history_entries": 0, "size_mb": 0.0, "transcript_mb": 0.0, "date_range": ("", ""),
        "retention_days": None, "retention_effective": DEFAULT_RETENTION_DAYS,
        "oldest_age_days": None, "expiring_soon": 0, "tokens_total": 0, "sessions_reported": 0,
        "present_files": {}, "present_dirs": {}, "versions": [], "app_version": "",
        "has_credentials": False, "global_json": "", "desktop_roots": [], "issues": [],
        "file_history_sessions": 0, "plans": 0, "memory_files": 0, "file_history": [], "dir_stats": {},
    }
    if not config_dir.is_dir():
        stats["issues"].append(f"Directory not found: {config_dir}")
        return stats

    total = tx_bytes = 0
    mtimes: list[float] = []
    newest: list[tuple[float, Path]] = []
    projects: set[str] = set()
    file_hist: dict[str, list] = {}                 # session -> [files, bytes]
    dir_stats: dict[str, dict] = {}                 # top-level dir -> files / bytes / newest mtime
    for root, dirs, files in os.walk(config_dir):
        rel = Path(root).relative_to(config_dir).parts
        top = rel[0] if rel else ""
        for name in files:
            try:
                st_ = os.stat(os.path.join(root, name))
            except OSError:
                continue
            size = st_.st_size
            total += size
            if top:
                d = dir_stats.setdefault(top, {"files": 0, "bytes": 0, "newest": 0.0})
                d["files"] += 1
                d["bytes"] += size
                d["newest"] = max(d["newest"], st_.st_mtime)
            if top == "projects" and len(rel) >= 2:
                projects.add(rel[1])
                if name.endswith(".jsonl") or ".jsonl.superseded-" in name:
                    tx_bytes += size
                    full = Path(root) / name
                    if len(rel) == 2:
                        if _ORPHAN_RE.match(name) or _SUPERSEDED_RE.match(name):
                            stats["orphaned_transcripts"] += 1
                        elif name.startswith("agent-"):
                            stats["subagents"] += 1
                        else:
                            stats["active_transcripts"] += 1
                            try:
                                mt = os.stat(full).st_mtime
                                mtimes.append(mt)
                                newest.append((mt, full))
                            except OSError:
                                pass
                    elif "subagents" in rel:
                        stats["subagents"] += 1
                elif len(rel) >= 3 and rel[2] == "memory":
                    stats["memory_files"] += 1
            elif top == "file-history" and len(rel) >= 2:
                fh = file_hist.setdefault(rel[1], [0, 0])
                fh[0] += 1
                fh[1] += size
            elif top == "plans" and name.endswith(".md"):
                stats["plans"] += 1
    stats["projects"] = len(projects)
    stats["size_mb"] = round(total / 1_048_576, 1)
    stats["transcript_mb"] = round(tx_bytes / 1_048_576, 1)
    stats["file_history_sessions"] = len(file_hist)
    stats["file_history"] = [{"session": k, "files": v[0], "bytes": v[1]} for k, v in sorted(file_hist.items())]
    stats["dir_stats"] = dir_stats

    stats["present_files"] = {n: (config_dir / n).is_file() for n in KNOWN_FILES}
    stats["present_dirs"] = {n: (config_dir / n).is_dir() for n in KNOWN_DIRS}
    stats["has_credentials"] = stats["present_files"].get(".credentials.json", False)
    stats["looks_like_claude"] = is_claude_dir(config_dir)
    gj = paths.claude_global_json(config_dir)
    stats["global_json"] = str(gj) if gj else ""
    if (config_dir / "history.jsonl").is_file():
        stats["history_entries"] = _count_lines(config_dir / "history.jsonl")

    retention = read_retention_days(config_dir)
    stats["retention_days"] = retention
    eff = retention or DEFAULT_RETENTION_DAYS
    stats["retention_effective"] = eff
    now = time.time()
    if mtimes:
        lo, hi = min(mtimes), max(mtimes)
        stats["date_range"] = (datetime.fromtimestamp(lo, timezone.utc).strftime("%Y-%m-%d"),
                               datetime.fromtimestamp(hi, timezone.utc).strftime("%Y-%m-%d"))
        stats["oldest_age_days"] = round((now - lo) / 86400, 1)
        stats["expiring_soon"] = sum(1 for m in mtimes if (now - m) / 86400 >= eff - 7)

    # versions: newest transcripts first
    newest.sort(reverse=True)
    seen: list[str] = []
    for _, p in newest[:6]:
        for v in _tail_versions(p):
            if v not in seen:
                seen.append(v)
    seen.sort(key=_vkey, reverse=True)
    stats["versions"] = seen
    stats["app_version"] = seen[0] if seen else ""

    # stats-cache: tokens + reported session count
    try:
        sc = json.loads((config_dir / "stats-cache.json").read_text(encoding="utf-8", errors="replace"))
        usage = sc.get("modelUsage") or {}
        stats["tokens_total"] = sum(int(v.get("inputTokens", 0)) + int(v.get("outputTokens", 0))
                                    for v in usage.values() if isinstance(v, dict))
        stats["sessions_reported"] = int(sc.get("totalSessions") or 0)
    except (OSError, ValueError, AttributeError):
        pass

    stats["desktop_roots"] = [str(p) for p in paths.desktop_roots()]

    # issues / hints -------------------------------------------------------------
    issues = stats["issues"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        issues.append(f"CLAUDE_CONFIG_DIR is set ({os.environ['CLAUDE_CONFIG_DIR']}).")
    if not stats["looks_like_claude"]:
        issues.append("Does not look like a Claude Code data directory (no projects/, history.jsonl, settings).")
    if stats["sessions_reported"] and stats["active_transcripts"] < stats["sessions_reported"]:
        gone = stats["sessions_reported"] - stats["active_transcripts"]
        issues.append(f"stats-cache.json counts {stats['sessions_reported']} sessions but only "
                      f"{stats['active_transcripts']} transcripts remain - ~{gone} aged out or were deleted.")
    issues.append(f"Claude Code deletes transcripts older than {eff} days"
                  + ("" if retention else " (default cleanupPeriodDays)") + "."
                  + (f" {stats['expiring_soon']} expire within a week - archive now." if stats["expiring_soon"] else ""))
    return stats


def _vkey(v: str) -> tuple:
    nums = re.findall(r"\d+", v)[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))
