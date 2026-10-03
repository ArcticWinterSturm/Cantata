"""Recovery orchestrator: discover -> parse -> link -> enrich -> redact -> write.

Callback trio (same contract as every long-running operation in Cantata):
``log(msg, level)``, ``progress(fraction, label)``, ``cancel() -> bool`` (truthy raises ``RecoveryCancelled``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from .. import __version__
from . import configs, desktop as desktop_mod, paths, report, scanner, stores
from .models import RecoveryCancelled, RecoveryOptions, RecoveryResult, SessionRecord, VersionInfo
from .redaction import Redactor
from .transcripts import link_subagents, parse_transcript

_HOME_RX = re.compile(r"^(/home/[^/]+|/Users/[^/]+|[A-Za-z]:[\\/]+Users[\\/]+[^\\/]+)", re.I)


def _noop_log(msg: str, level: str = "info") -> None:
    pass


def derive_homes(sessions: list[SessionRecord], source: Path) -> list[str]:
    """Home directories to fold to ``~`` - the machine's own, the image's, and those seen in cwd fields."""
    homes = {str(Path.home())}
    if source.name == ".claude":
        homes.add(str(source.parent))
    for s in sessions:
        for cand in (s.project_path, *s.tracked_files[:3]):
            m = _HOME_RX.match(cand or "")
            if m:
                homes.add(m.group(1))
    return sorted(homes)


def _vkey(v: str) -> tuple:
    nums = re.findall(r"\d+", v)[:3]
    return tuple(int(x) for x in nums) + (0,) * (3 - len(nums))


def collect_bundle(source: Path, opts: RecoveryOptions, log: Callable, progress: Callable, cancel: Callable) -> dict:
    """Parse everything and return the bundle consumed by the report writer (also used by audit/flight)."""
    def check() -> None:
        if cancel and cancel():
            raise RecoveryCancelled()

    progress(0.01, "Scanning directory…")
    warnings: list[str] = []
    unreadable: list[str] = []
    files = scanner.discover_code_transcripts(source, include_orphaned=opts.include_orphaned, errors=unreadable)
    log(f"Found {len(files)} Claude Code transcript file(s) under {source}", "info")
    if unreadable:
        msg = (f"{len(unreadable)} path(s) under projects/ could not be listed or read (first: {unreadable[0]}). On Windows this is usually "
               "the 260-character path limit - enable LongPathsEnabled or copy the folder to a shorter path.")
        warnings.append(msg)
        log(msg, "warn")

    info = None
    cowork_jobs: list[tuple] = []
    live = paths.is_live_source(source)
    if opts.include_desktop:
        # Explicit dirs always win. Auto-detection only makes sense for *this machine's* live ~/.claude - for a copied
        # forensic image it would silently mix in the analyst's own Claude Desktop data.
        roots = list(opts.desktop_dirs) if opts.desktop_dirs else (None if live else [])
        if roots == []:
            log("Claude Desktop auto-detection skipped: the source is not this machine's live Claude dir "
                "(pass --desktop-dir / the Desktop field to include a copied Desktop app-data directory).", "info")
        info = desktop_mod.scan_desktop(roots, log=log)
        if info.found:
            for ds in info.sessions:
                for tf in ds.transcripts:
                    cowork_jobs.append((tf, ds))
            st = info.stats()
            log(f"Claude Desktop: {st['cowork_sessions']} Cowork/chat session(s), {st['code_tab_sessions']} Code-tab wrapper(s), "
                f"{len(cowork_jobs)} Cowork transcript(s)", "ok")
        else:
            log("Claude Desktop data not found (skipping)", "info")
    check()

    total_bytes = sum(f.size for f in files) + sum(tf.size for tf, _ in cowork_jobs) or 1
    done_before = 0
    sessions: list[SessionRecord] = []

    def parse_one(tf, ds=None) -> None:
        nonlocal done_before
        check()
        label = tf.path.name if len(tf.path.name) < 26 else tf.path.name[:23] + "…"

        def on_bytes(nb: int) -> None:
            progress(0.05 + 0.70 * min(1.0, (done_before + nb) / total_bytes), f"Parsing {label}")
        try:
            rec = parse_transcript(tf, on_bytes=on_bytes, cancel=cancel)
        except RecoveryCancelled:
            raise
        except Exception as exc:                                       # never let one bad file abort the run
            warnings.append(f"Could not parse {tf.path.name}: {type(exc).__name__}: {exc}")
            log(f"Could not parse {tf.path.name}: {exc}", "warn")
            done_before += tf.size
            return
        done_before += tf.size
        if ds is not None:
            rec.origin = "desktop-cowork"
            rec.extra["desktop"] = {"local_id": ds.local_id, "title": ds.title, "model": ds.model,
                                    "audit": {k: v for k, v in ds.audit.items() if k != "types"}}
            rec.title = rec.title or ds.title
            if not rec.project_path:
                rec.project_path = ds.cwd
        sessions.append(rec)
        for child in rec.extra.pop("inline_children", []):
            child.origin = child.origin or rec.origin
            sessions.append(child)

    for tf in files:
        parse_one(tf)
    for tf, ds in cowork_jobs:
        parse_one(tf, ds)

    progress(0.78, "Linking sessions…")
    link_subagents(sessions)
    wrappers = {}
    if info is not None:
        wrappers = {s.cli_session_id: s for s in info.sessions if s.kind == "code-tab" and s.cli_session_id}
    indexes = stores.read_sessions_indexes(source / "projects")
    idx_title = {}
    for proj_dir, idx in indexes.items():
        for e in idx["entries"]:
            if e.get("sessionId"):
                idx_title[str(e["sessionId"])] = e
    for s in sessions:
        w = wrappers.get(s.session_id)
        if w is not None and s.kind == "main":
            s.origin = "desktop-code"
            s.extra["wrapper"] = {"local_id": w.local_id, "title": w.title, "model": w.model, "completed_turns": w.extra.get("completed_turns"),
                                  "worktree": w.extra.get("worktree"), "archived": w.archived}
            s.title = s.title or w.title
        e = idx_title.get(s.session_id)
        if e and not (s.custom_title or s.ai_title or s.summary or s.title):
            s.title = str(e.get("summary") or "")
    check()

    # --- state stores / config ------------------------------------------------------------------
    stats = scanner.quick_stats(source)
    vinfo = VersionInfo()
    vers: dict[str, int] = {}
    ents: list[str] = []
    for s in sessions:
        for v in s.versions:
            vers[v] = vers.get(v, 0) + 1
        for e in s.entrypoints:
            if e not in ents:
                ents.append(e)
    vinfo.claude_code_versions = sorted(vers, key=_vkey, reverse=True)
    vinfo.entrypoints = ents
    vinfo.cleanup_period_days = stats.get("retention_days")

    project_paths = sorted({s.project_path for s in sessions if s.project_path and Path(s.project_path).is_absolute()})
    bundle: dict = {"source": source, "sessions": sessions, "version_info": vinfo, "stats": stats, "warnings": warnings,
                    "desktop": info, "generated": report.stamp(), "live_source": live}
    history = stores.read_history(source)
    bundle["history"] = history
    bundle["stats_cache"] = stores.read_stats_cache(source)
    bundle["index_only"] = stores.index_only_sessions(history, indexes, bundle["stats_cache"],
                                                      {s.session_id for s in sessions if s.kind in ("main", "orphaned", "superseded")})
    if opts.include_app_state:
        progress(0.82, "Reading application state…")
        gj = configs.read_global_json(paths.claude_global_json(source))
        vinfo.install_method = str(gj.get("install_method") or "")
        bundle.update({
            "global": gj,
            "settings": configs.read_settings(source, project_paths),
            "credentials": configs.read_credentials(source),
            "mcp": configs.effective_mcp_servers(gj, configs.project_mcp_files(project_paths)),
            "memory": stores.read_memory(source, project_paths),
            "extensions": stores.read_extensions(source),
            "plugins": stores.read_plugins(source),
            "plans": stores.read_plans(source),
            "todos": stores.read_todos(source),
            "tasks": stores.read_tasks(source),
            "ide": stores.read_ide_locks(source),
            "misc": {k: v for k, v in stats["dir_stats"].items() if k in stores.MISC_DIRS},      # from the single walk
            "file_history": stats["file_history"],
            "telemetry": stores.read_telemetry_events(source),
        })
    if info is not None and info.found and info.configs:
        bundle["desktop_config"] = info.configs
    return bundle


def run_recovery(source, output, options: Optional[RecoveryOptions] = None, *,
                 log: Optional[Callable] = None, progress: Optional[Callable] = None,
                 cancel: Optional[Callable] = None) -> RecoveryResult:
    opts = options or RecoveryOptions()
    log = log or _noop_log
    progress = progress or (lambda frac, label="": None)
    source, output = Path(source), Path(output)
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    if opts.redaction_level not in RecoveryOptions.LEVELS:
        raise ValueError(f"Unknown redaction level: {opts.redaction_level}")

    bundle = collect_bundle(source, opts, log, progress, cancel or (lambda: False))
    sessions: list[SessionRecord] = bundle["sessions"]
    if not sessions:
        log("No transcripts found - the report will only contain state and configuration.", "warn")
        bundle["warnings"].append("No transcripts found. Claude Code deletes transcripts after `cleanupPeriodDays` (default 30).")

    red = Redactor(opts.redaction_level, home_dirs=derive_homes(sessions, source))
    if opts.include_app_state:
        # state that needs the final homes for path display
        bundle["history"] = bundle["history"]
    if opts.include_logs:
        progress(0.84, "Reading logs…")
        if bundle.get("live_source"):
            bundle["mcp_logs"] = stores.read_mcp_logs(paths.cli_cache_dirs(), limit=opts.log_limit)
        else:
            bundle["mcp_logs"] = []
            log("MCP cache logs skipped: they live outside ~/.claude and belong to this machine, not the source image.", "info")
        bundle["debug_tail"] = stores.read_debug_tail(source, red, limit=opts.log_limit)
    if opts.include_flight:
        from . import flight
        fr = flight.compute_flight(sessions, desktop=bundle.get("desktop"), global_info=bundle.get("global"))
        bundle["flight_md"] = flight.render_markdown(fr, red)

    total_bytes = sum(s.size_bytes for s in sessions)
    embed = opts.include_transcripts and (not opts.per_sessions or total_bytes <= report.EMBED_LIMIT_BYTES)
    if opts.include_transcripts and not embed:
        log(f"{total_bytes / 1e6:,.0f} MB of transcripts: combined report will link to per-session files instead of embedding.", "warn")

    progress(0.86, "Rendering report…")
    ordered = report.order_sessions(sessions)
    lookup = {s.session_id: s for s in sessions}
    per_paths: list[Path] = []
    session_files: dict[int, str] = {}
    sess_dir = output.parent / f"{output.stem}_sessions"
    if opts.per_sessions and opts.include_transcripts:
        for i, s in enumerate(ordered, 1):
            if s.is_subagent and (not opts.include_subagents or opts.flatten_subagents):
                continue
            session_files[id(s)] = f"{sess_dir.name}/{report.session_filename(i, s, red)}"

    text = report.build_report(bundle, red, opts, version=__version__, session_files=session_files or None, embed=embed)
    progress(0.93, "Writing report…")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    if session_files:
        sess_dir.mkdir(parents=True, exist_ok=True)
        numbering = {id(s): i for i, s in enumerate(ordered, 1)}
        for n_done, s in enumerate(ordered, 1):
            if id(s) not in session_files:
                continue
            path = sess_dir / Path(session_files[id(s)]).name
            path.write_text(report.render_session_file(s, red, opts, numbering[id(s)], lookup, output.name, __version__),
                            encoding="utf-8")
            per_paths.append(path)
            if n_done % 25 == 0:
                progress(0.93 + 0.06 * n_done / len(ordered), "Writing session files…")
    progress(1.0, "Done")

    mains = [s for s in sessions if not s.is_subagent]
    stats = {
        "total_sessions": len(mains), "archived": sum(1 for s in mains if s.kind in ("orphaned", "superseded")),
        "subagents": len(sessions) - len(mains), "parents": sum(1 for s in mains if s.child_ids),
        "total_tokens": sum(s.usage.total for s in report.counted(sessions)), "turns": sum(len(s.turns) for s in sessions),
        "tool_calls": sum(s.tool_count for s in sessions), "projects": len({report.project_key(s) for s in sessions}),
        "corrupt_lines": sum(s.corrupt_lines for s in sessions), "index_only": len(bundle.get("index_only", [])),
        "desktop_sessions": sum(1 for s in sessions if s.kind.startswith("cowork")),
        "redactions": red.summary(), "report_bytes": len(text.encode("utf-8")),
    }
    return RecoveryResult(output_path=output, per_session_paths=per_paths, stats=stats,
                          warnings=bundle["warnings"], sessions=sessions)
