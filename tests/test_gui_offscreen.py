"""GUI smoke + behaviour tests on Qt's offscreen platform (no display needed)."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt                                          # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox                # noqa: E402

from cantata.core import gate, paths                                 # noqa: E402
from cantata.ui import theme                                         # noqa: E402
from cantata.ui.main_window import (                                 # noqa: E402
    ARCHIVE, CHATCOWORK, FLIGHT, PAGES, RECOVER, REPORT, MainWindow,
)


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.apply(a)
    return a


@pytest.fixture(autouse=True)
def no_real_machine_scans(monkeypatch, tmp_path):
    """The GUI must not depend on (or read) the developer's real Desktop / browser data."""
    monkeypatch.setattr(paths, "desktop_roots", lambda extra=(): [Path(p) for p in extra])
    monkeypatch.setattr(paths, "browser_user_data_dirs", lambda: [])
    monkeypatch.setattr(paths, "cli_cache_dirs", lambda: [])
    monkeypatch.setattr("cantata.ui._paths.APP_ROOT", tmp_path / "app_root")          # Archive/ + Streamed/ must not appear in the repo
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))


@pytest.fixture()
def win(app, fx):
    w = MainWindow(str(fx["claude_path"]))
    w.show()
    app.processEvents()
    yield w
    w.flight.shutdown()
    w.close()
    app.processEvents()


