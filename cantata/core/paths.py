"""Where Claude keeps things, per OS - plus the project-folder name encoder.

Every lookup here is *discovery only*: it returns candidates that exist on disk and never
creates, modifies or reads secrets. See docs/CLAUDE_STORAGE.md for the evidence behind each path.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

CLAUDE_IN_CHROME_EXT_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #

def home() -> Path:
    return Path.home()


def claude_config_dir() -> Path:
    """``$CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    return home() / ".claude"


def _same(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def is_live_source(config_dir: Path) -> bool:
    """True when ``config_dir`` is *this machine's* Claude config dir (not a copied forensic image)."""
    return _same(Path(config_dir), claude_config_dir())


def claude_global_json(config_dir: Path) -> Optional[Path]:
    """Locate ``.claude.json`` for a config dir.

    Deliberately does **not** fall back to the analyst's own ``~/.claude.json`` when ``config_dir`` is a copied image -
    that would silently mix another user's account and MCP servers into the report.
    """
    config_dir = Path(config_dir)
    cands = [config_dir / ".claude.json", config_dir.parent / ".claude.json"]
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env and _same(config_dir, Path(os.path.expanduser(env))):
        cands.append(home() / ".claude.json")                  # custom config dir on this machine, legacy location
    for cand in cands:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def managed_settings_candidates() -> list[Path]:
    if sys.platform == "win32":
        bases = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramData", r"C:\ProgramData")]
        out = [Path(b) / "ClaudeCode" / "managed-settings.json" for b in bases]
    elif sys.platform == "darwin":
        out = [Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    else:
        out = [Path("/etc/claude-code/managed-settings.json")]
    return out


def cli_cache_dirs() -> list[Path]:
    """``claude-cli-nodejs`` cache (MCP server logs, errors, messages), per OS."""
    cands: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(Path(local) / "claude-cli-nodejs" / "Cache")
    elif sys.platform == "darwin":
        cands.append(home() / "Library" / "Caches" / "claude-cli-nodejs")
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        cands.append(Path(xdg) / "claude-cli-nodejs" if xdg else home() / ".cache" / "claude-cli-nodejs")
    return [c for c in cands if _isdir(c)]


# --------------------------------------------------------------------------- #
# Claude Desktop
# --------------------------------------------------------------------------- #

def desktop_roots(extra: Iterable[str | Path] = ()) -> list[Path]:
    """Existing Claude Desktop application-data directories (all variants)."""
    cands: list[Path] = [Path(p) for p in extra]
    names = ("Claude", "Claude-3p")
    if sys.platform == "win32":
        roaming, local = os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")
        for base in (roaming, local):
            if base:
                cands += [Path(base) / n for n in names]
        if local:
            for pkg in glob.glob(os.path.join(local, "Packages", "Claude_*")):
                cands += [Path(pkg) / "LocalCache" / "Roaming" / n for n in names]
    elif sys.platform == "darwin":
        base = home() / "Library" / "Application Support"
        cands += [base / n for n in names]
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (home() / ".config"))
        cands += [base / n for n in names]
    return _dedupe([c for c in cands if _isdir(c)])


def desktop_output_dirs() -> list[Path]:
    """User-visible Desktop outputs (Artifacts, scheduled-task results)."""
    return [p for p in (home() / "Claude", home() / "Documents" / "Claude") if _isdir(p)]


def desktop_indexeddb_dirs() -> list[Path]:
    """Claude Desktop IndexedDB directories (MSIX package LocalCache)."""
    cands: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            for pkg in glob.glob(os.path.join(local, "Packages", "Claude_*")):
                idb = Path(pkg) / "LocalCache" / "Roaming" / "Claude" / "IndexedDB"
                if _isdir(idb):
                    cands.append(idb)
    elif sys.platform == "darwin":
        base = home() / "Library" / "Application Support"
        for n in ("Claude", "Claude-3p"):
            idb = base / n / "IndexedDB"
            if _isdir(idb):
                cands.append(idb)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (home() / ".config"))
        for n in ("Claude", "Claude-3p"):
            idb = base / n / "IndexedDB"
            if _isdir(idb):
                cands.append(idb)
    return _dedupe(cands)


