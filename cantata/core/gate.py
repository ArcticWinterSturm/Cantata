"""The deliberately *weak* experimental gate.

Time-in-flight analytics and the Live modes are built but switched off. This is a
speed bump, **not security**: a flag in a JSON file, an environment variable, or a
``--experimental`` CLI switch opens it. The GUI shows a lock overlay until the user
unlocks it (Settings -> Experimental, or the Unlock button on the locked page).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENV_VAR = "CANTATA_EXPERIMENTAL"
STATE_KEY = "experimental_unlocked"
_TRUTHY = {"1", "true", "yes", "on", "unlock", "unlocked"}

LOCK_TITLE = "Time in flight & Live modes are locked"
LOCK_BODY = (
    "These features are built but switched off for now. They measure how long requests "
    "spend in flight (time to first token, tool wait, turn duration) and can tail Claude "
    "sessions in real time — including a loopback telemetry receiver. They only ever read "
    "local files and listen on 127.0.0.1, but they are still experimental.\n\n"
    "This lock is a speed bump, not a security control."
)


def state_dir() -> Path:
    env = os.environ.get("CANTATA_HOME", "").strip()
    return Path(env) if env else Path.home() / ".cantata"


def state_file() -> Path:
    return state_dir() / "state.json"


def read_state() -> dict[str, Any]:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        state_file().write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # a read-only home must never break the app


def is_unlocked() -> bool:
    if os.environ.get(ENV_VAR, "").strip().lower() in _TRUTHY:
        return True
    return bool(read_state().get(STATE_KEY, False))


def set_unlocked(flag: bool) -> None:
    state = read_state()
    state[STATE_KEY] = bool(flag)
    write_state(state)


def get_setting(key: str, default: Any = None) -> Any:
    return read_state().get("ui", {}).get(key, default)


def set_setting(key: str, value: Any) -> None:
    state = read_state()
    state.setdefault("ui", {})[key] = value
    write_state(state)