def wait_until(app, cond, timeout=25.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(step)
    app.processEvents()
    return bool(cond())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- structure


def test_window_builds_every_page(win, app):
    assert win.stack.count() == len(PAGES) == 7 and len(win.nav_buttons) == len(PAGES)
    for i in range(len(PAGES)):
        win.show_page(i)
        app.processEvents()
        assert win.stack.currentIndex() == i and win.nav_buttons[i].isChecked()
        assert sum(b.isChecked() for b in win.nav_buttons) == 1


def test_sidebar_ampersand_is_escaped(win):
    assert "&&" in win.nav_buttons[CHATCOWORK].text()                      # 'Chat && Cowork' renders as 'Chat & Cowork' (no mnemonic underscore)
    assert "&&" in win.nav_buttons[FLIGHT].text()
    assert not any(ord(ch) > 0x2000 for b in win.nav_buttons for ch in b.text())     # no emoji: they render as tofu on many systems


def test_stylesheet_has_no_unreplaced_placeholders():
    for name in theme.ACCENTS:
        qss = theme.build_qss(name)
        assert "$" not in qss and theme.ACCENTS[name] in qss


def test_source_propagates_to_pages(win, fx):
    for row in (win.recover.source_row, win.dashboard.source_row, win.chatcowork.source_row, win.flight.source_row):
        assert row.text() == str(fx["claude_path"])


# --------------------------------------------------------------------------- the soft gate


def test_flight_and_live_are_locked_by_default(win):
    assert not gate.is_unlocked()
    assert win.flight.locked and win.flight.overlay.isVisibleTo(win.flight) and not win.flight.content.isEnabled()
    assert win.nav_buttons[FLIGHT].property("locked") == "true"
    assert not win.recover.chk_flight.isEnabled() and "locked" in win.recover.chk_flight.text()
    assert not win.settings.gate_box.isChecked()
    assert not win.recover.collect_options().include_flight
    assert win.flight.worker is None


def test_locked_page_cannot_start_capture(win):
    win.flight._start(True)
    assert win.flight.worker is None


def test_unlock_then_relock(win, app):
    win.flight.overlay.unlocked.emit()                            # what the overlay's confirmed Unlock button does
    app.processEvents()
    assert gate.is_unlocked() and not win.flight.locked and win.flight.content.isEnabled()
    assert win.nav_buttons[FLIGHT].property("locked") == "false" and win.recover.chk_flight.isEnabled() and win.settings.gate_box.isChecked()
    win.recover.chk_flight.setChecked(True)
    assert win.recover.collect_options().include_flight
    win.settings.gate_box.setChecked(False)                       # Settings → Experimental → off
    app.processEvents()
    assert not gate.is_unlocked() and win.flight.locked and not win.recover.chk_flight.isChecked()
    assert not win.recover.collect_options().include_flight


def test_overlay_asks_for_confirmation(win, app, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    win.flight.overlay.unlock_btn.click()
    assert win.flight.locked and not gate.is_unlocked()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    win.flight.overlay.unlock_btn.click()
    app.processEvents()
    assert not win.flight.locked and gate.is_unlocked()


def test_env_var_opens_the_gate(app, fx, monkeypatch):
    monkeypatch.setenv(gate.ENV_VAR, "1")
    w = MainWindow(str(fx["claude_path"]))
    try:
        assert not w.flight.locked and w.recover.chk_flight.isEnabled()
    finally:
        w.close()


def test_lock_card_text_is_not_clipped(win, app):
    """Regression: the card under-reported its height and the last sentence overlapped the button."""
    win.show_page(FLIGHT)
    app.processEvents()
    ov = win.flight.overlay
    for label, text in ov._texts:
        need = label.fontMetrics().boundingRect(0, 0, label.width(), 6000, Qt.TextFlag.TextWordWrap, text).height()
        assert label.height() >= need, (text[:30], label.height(), need)
    card_bottom = ov._card.geometry().bottom()
    assert ov.unlock_btn.geometry().bottom() + ov._card.layout().contentsMargins().bottom() <= card_bottom + 2


def test_overlay_covers_the_page(win, app):
    win.show_page(FLIGHT)
    app.processEvents()
    assert win.flight.overlay.geometry() == win.flight.rect()


# --------------------------------------------------------------------------- dashboard & recovery


def test_dashboard_scan(win, app):
    win.show_page(ARCHIVE)
    win.dashboard.scan(win.dashboard.source_row.text())
    assert wait_until(app, lambda: win.dashboard.cards["sessions"].value_label.text() == "4")
    assert win.dashboard.cards["orphaned"].value_label.text() == "1" and win.dashboard.cards["retention"].value_label.text() == "30 d"
    assert not win.dashboard.banner.isHidden() and "within a week" in win.dashboard.banner.text()
    assert "2.1.280" in win.dashboard.version_label.text() and win.dashboard.inventory.count() >= 10


def test_full_recovery_through_the_worker(win, app, tmp_path):
    out = tmp_path / "gui_report.md"
    win.recover.output_row.setText(str(out))
    win.recover.chk_desktop.setChecked(False)
    win.show_page(RECOVER)
    win.recover.start_btn.click()
    assert win.worker is not None and not win.recover.start_btn.isEnabled() and win.recover.cancel_btn.isEnabled()
    assert wait_until(app, lambda: win.worker is None and out.exists())
    assert wait_until(app, lambda: win.recover.start_btn.isEnabled())
    text = out.read_text(encoding="utf-8")
    assert "# Claude Local Recovery Report" in text and (tmp_path / "gui_report_sessions").is_dir()
    assert "Recovery complete" in win.recover.console.toPlainText()
    assert wait_until(app, lambda: win.report.preview.toPlainText().startswith("# Claude Local Recovery Report"))
    assert win.stack.currentIndex() == REPORT and win.recover.progress.bar.value() == 1000   # auto-open → Report page


def test_recovery_cancel(win, app, tmp_path, monkeypatch):
    from cantata.core import recovery
    real = recovery.collect_bundle

    def slow(*a, **k):
        time.sleep(0.4)
        return real(*a, **k)
    monkeypatch.setattr(recovery, "collect_bundle", slow)
    out = tmp_path / "never.md"
    win.recover.output_row.setText(str(out))
    win.recover.chk_desktop.setChecked(False)
    win.recover.start_btn.click()
    win.recover.cancel_btn.click()
    assert wait_until(app, lambda: win.worker is None)
    assert not out.exists() and "cancel" in win.recover.console.toPlainText().lower()


def test_invalid_source_is_rejected(win, app, tmp_path):
    win.recover.source_row.setText(str(tmp_path / "does-not-exist"))
    win.recover.start_btn.click()
    app.processEvents()
    assert win.worker is None and win.recover.start_btn.isEnabled()


# --------------------------------------------------------------------------- chat & cowork


def test_chatcowork_scan_through_the_worker(win, app, wc, tmp_path):
    cc = win.chatcowork
    cc.desktop_row.setText(wc["desktop_root"])
    cc.add_export(wc["zip"])
    cc.chk_browser.setChecked(False)
    cc.extract_row.setText(str(tmp_path / "chats"))
    cc.output_row.setText(str(tmp_path / "cc.md"))
    win.show_page(CHATCOWORK)
    cc.start_btn.click()
    assert wait_until(app, lambda: cc.open_btn.isEnabled() and win._chat_worker is None)
    assert (tmp_path / "cc.md").exists() and list((tmp_path / "chats").rglob("*.md"))
    assert "threads" in cc.summary.text() and "Report written" in cc.console.toPlainText()
    cc.open_btn.click()
    assert wait_until(app, lambda: win.stack.currentIndex() == REPORT)


def test_chatcowork_worker_rebuilds_indexeddb_conversations(win, app, wc, tmp_path, monkeypatch):
    """Regression: the GUI never called extract_indexeddb_conversations, so 'Extract to'
    wrote zero files for anyone whose only local history was IndexedDB (no exports)."""
    from cantata.core import webchat as wc_mod

    # Assembled at runtime: a literal API-key prefix in this file gets redacted by the
    # tooling that reads it, which would quietly gut the assertion below.
    key = "sk-" + "ant-" + "api03-" + ("A" * 28)
    convs = [{"uuid": "11111111-1111-4111-8111-111111111111", "name": "Rebuilt chat", "model": "claude-sonnet-4-6",
              "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z", "platform": "CLAUDE_AI",
              "thinking_mode": "off", "is_starred": False, "is_archived": False, "settings": {}, "summary": "",
              "messages": [{"index": 0, "sender": "human", "created_at": "2026-01-01T00:00:00Z", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": f"my key is {key}"}]}],
              "stop_reasons": {"end_turn": 1}, "approxBytes": 10, "accountUuid": "", "orgUuid": "", "source": "Desktop"}]

    monkeypatch.setattr(wc_mod, "extract_indexeddb_conversations",
                        lambda log=None, sources=None, progress=None, cancel=None: convs)
    cc = win.chatcowork
    cc.desktop_row.setText(wc["desktop_root"])
    cc.chk_browser.setChecked(False)
    cc.extract_row.setText(str(tmp_path / "chats"))
    cc.output_row.setText(str(tmp_path / "cc.md"))
    win.show_page(CHATCOWORK)
    cc.start_btn.click()
    assert wait_until(app, lambda: cc.open_btn.isEnabled() and win._chat_worker is None)

    built = list((tmp_path / "chats" / "indexeddb").rglob("*.md"))
    assert built, "IndexedDB conversations were not rebuilt into the extract folder"
    body = built[0].read_text(encoding="utf-8")
    assert "Rebuilt chat" in body
    assert key not in body and "[ANTHROPIC-KEY]" in body       # went through the redactor
    assert "IndexedDB" in cc.summary.text() and "rebuilt" in cc.console.toPlainText()


def test_chatcowork_progress_reaches_100_and_opens_the_extract_folder(win, app, wc, tmp_path, monkeypatch):
    """Regression: the worker emitted 1.0 inside run_webchat and then 0.96 for the IndexedDB
    rebuild, so the bar *ended* at 96% while Cancel was already greyed out - it looked stuck."""
    from cantata.core import webchat as wc_mod
    seen: list[float] = []
    orig_update = win.chatcowork.progress.update

    def spy(frac, label=""):
        seen.append(round(frac, 4))
        orig_update(frac, label)

    monkeypatch.setattr(win.chatcowork.progress, "update", spy)
    monkeypatch.setattr(wc_mod, "extract_indexeddb_conversations",
                        lambda log=None, sources=None, progress=None, cancel=None: [
                            {"uuid": "22222222-2222-4222-8222-222222222222", "name": "Cached", "model": "claude-sonnet-4-6",
                             "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z", "messages": [
                                 {"index": 0, "sender": "human", "created_at": "2026-01-01T00:00:00Z", "content": [{"type": "text", "text": "hi"}]}],
                             "stop_reasons": {}, "source": "Brave:Default"}])
    cc = win.chatcowork
    cc.desktop_row.setText(wc["desktop_root"])
    cc.add_export(wc["zip"])
    cc.chk_browser.setChecked(False)
    cc.extract_row.setText(str(tmp_path / "chats"))
    cc.output_row.setText(str(tmp_path / "cc.md"))
    win.show_page(CHATCOWORK)
    cc.start_btn.click()
    assert wait_until(app, lambda: cc.open_btn.isEnabled() and win._chat_worker is None)

    assert seen and seen[-1] == 1.0, f"progress must end at 100%, ended at {seen[-3:]}"
    assert seen == sorted(seen), f"progress must never go backwards, got {seen}"
    assert cc.progress.pct.text() == "100%"
    assert cc.cancel_btn.isEnabled() is False                       # nothing left to cancel
    assert cc.open_dir_btn.isEnabled() and cc._last_indexeddb_dir.endswith("indexeddb")
    dirs = []
    cc.open_folder.connect(dirs.append)
    cc.open_dir_btn.click()
    assert dirs == [cc._last_indexeddb_dir]


# --------------------------------------------------------------------------- flight & live (unlocked)


def test_flight_analysis_populates_the_page(win, app, tmp_path):
    win.set_gate(True)
    win.show_page(FLIGHT)
    win.flight.chk_desktop.setChecked(False)
    win.flight.analyze_btn.click()
    assert wait_until(app, lambda: win.flight.table.rowCount() >= 3)
    assert win.flight.f_cards["n"].value_label.text() == "13" and win.flight.save_btn.isEnabled()
    assert "upper bound" in win.flight.f_status.text()
    models = {win.flight.table.item(r, 0).text() for r in range(win.flight.table.rowCount())}
    assert "claude-opus-4-5-20251101" in models


def test_live_capture_from_the_page(win, app, tmp_path):
    cfg = tmp_path / ".claude"
    f = cfg / "projects" / "-tmp-p" / "sess-1.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("", encoding="utf-8")
    win.set_gate(True)
    fl = win.flight
    fl.source_row.setText(str(cfg))
    fl.out_row.setText(str(tmp_path / "streams"))
    fl.port.setValue(free_port())
    modes = []
    fl.mode_changed.connect(modes.append)
    fl.live_btn.click()
    assert fl.worker is not None and "LIVE" in fl.dot.text() and not fl.live_btn.isEnabled() and fl.stop_btn.isEnabled()
    assert wait_until(app, lambda: fl.worker.engine is not None and fl.worker.engine.receiver is not None)
    time.sleep(0.25)                                                # let the first scan attach at EOF
    t0 = "2026-01-01T00:00:00.000Z"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": t0, "sessionId": "sess-1", "message": {"role": "user", "content": "go"}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:01.500Z", "sessionId": "sess-1", "message": {
            "id": "msg_1", "model": "claude-opus-4-5", "role": "assistant", "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 7}}}) + "\n")
    assert wait_until(app, lambda: fl.l_cards["flight"].value_label.text() not in ("—", "0"), timeout=15)
    assert fl.big_flight.text() == "1.5 s"
    fl.stop_btn.click()
    assert wait_until(app, lambda: fl.worker is None)
    assert "idle" in fl.dot.text() and fl.live_btn.isEnabled() and modes[0] == "live" and modes[-1] == "stopped"
    assert list((tmp_path / "streams").glob("flight-*.jsonl")) and list((tmp_path / "streams").glob("session-*.md"))


def test_audit_mode_from_the_page_writes_nothing(win, app, tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "projects").mkdir(parents=True)
    win.set_gate(True)
    fl = win.flight
    fl.source_row.setText(str(cfg))
    fl.out_row.setText(str(tmp_path / "streams"))
    fl.chk_otlp.setChecked(False)
    fl.audit_btn.click()
    assert wait_until(app, lambda: fl.worker is not None and fl.worker.engine is not None) and "AUDIT" in fl.dot.text()
    fl.stop_btn.click()
    assert wait_until(app, lambda: fl.worker is None)
    assert not (tmp_path / "streams").exists()


def test_relocking_stops_a_running_capture(win, app, tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "projects").mkdir(parents=True)
    win.set_gate(True)
    fl = win.flight
    fl.source_row.setText(str(cfg))
    fl.chk_otlp.setChecked(False)
    fl.audit_btn.click()
    assert wait_until(app, lambda: fl.worker is not None and fl.worker.engine is not None)
    win.set_gate(False)
    assert wait_until(app, lambda: fl.worker is None) and fl.locked


def test_copy_env_buttons(win, app):
    win.set_gate(True)
    win.flight.port.setValue(4455)
    win.flight._copy_env("bash")
    assert "127.0.0.1:4455" in QApplication.clipboard().text()


# --------------------------------------------------------------------------- settings & lifecycle


def test_accent_persists_and_restyles(win, app):
    win.settings.accent_combo.setCurrentText("Mint")
    assert gate.get_setting("accent") == "Mint" and theme.ACCENTS["Mint"] in app.styleSheet()
    win.settings.accent_combo.setCurrentText("Claude Clay")


def test_close_with_running_capture_is_clean(app, fx, tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "projects").mkdir(parents=True)
    gate.set_unlocked(True)
    w = MainWindow(str(fx["claude_path"]))
    w.show()
    w.flight.source_row.setText(str(cfg))
    w.flight.chk_otlp.setChecked(False)
    w.flight.audit_btn.click()
    assert wait_until(app, lambda: w.flight.worker is not None and w.flight.worker.engine is not None)
    w.close()
    assert wait_until(app, lambda: w.flight.worker is None or not w.flight.worker.isRunning())


def test_rapid_open_close_never_aborts(app, fx):
    """Regression: a QThread destroyed while running (Archive scan still in flight) aborts the whole process."""
    import gc
    from cantata.ui.worker import wait_all
    for _ in range(10):
        w = MainWindow(str(fx["claude_path"]))
        w.show()
        app.processEvents()                     # showEvent -> dashboard scan thread starts
        w.close()
        del w
        gc.collect()
        app.processEvents()
    assert wait_all(10_000)


# --------------------------------------------------------------------------- settings wiring & path defaults


def test_settings_output_dir_is_honoured(win, app, tmp_path):
    target = tmp_path / "my_reports"
    win.settings.default_out.setText(str(target))
    assert gate.get_setting("output_dir") == str(target)
    assert str(target) in win.recover.output_row.edit.placeholderText() and str(target) in win.chatcowork.extract_row.edit.placeholderText()
    win.recover.output_row.setText("")                        # empty field -> the configured default
    win.recover.chk_desktop.setChecked(False)
    win.recover.start_btn.click()
    assert wait_until(app, lambda: win.worker is None and (target / "Cantata_recovery.md").exists())
    win.settings.default_out.setText("")                      # cleared -> back to Archive/
    assert "Archive" in win.recover.output_row.edit.placeholderText()


def test_output_dir_setting_is_restored_on_startup(app, fx, tmp_path):
    gate.set_setting("output_dir", str(tmp_path / "saved"))
    w = MainWindow(str(fx["claude_path"]))
    try:
        assert str(tmp_path / "saved") in w.recover.output_row.edit.placeholderText() and w.settings.default_out.text() == str(tmp_path / "saved")
    finally:
        w.close()


def test_chatcowork_extracts_to_the_displayed_default_when_the_field_is_empty(win, app, wc, tmp_path):
    """Regression: an empty 'Extract to' field silently disabled reconstruction although its default path was displayed."""
    win.settings.default_out.setText(str(tmp_path / "out"))
    cc = win.chatcowork
    cc.desktop_row.setText(wc["desktop_root"])
    cc.add_export(wc["zip"])
    cc.chk_browser.setChecked(False)
    assert cc.extract_row.text() == "" and cc.chk_extract.isChecked()
    win.show_page(CHATCOWORK)
    cc.start_btn.click()
    assert wait_until(app, lambda: cc.open_btn.isEnabled() and win._chat_worker is None)
    assert list((tmp_path / "out" / "Cantata_chats").rglob("*.md")) and (tmp_path / "out" / "Cantata_chatcowork.md").exists()


def test_recover_page_passes_the_desktop_dir(win, wc):
    assert win.recover.collect_options().desktop_dirs == ()
    win.recover.desktop_row.setText(wc["desktop_root"])
    assert win.recover.collect_options().desktop_dirs == (wc["desktop_root"],)


def test_dashboard_does_not_show_this_machines_desktop_for_an_image(win, app, wc, monkeypatch):
    monkeypatch.setattr(paths, "desktop_roots", lambda extra=(): [Path(wc["desktop_root"])])
    win.dashboard.scan(win.dashboard.source_row.text())           # fixture dir is not the live Claude dir
    assert wait_until(app, lambda: win.dashboard.cards["sessions"].value_label.text() == "4")
    assert "Not auto-detected" in win.dashboard.desktop_label.text() and "copied" in win.dashboard.desktop_label.text()