def claude_ai_indexeddb_dirs(browsers: Optional[Iterable[tuple[str, Path]]] = None) -> list[tuple[str, Path]]:
    """Find every ``https_claude.ai_0.indexeddb.leveldb`` dir, in browsers AND Desktop.

    Returns (label, leveldb_dir) pairs. The sibling ``.blob`` dir (if present) holds
    externally-stored blobs referenced by IndexedDB records.

    ``browsers`` overrides the machine's own Chromium user-data dirs (the same
    ``(name, root)`` pairs :func:`browser_user_data_dirs` yields) so a caller that was
    handed an explicit profile list — tests, a copied forensic image — stays hermetic.
    """
    out: list[tuple[str, Path]] = []

    # Claude Desktop (Electron) — MSIX or plain Roaming
    for d in desktop_indexeddb_dirs():
        for entry in d.iterdir():
            if entry.is_dir() and entry.name.startswith("https_claude.ai_") and entry.name.endswith(".leveldb"):
                out.append((f"Desktop:{d.parent.name}", entry))

    # Browsers (Chromium family) — every profile of every browser
    for browser, root in (browsers if browsers is not None else browser_user_data_dirs()):
        for prof in browser_profiles(Path(root)):
            idb_root = prof / "IndexedDB"
            if not _isdir(idb_root):
                continue
            for entry in idb_root.iterdir():
                if entry.is_dir() and entry.name.startswith("https_claude.ai_") and entry.name.endswith(".leveldb"):
                    out.append((f"{browser}:{prof.name}", entry))

    # Also platform.claude.com (console/API) if present
    return _dedupe_tuples(out)


