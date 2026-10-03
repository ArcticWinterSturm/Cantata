#!/usr/bin/env python3
"""Cantata launcher.

    python run.py                    # GUI (PyQt6)
    python run.py cli recover ...    # headless CLI (pure stdlib - no PyQt needed)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_cli(argv: list[str]) -> int:
    from cantata.cli import main
    return main(argv)


def run_gui(argv: list[str]) -> int:
    try:
        from PyQt6.QtCore import QDir, QLockFile
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("PyQt6 is not installed. Run install.bat (Windows) or `pip install -r requirements.txt`,\n"
              "or use the headless CLI:  python run.py cli --help", file=sys.stderr)
        return 1
    from cantata import APP_NAME
    from cantata.core import gate
    from cantata.ui import theme
    from cantata.ui.main_window import MainWindow

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    theme.apply(app, gate.get_setting("accent", theme.DEFAULT_ACCENT))

    lock = None
    if "--multi" not in argv:
        import getpass
        try:
            user = getpass.getuser()
        except Exception:                                   # no passwd entry (containers)
            user = str(os.getuid()) if hasattr(os, "getuid") else "user"
        lock = QLockFile(QDir.temp().absoluteFilePath(f"cantata-{user}.lock"))      # per-user: two accounts must not block each other
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running.")
            return 0
    initial = ""
    for i, a in enumerate(argv):
        if a == "--source" and i + 1 < len(argv):
            initial = argv[i + 1]
    window = MainWindow(initial)
    window.show()
    code = app.exec()
    if lock is not None:
        lock.unlock()
    return code


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "cli":
        return run_cli(argv[1:])
    return run_gui([sys.argv[0]] + argv)


if __name__ == "__main__":
    raise SystemExit(main())
