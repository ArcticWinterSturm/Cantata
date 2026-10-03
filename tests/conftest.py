"""Shared fixtures: synthetic ~/.claude, Desktop tree, Chromium profile; isolated CANTATA_HOME; offscreen Qt."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from make_fixture import build_fixture                # noqa: E402
from make_webchat_fixture import build_webchat_fixture  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.cantata state or inherit the experimental env switch."""
    monkeypatch.setenv("CANTATA_HOME", str(tmp_path / "cantata_home"))
    monkeypatch.delenv("CANTATA_EXPERIMENTAL", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(scope="session")
def fx(tmp_path_factory):
    root = tmp_path_factory.mktemp("claude_home")
    info = build_fixture(root)
    info["claude_path"] = Path(info["claude"])
    return info


@pytest.fixture(scope="session")
def wc(tmp_path_factory):
    return build_webchat_fixture(tmp_path_factory.mktemp("webchat"))


def all_text(paths) -> str:
    return "".join(Path(p).read_text(encoding="utf-8", errors="replace") for p in paths)
