"""Headless CLI for Cantata.

    python run.py cli recover  -s ~/.claude -o report.md        # full Markdown report (+ per-session files)
    python run.py cli audit    --json                            # token / tool / stop-reason audit
    python run.py cli chatcowork -e ~/Downloads/claude-export.zip --extract chats/
    python run.py cli gate     --status | --unlock | --lock     # the (weak) experimental gate
    python run.py cli flight   --experimental                    # time-in-flight analytics   [gated]
    python run.py cli capture  --experimental --otlp-port 4318   # live capture / TTFT tracker [gated]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from . import APP_NAME, __version__
from .core import gate, paths, scanner
from .core.models import RecoveryCancelled, RecoveryOptions
from .core.recovery import run_recovery

COMMANDS = ("recover", "audit", "chatcowork", "chat", "webchat", "chatwork", "flight", "capture", "gate")


def _bar(frac: float, width: int = 36) -> str:
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def _make_log(quiet: bool):
    def log(msg: str, level: str = "info") -> None:
        if quiet and level == "info":
            return
        prefix = {"warn": "!", "error": "x", "ok": "+"}.get(level, "*")
        print(f"[{time.strftime('%H:%M:%S')}] {prefix} {msg}")
    return log


def _make_progress(quiet: bool = False):
    if quiet:
        return lambda frac, label="": None
    last = [-1]

    def progress(frac: float, label: str = "") -> None:
        pct = int(frac * 100)
        if pct == last[0]:
            return
        last[0] = pct
        width = shutil.get_terminal_size((80, 24)).columns
        sys.stdout.write(f"\r[{_bar(frac, max(width - 30, 10))}] {pct:3d}%  {label[:24]:24s}")
        sys.stdout.flush()
    return progress


def _require_gate(args: argparse.Namespace, what: str) -> bool:
    if getattr(args, "experimental", False) or gate.is_unlocked():
        return True
    print(f"'{what}' is locked.\n{gate.LOCK_BODY}\n\n"
          f"Unlock with:  python run.py cli gate --unlock     (or pass --experimental, or set {gate.ENV_VAR}=1)", file=sys.stderr)
    return False


# --------------------------------------------------------------------------- #
def _cmd_recover(args: argparse.Namespace) -> int:
    source = args.source or scanner.detect_default_source()
    output = args.output or Path.cwd() / "Cantata_recovery.md"
    log = _make_log(args.quiet)

    if args.scan_only:
        s = scanner.quick_stats(source)
        print(f"Source              : {source}")
        print(f"Looks like Claude   : {s['looks_like_claude']}")
        print(f"Projects            : {s['projects']}")
        print(f"Transcripts (active): {s['active_transcripts']}  (+{s['orphaned_transcripts']} orphaned/superseded, {s['subagents']} subagent)")
        print(f"History prompts     : {s['history_entries']:,}")
        print(f"Tokens (stats-cache): {s['tokens_total']:,}   sessions reported: {s['sessions_reported']}")
        print(f"Data size           : {s['size_mb']} MB ({s['transcript_mb']} MB transcripts)")
        print(f"Date range          : {s['date_range'][0] or '-'} -> {s['date_range'][1] or '-'}")
        print(f"Retention           : {s['retention_effective']} days   oldest {s['oldest_age_days']} d   expiring ≤7d: {s['expiring_soon']}")
        if s["app_version"]:
            print(f"Claude Code version : {s['app_version']}  (seen: {', '.join(s['versions'][:5])})")
        for note in s["issues"]:
            print(f"  - {note}")
        return 0

    include_flight = False
    if args.flight:
        if not _require_gate(args, "recover --flight"):
            return 3
        include_flight = True
    options = RecoveryOptions(
        include_transcripts=not args.no_transcripts, include_app_state=not args.no_app_state, include_tokens=not args.no_tokens,
        include_tools=not args.no_tools, include_thinking=not args.no_thinking, include_logs=not args.no_logs,
        include_desktop=not args.no_desktop, include_orphaned=not args.no_orphaned, log_limit=args.log_limit,
        redaction_level=args.level, per_sessions=not args.no_per_sessions, include_subagents=not args.no_subagents,
        flatten_subagents=args.flatten_subagents, include_flight=include_flight,
        desktop_dirs=tuple(args.desktop_dir or ()))
    try:
        result = run_recovery(source, output, options, log=log, progress=_make_progress(args.quiet))
    except RecoveryCancelled:
        print("\ncancelled")
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        print()
    st = result.stats
    log(f"Sessions recovered : {st['total_sessions']} ({st['archived']} orphaned/superseded copies)", "ok")
    log(f"Subagents          : {st['subagents']} under {st['parents']} parents")
    log(f"Index-only sessions: {st['index_only']} (remembered, transcript gone)")
    log(f"Tokens             : {st['total_tokens']:,}")
    log(f"Redactions         : {sum(st['redactions'].values()):,} ({', '.join(f'{k}×{v}' for k, v in sorted(st['redactions'].items())[:6])})")
    log(f"Report             : {result.output_path}", "ok")
    if result.per_session_paths:
        log(f"Per-session files  : {len(result.per_session_paths)} in {Path(output).stem}_sessions/")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    from .core.audit import run_audit
    print(run_audit(args.source or scanner.detect_default_source(), output_json=args.json, include_desktop=args.desktop,
                    redaction=args.level))
    return 0


def _cmd_chatcowork(args: argparse.Namespace) -> int:
    from .core.redaction import Redactor
    from .core.webchat import run_webchat, extract_indexeddb_conversations, write_indexeddb_conversations
    from .core.webchat_report import build_webchat_report
    source = args.source or scanner.detect_default_source()
    output = args.output or Path.cwd() / "Cantata_chatcowork.md"
    log = _make_log(args.quiet)
    try:
        rep = run_webchat(source, [Path(p) for p in (args.exports or [])], desktop_dirs=[Path(p) for p in (args.desktop_dir or [])] or None,
                          scan_browser=not args.no_browser, scan_transcripts=not args.no_transcripts,
                          extract_dir=Path(args.extract) if args.extract else None, redaction_level=args.level,
                          include_thinking=not args.no_thinking, log=log, progress=_make_progress(args.quiet))
    finally:
        print()
    red = Redactor(args.level, home_dirs=[Path.home()])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_webchat_report(rep, red, version=__version__), encoding="utf-8")

    # Full Claude Desktop IndexedDB conversation extraction
    if getattr(args, "idb_out", None):
        try:
            convs = extract_indexeddb_conversations(log=log)
            if convs:
                written = write_indexeddb_conversations(convs, Path(args.idb_out), redact=red.apply)
                log(f"IndexedDB extract : {len(convs)} conversation(s) -> {args.idb_out}", "ok")
        except Exception as e:
            log(f"IndexedDB extract failed: {e}", "warn")

    st = rep.stats()
    log(f"Threads         : {st['threads']} ({st['recoverable']} recoverable, {st['server_only']} server-side only)", "ok")
    log(f"Cowork / Code   : {st['cowork']} / {st['code_tab']}")
    log(f"Browser         : {st['browsers']} profile(s), {st['browser_visits']} history visits")
    log(f"Exports triaged : {st['exports']} ({st['export_messages']:,} messages)")
    if st.get("visited"):
        log(f"Coverage        : {st['visited_recoverable']} of {st['visited']} conversations opened in the browser "
            f"have a local body"
            + (f" ({st['visited_missing']} server-side only)" if st["visited_missing"] else ""),
            "warn" if st["visited_missing"] else "ok")
    for label, _path, n_rec, n_body in rep.idb_sources:
        if n_rec == 0:
            log(f"IndexedDB cache : {label} holds no conversation records (signed-out / account-switched session)", "warn")
    log(f"Report          : {output}", "ok")
    return 0


def _cmd_flight(args: argparse.Namespace) -> int:
    if not _require_gate(args, "flight"):
        return 3
    from .core import flight, recovery
    from .core.redaction import Redactor
    source = args.source or scanner.detect_default_source()
    bundle = recovery.collect_bundle(Path(source), RecoveryOptions(include_desktop=not args.no_desktop, include_logs=False),
                                     _make_log(True), _make_progress(args.quiet or args.json), lambda: False)
    fr = flight.compute_flight(bundle["sessions"], desktop=bundle.get("desktop"), global_info=bundle.get("global"),
                               otel_dir=Path(args.otel_dir) if args.otel_dir else None)
    red = Redactor(args.level, home_dirs=recovery.derive_homes(bundle["sessions"], Path(source)))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(flight.render_markdown(fr, red) + "\n", encoding="utf-8")
        print(f"Report: {args.output}")
    print(flight.to_json(fr) if args.json else flight.format_text(fr, red))
    return 0


def _cmd_capture(args: argparse.Namespace) -> int:
    if not _require_gate(args, "capture"):
        return 3
    from .core import capture
    source = Path(args.source or scanner.detect_default_source())
    if args.print_env:
        print(capture.env_snippet(args.otlp_port or 4318, args.shell))
        return 0
    out = Path(args.out) if args.out else Path.cwd() / "Streamed"
    capture.run_capture_daemon(source, out, live=not args.audit, otlp_port=args.otlp_port, duration=args.duration)
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    if args.unlock:
        gate.set_unlocked(True)
    elif args.lock:
        gate.set_unlocked(False)
    state = "UNLOCKED" if gate.is_unlocked() else "locked"
    print(f"Experimental gate (time in flight + Live modes): {state}")
    print(f"  state file: {gate.state_file()}")
    if not (args.unlock or args.lock):
        print(f"  unlock: python run.py cli gate --unlock   |   env {gate.ENV_VAR}=1   |   --experimental on a gated command")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cantata", description=f"{APP_NAME} - local-only archive and forensic recovery for Claude.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    def common(p, *, output_default="", src=True):
        if src:
            p.add_argument("--source", "-s", type=Path, default=None, help="Claude Code data dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
        p.add_argument("--level", "-l", choices=RecoveryOptions.LEVELS, default="standard", help="redaction level (default: standard)")
        p.add_argument("--quiet", "-q", action="store_true", help="only warnings and errors")

    rec = sub.add_parser("recover", help="Generate the Markdown recovery report")
    common(rec)
    rec.add_argument("--output", "-o", type=Path, default=None, help="output .md (default ./Cantata_recovery.md)")
    for flag, text in (("transcripts", "conversation transcripts"), ("app-state", "application state section"), ("tokens", "token tables"),
                       ("tools", "tool call details"), ("thinking", "thinking blocks"), ("logs", "debug / MCP logs"),
                       ("desktop", "Claude Desktop (Cowork / Code-tab)"), ("orphaned", ".orphaned / .superseded transcript copies"),
                       ("per-sessions", "individual per-session .md files"), ("subagents", "subagent sessions")):
        rec.add_argument(f"--no-{flag}", action="store_true", help=f"exclude {text}")
    rec.add_argument("--flatten-subagents", action="store_true", help="show subagent turns inline in the parent")
    rec.add_argument("--desktop-dir", type=Path, action="append", help="extra/explicit Claude Desktop data dir (repeatable)")
    rec.add_argument("--log-limit", type=int, default=1000)
    rec.add_argument("--scan-only", action="store_true", help="inventory only, no report")
    rec.add_argument("--flight", action="store_true", help="[gated] add the time-in-flight section")
    rec.add_argument("--experimental", action="store_true", help="open the experimental gate for this run")
    rec.set_defaults(func=_cmd_recover)

    aud = sub.add_parser("audit", help="Token / tool / stop-reason audit of the transcripts")
    common(aud)
    aud.add_argument("--json", action="store_true")
    aud.add_argument("--desktop", action="store_true", help="include Cowork transcripts")
    aud.set_defaults(func=_cmd_audit)

    cc = sub.add_parser("chatcowork", aliases=["chat", "webchat", "chatwork"], help="Claude.ai / Desktop footprint + export reconstruction")
    common(cc)
    cc.add_argument("--output", "-o", type=Path, default=None)
    cc.add_argument("--exports", "-e", type=Path, nargs="*", default=None, help="export archives (.zip/.json/.md)")
    cc.add_argument("--extract", type=Path, default=None, help="write redacted per-conversation Markdown from exports here")
    cc.add_argument("--desktop-dir", type=Path, action="append")
    cc.add_argument("--idb-out", type=Path, default=None,
                    help="extract FULL Claude Desktop IndexedDB conversations as Markdown into this dir")
    cc.add_argument("--no-browser", action="store_true")
    cc.add_argument("--no-transcripts", action="store_true")
    cc.add_argument("--no-thinking", action="store_true")
    cc.set_defaults(func=_cmd_chatcowork)

    fl = sub.add_parser("flight", help="[gated] time-in-flight analytics from the transcripts")
    common(fl)
    fl.add_argument("--json", action="store_true")
    fl.add_argument("--output", "-o", type=Path, default=None, help="also write a Markdown report")
    fl.add_argument("--otel-dir", type=Path, default=None, help="Live-mode output dir holding flight-*.jsonl (exact TTFT)")
    fl.add_argument("--no-desktop", action="store_true")
    fl.add_argument("--experimental", action="store_true")
    fl.set_defaults(func=_cmd_flight)

    cap = sub.add_parser("capture", help="[gated] live capture daemon (transcript tail + OTLP receiver)")
    common(cap)
    cap.add_argument("--out", type=Path, default=None, help="stream output dir (default ./Streamed)")
    cap.add_argument("--audit", action="store_true", help="AUDIT mode: dry run, write nothing")
    cap.add_argument("--otlp-port", type=int, default=None, help="also listen for Claude Code OTLP/HTTP-JSON on 127.0.0.1:PORT")
    cap.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    cap.add_argument("--print-env", action="store_true", help="print the env that points Claude Code at the receiver")
    cap.add_argument("--shell", choices=("bash", "powershell", "cmd"), default="bash")
    cap.add_argument("--experimental", action="store_true")
    cap.set_defaults(func=_cmd_capture)

    g = sub.add_parser("gate", help="show / change the experimental gate")
    g.add_argument("--unlock", action="store_true")
    g.add_argument("--lock", action="store_true")
    g.add_argument("--status", action="store_true")
    g.set_defaults(func=_cmd_gate)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "recover")                 # bare flags default to `recover`
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
