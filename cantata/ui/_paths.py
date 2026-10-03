"""Launch-relative output locations.

Every report the app writes lands inside the folder it lives in:
  Archive/   - recovery + Chat & Cowork reports (past work)
  Streamed/  - Live-mode JSONL streams + session reports (gated feature)
Always relative to the package root, never the CWD, so launching from a shortcut still works.
"""

from __future__ import annotations

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent.parent


def archive_dir() -> Path:
    d = APP_ROOT / "Archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def streamed_dir() -> Path:
    d = APP_ROOT / "Streamed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_report(name: str) -> Path:
    return archive_dir() / name


def streamed_report(name: str) -> Path:
    return streamed_dir() / name