def _dedupe_tuples(pairs: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    seen = set()
    out = []
    for label, p in pairs:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append((label, p))
    return out


def desktop_log_dirs() -> list[Path]:
    cands = []
    if sys.platform == "darwin":
        cands += [home() / "Library" / "Logs" / "Claude", home() / "Library" / "Logs" / "Claude-3p"]
    return [c for c in cands if _isdir(c)]


# --------------------------------------------------------------------------- #
# Browsers (Chromium family)
# --------------------------------------------------------------------------- #

def browser_user_data_dirs() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        table = [
            ("Chrome", Path(local) / "Google" / "Chrome" / "User Data"),
            ("Edge", Path(local) / "Microsoft" / "Edge" / "User Data"),
            ("Brave", Path(local) / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ("Vivaldi", Path(local) / "Vivaldi" / "User Data"),
            ("Chromium", Path(local) / "Chromium" / "User Data"),
            ("Opera", Path(roaming) / "Opera Software" / "Opera Stable"),
        ]
        for pkg in glob.glob(os.path.join(local, "Packages", "TheBrowserCompany.Arc_*")):
            table.append(("Arc", Path(pkg) / "LocalCache" / "Local" / "Arc" / "User Data"))
    elif sys.platform == "darwin":
        base = home() / "Library" / "Application Support"
        table = [
            ("Chrome", base / "Google" / "Chrome"),
            ("Edge", base / "Microsoft Edge"),
            ("Brave", base / "BraveSoftware" / "Brave-Browser"),
            ("Vivaldi", base / "Vivaldi"),
            ("Chromium", base / "Chromium"),
            ("Opera", base / "com.operasoftware.Opera"),
            ("Arc", base / "Arc" / "User Data"),
        ]
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (home() / ".config"))
        table = [
            ("Chrome", cfg / "google-chrome"),
            ("Edge", cfg / "microsoft-edge"),
            ("Brave", cfg / "BraveSoftware" / "Brave-Browser"),
            ("Vivaldi", cfg / "vivaldi"),
            ("Chromium", cfg / "chromium"),
            ("Opera", cfg / "opera"),
            ("Chromium (snap)", home() / "snap" / "chromium" / "common" / "chromium"),
            ("Chrome (flatpak)", home() / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome"),
        ]
    for name, path in table:
        if _isdir(path):
            out.append((name, path))
    return out


def browser_profiles(user_data: Path) -> list[Path]:
    """Profile directories inside a Chromium user-data dir (``Default``, ``Profile 3`` ...)."""
    profiles: list[Path] = []
    try:
        entries = sorted(user_data.iterdir())
    except OSError:
        return profiles
    for p in entries:
        if p.is_dir() and (p / "History").exists() or (p.is_dir() and (p / "Preferences").exists()):
            profiles.append(p)
    if not profiles and (user_data / "History").exists():   # Opera: root *is* the profile
        profiles.append(user_data)
    return profiles


# --------------------------------------------------------------------------- #
# Project folder names  (docs/CLAUDE_STORAGE.md section 1.4)
# --------------------------------------------------------------------------- #

def _utf16_units(text: str) -> list[int]:
    data = text.encode("utf-16-le", errors="surrogatepass")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def _sanitize(path: str) -> str:
    """Every UTF-16 code unit outside [A-Za-z0-9] becomes '-' (an emoji becomes two)."""
    out = []
    for unit in _utf16_units(path):
        ch = chr(unit) if unit < 0xD800 or unit > 0xDFFF else "\ufffd"
        out.append(ch if (ch.isascii() and ch.isalnum()) else "-")
    return "".join(out)


def java_hash32(text: str) -> int:
    """``h = (h << 5) - h + unit`` with 32-bit wrap - Java's String.hashCode."""
    h = 0
    for unit in _utf16_units(text):
        h = (h * 31 + unit) & 0xFFFFFFFF
    return h - 0x100000000 if h >= 0x80000000 else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def encode_project_dir(cwd: str) -> str:
    """Folder name Claude Code uses under ``projects/`` for a working directory."""
    name = _sanitize(cwd)
    if len(name) <= 200:
        return name
    return f"{name[:200]}-{_base36(abs(java_hash32(cwd)))}"


_WIN_DRIVE = re.compile(r"^([A-Za-z])--(.*)$")


def decode_project_dir(name: str) -> str:
    """Best-effort reverse of :func:`encode_project_dir` (lossy - prefer the ``cwd`` field)."""
    m = _WIN_DRIVE.match(name)
    if m:
        return f"{m.group(1)}:\\" + m.group(2).replace("-", "\\")
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name


def project_label(project_path: str, project_dir: str = "") -> str:
    """Short display name for a project."""
    src = project_path or decode_project_dir(project_dir)
    src = src.replace("\\", "/").rstrip("/")
    return src.rsplit("/", 1)[-1] or src or project_dir or "(unknown)"


def long_path(p) -> str:
    r"""Extended-length form (``\\?\C:\...``) for paths near Windows' 260-character limit; identity elsewhere.

    Claude Desktop's MSIX data dir plus a Cowork session folder routinely exceeds 260 characters
    (``...\LocalCache\Roaming\Claude\local-agent-mode-sessions\<uuid>\<uuid>\local_<uuid>\.claude\projects\...``).
    Only use this at I/O boundaries (open / stat): stored paths stay in their normal form so reports never show the prefix.
    """
    s = str(p)
    if os.name == "nt" and len(s) >= 240 and not s.startswith("\\\\?\\"):
        s = os.path.abspath(s)
        return "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return s


# --------------------------------------------------------------------------- #
def _isdir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _dedupe(paths: list[Path]) -> list[Path]:
    seen, out = set(), []
    for p in paths:
        try:
            key = str(p.resolve()).lower()
        except OSError:
            key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out
