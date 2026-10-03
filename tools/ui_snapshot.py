"""Render the GUI offscreen to PNG - for the docs and as a visual smoke test.

    QT_QPA_PLATFORM=offscreen python tools/ui_snapshot.py OUT_DIR [--source ~/.claude]    # empty pages
    QT_QPA_PLATFORM=offscreen python tools/ui_snapshot.py OUT_DIR --demo                   # drive the real GUI on synthetic data

``--demo`` builds the synthetic fixtures (fake Claude Code home, Desktop tree, Chromium profile, export zip) in a temp dir,
then clicks through Recover, Chat & Cowork, Flight (unlocked) and a short LIVE capture so the screenshots show populated pages.
It never touches your real ~/.claude or ~/.cantata.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if "CANTATA_HOME" not in os.environ:                    # the snapshot toggles the gate - never touch the real ~/.cantata
    os.environ["CANTATA_HOME"] = tempfile.mkdtemp(prefix="cantata_snap_")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))


def main() -> int:
    args = sys.argv[1:]
    out = Path(args[0] if args and not args[0].startswith("--") else "snapshots")
    demo = "--demo" in args
    source = args[args.index("--source") + 1] if "--source" in args else ""
    out.mkdir(parents=True, exist_ok=True)

    from PyQt6.QtWidgets import QApplication
    from cantata.core import paths
    from cantata.ui import theme
    from cantata.ui.main_window import MainWindow

    work = Path(tempfile.mkdtemp(prefix="cantata_demo_"))
    fx = wc = None
    if demo:
        from make_fixture import build_fixture
        from make_webchat_fixture import build_webchat_fixture
        fx, wc = build_fixture(work / "home"), build_webchat_fixture(work / "wc")
        source = fx["claude"]
        paths.desktop_roots = lambda extra=(): [Path(wc["desktop_root"])]            # type: ignore[assignment]
        paths.browser_user_data_dirs = lambda: [("Chromium", Path(wc["browser_root"]))]   # type: ignore[assignment]

    app = QApplication(sys.argv[:1])
    theme.apply(app)
    win = MainWindow(source)
    win.resize(1260, 860)
    win.show()

    def pump(seconds: float = 0.4, until=None, timeout: float = 30.0) -> None:
        end = time.monotonic() + (timeout if until else seconds)
        while time.monotonic() < end:
            app.processEvents()
            if until and until():
                break
            time.sleep(0.01)
        app.processEvents()

    def shot(name: str) -> None:
        pump(0.25)
        win.grab().save(str(out / f"{name}.png"))
        print("saved", out / f"{name}.png")

    if not demo:
        for i, name in enumerate(["archive", "recover", "chatcowork", "flight_locked", "report", "settings"]):
            win.show_page(i)
            pump(0.9 if i == 0 else 0.25)
            shot(name)
        win.set_gate(True)
        win.show_page(3)
        shot("flight_unlocked")
        win.set_gate(False)
        win.close()
        return 0

    # ------------------------------------------------------------------ demo: archive
    win.show_page(0)
    win.dashboard.scan(source)
    pump(until=lambda: win.dashboard.cards["sessions"].value_label.text() != "—")
    shot("archive")

    # recover
    win.show_page(1)
    win.recover.desktop_row.setText(wc["desktop_root"])
    win.recover.output_row.setText(str(work / "out" / "Cantata_recovery.md"))
    win.settings.auto_open.setChecked(False)
    win.recover.start_btn.click()
    pump(until=lambda: win.worker is None and (work / "out" / "Cantata_recovery.md").exists())
    win.recover.console.verticalScrollBar().setValue(win.recover.console.verticalScrollBar().maximum())
    win.recover.findChild(type(win.recover.layout().itemAt(0).widget()))  # keep scroll area alive
    scroll = win.recover.layout().itemAt(0).widget()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    shot("recover")

    # report page
    win.report.load(str(work / "out" / "Cantata_recovery.md"))
    pump(until=lambda: win.report.preview.toPlainText().startswith("#"))
    win.show_page(4)
    shot("report")

    # chat & cowork
    cc = win.chatcowork
    win.show_page(2)
    cc.add_export(wc["zip"])
    cc.add_export(wc["single"])
    cc.extract_row.setText(str(work / "out" / "Cantata_chats"))
    cc.output_row.setText(str(work / "out" / "Cantata_chatcowork.md"))
    cc.start_btn.click()
    pump(until=lambda: cc.open_btn.isEnabled() and win._chat_worker is None)
    sc = cc.layout().itemAt(0).widget()
    sc.verticalScrollBar().setValue(sc.verticalScrollBar().maximum())
    shot("chatcowork")

    # flight: locked, then unlocked + analysed + a short LIVE capture
    win.show_page(3)
    shot("flight_locked")
    win.set_gate(True)
    fl = win.flight
    fl.source_row.setText(source)
    fl.analyze_btn.click()
    pump(until=lambda: fl.table.rowCount() >= 3)
    cfg = work / "livecfg" / ".claude"
    tail = cfg / "projects" / "-demo" / "live.jsonl"
    tail.parent.mkdir(parents=True)
    tail.write_text("", encoding="utf-8")
    fl.out_row.setText(str(work / "streams"))
    fl.port.setValue(4318 + (os.getpid() % 500))
    # use the live capture against a scratch config dir so the demo is self-contained
    fl.source_row.setText(str(cfg))
    fl.live_btn.click()
    pump(0.6)
    t = [f"2026-01-01T00:00:{s:02d}.{ms:03d}Z" for s, ms in ((0, 0), (2, 380), (3, 100), (5, 120), (9, 0), (11, 250))]
    lines = [
        {"type": "user", "timestamp": t[0], "sessionId": "demo-session", "cwd": "/work/demo", "message": {"role": "user", "content": "refactor the parser"}},
        {"type": "assistant", "timestamp": t[1], "sessionId": "demo-session", "message": {"id": "msg_a", "model": "claude-opus-4-5-20251101", "role": "assistant",
         "content": [{"type": "thinking", "thinking": "Plan the refactor"}], "usage": {"input_tokens": 120, "output_tokens": 340, "cache_read_input_tokens": 9000}}},
        {"type": "assistant", "timestamp": t[2], "sessionId": "demo-session", "message": {"id": "msg_a", "model": "claude-opus-4-5-20251101", "role": "assistant",
         "stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q"}}],
         "usage": {"input_tokens": 120, "output_tokens": 340, "cache_read_input_tokens": 9000}}},
        {"type": "user", "timestamp": t[3], "sessionId": "demo-session", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": t[4], "sessionId": "demo-session", "message": {"id": "msg_b", "model": "claude-opus-4-5-20251101", "role": "assistant",
         "stop_reason": "end_turn", "content": [{"type": "text", "text": "Done"}], "usage": {"input_tokens": 30, "output_tokens": 80, "cache_read_input_tokens": 9500}}},
        {"type": "system", "subtype": "turn_duration", "timestamp": t[5], "sessionId": "demo-session", "durationMs": 11250},
    ]
    with open(tail, "a", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")
    # an OTLP span with exact TTFT, posted over real loopback HTTP
    import urllib.request
    port = fl.port.value()
    pump(0.5, until=lambda: fl.worker is not None and fl.worker.engine is not None and fl.worker.engine.receiver is not None)
    span = {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [{"name": "claude_code.llm_request", "startTimeUnixNano": "1000000000",
            "endTimeUnixNano": "4200000000", "attributes": [{"key": "ttft_ms", "value": {"intValue": "812"}}, {"key": "duration_ms", "value": {"intValue": "3200"}},
            {"key": "model", "value": {"stringValue": "claude-opus-4-5"}}, {"key": "session.id", "value": {"stringValue": "demo-session"}}]}]}]}]}
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/traces", data=json.dumps(span).encode(),
                                                      headers={"Content-Type": "application/json"}), timeout=3)
    except OSError as exc:
        print("otlp post failed:", exc)
    pump(1.6)
    shot("flight_live")
    fl.stop_btn.click()
    pump(until=lambda: fl.worker is None)
    fl.source_row.setText(source)
    fl.analyze_btn.click()
    pump(until=lambda: fl.f_cards["n"].value_label.text() != "—")
    sc = fl.layout().itemAt(0).widget()
    sc.verticalScrollBar().setValue(0)
    shot("flight_unlocked")

    win.show_page(5)
    win.set_gate(False)
    shot("settings")
    win.close()
    # keep a copy of the generated reports for the docs
    for name in ("Cantata_recovery.md", "Cantata_chatcowork.md"):
        src = work / "out" / name
        if src.exists():
            (out / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
