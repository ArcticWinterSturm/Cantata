"""Chat & Cowork archaeology - the local footprint of Claude *outside* Claude Code.

Conversation bodies for claude.ai **Chat** live on Anthropic's servers; what is local is

1. **Claude Desktop** - Cowork sessions (full transcripts + audit log) and Code-tab wrappers (metadata),
2. **browser traces** - History rows (``claude.ai/chat/<uuid>`` with titles), IndexedDB / Local Storage size,
   the *Claude in Chrome* extension store (plaintext tokens - flagged, never extracted),
3. **export archives** you saved (Settings -> Privacy -> Export data): zipped ``conversations.json`` /
   ``projects.json`` / ``users.json`` - triaged *and* optionally reconstructed to Markdown.

Read-only everywhere; History is copied to a temp file first (Chromium locks it while running).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from . import desktop as desktop_mod, paths, scanner
from .models import RecoveryCancelled
from .redaction import Redactor, count_markers
from .report import slugify
from .transcripts import content_text, parse_transcript

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_URL_RE = re.compile(r"https?://(?:www\.)?claude\.(?:ai|com)/(chat|project|projects|code|artifacts|recents|new)(?:/([0-9A-Za-z_\-]{8,}))?")
_TITLE_TAIL = re.compile(r"\s*[-–|]\s*Claude\s*$")
_CHROME_EPOCH_OFFSET = 11_644_473_600


@dataclass
class ChatThread:
    thread_id: str = ""
    title: str = ""
    source_kind: str = ""          # cowork | chat-local | code-tab | export | browser
    created_at: str = ""
    updated_at: str = ""
    model: str = ""
    archived: bool = False
    cli_session_id: str = ""
    local_transcript: str = ""
    turns: int = 0
    messages: int = 0
    tools: int = 0
    browser_visits: int = 0
    in_export: bool = False
    export_messages: int = 0
    in_indexeddb: bool = False          # a full conversation tree lives in a local claude.ai IndexedDB
    indexeddb_source: str = ""          # which store it came from (e.g. "Brave:Default", "Desktop:Claude")
    folders: list = field(default_factory=list)
    cost_usd: Optional[float] = None
    error: str = ""
    scheduled: str = ""
    messageCount: int = 0

    @property
    def body_location(self) -> str:
        if self.local_transcript:
            return "local transcript"
        if self.in_export:
            return "export archive"
        if self.in_indexeddb:
            return "IndexedDB cache"
        return "server only"

    @property
    def catalog_only(self) -> bool:
        return not (self.local_transcript or self.in_export or self.in_indexeddb)


@dataclass
class BrowserProfile:
    browser: str = ""
    profile: str = ""
    path: str = ""
    history_hits: int = 0
    last_visit: str = ""
    thread_ids: dict = field(default_factory=dict)        # uuid -> {title, visits, last}
    indexeddb_bytes: int = 0
    local_storage_hit: bool = False
    service_worker_hit: bool = False
    wal_uuids: int = 0
    extension_bytes: int = 0
    kinds: Counter = field(default_factory=Counter)        # chat / project / code / ...
    error: str = ""


@dataclass
class ExportArtifact:
    path: Path = Path(".")
    kind: str = ""
    size_bytes: int = 0
    sha256: str = ""
    conversations: int = 0
    messages: int = 0
    projects: int = 0
    project_docs: int = 0
    attachments: int = 0
    tool_uses: int = 0
    models: Counter = field(default_factory=Counter)
    first: str = ""
    last: str = ""
    entries: list = field(default_factory=list)
    pii_hits: Counter = field(default_factory=Counter)
    chat_thread_refs: list = field(default_factory=list)
    extracted: int = 0
    extract_dir: str = ""
    error: str = ""


@dataclass
class WebChatReport:
    source_dir: Path = Path(".")
    threads: list = field(default_factory=list)
    browsers: list = field(default_factory=list)
    exports: list = field(default_factory=list)
    desktop: Optional[desktop_mod.DesktopInfo] = None
    warnings: list = field(default_factory=list)
    conversations: dict = field(default_factory=dict)     # uuid -> (title) for extracted exports
    idb_conversations: list = field(default_factory=list)  # conversation metadata from IndexedDB
    idb_sources: list = field(default_factory=list)        # [(label, path, records, with_body)]

    def coverage(self) -> dict:
        """How much of what you *opened* is still recoverable locally.

        The browser History knows every ``claude.ai/chat/<uuid>`` you visited; the IndexedDB
        cache only holds the ones whose body was still resident. The gap is the whole point of
        this report - a signed-out account switch wipes the Desktop cache and re-arms it for
        the new account, so a large gap after switching accounts is expected, not a bug.
        """
        visited = {t.thread_id for t in self.threads if t.browser_visits}
        recoverable = {t.thread_id for t in self.threads if not t.catalog_only}
        with_body = {c.get("uuid") for c in self.idb_conversations if c.get("has_tree")}
        return {
            "visited": len(visited),
            "visited_recoverable": len(visited & recoverable),
            "visited_missing": len(visited - recoverable),
            "missing_examples": sorted(visited - recoverable)[:12],
        }

    def stats(self) -> dict:
        th = self.threads
        d = self.desktop.stats() if self.desktop else {}
        cov = self.coverage()
        return {
            "threads": len(th),
            "cowork": sum(1 for t in th if t.source_kind in ("cowork", "chat-local")),
            "code_tab": sum(1 for t in th if t.source_kind == "code-tab"),
            "export_conversations": sum(1 for t in th if t.source_kind == "export" or t.in_export),
            "browser_threads": sum(1 for t in th if t.browser_visits),
            "recoverable": sum(1 for t in th if not t.catalog_only),
            "server_only": sum(1 for t in th if t.catalog_only),
            "with_transcript": sum(1 for t in th if t.local_transcript),
            "indexeddb_threads": sum(1 for t in th if t.in_indexeddb),
            "indexeddb_records": sum(s[2] for s in self.idb_sources),
            "visited": cov["visited"],
            "visited_recoverable": cov["visited_recoverable"],
            "visited_missing": cov["visited_missing"],
            "browsers": sum(1 for b in self.browsers if b.history_hits or b.indexeddb_bytes or b.extension_bytes),
            "browser_visits": sum(b.history_hits for b in self.browsers),
            "indexeddb_kb": sum(b.indexeddb_bytes for b in self.browsers) // 1024,
            "extension_stores": sum(1 for b in self.browsers if b.extension_bytes),
            "exports": len(self.exports),
            "export_pii_files": sum(1 for e in self.exports if e.pii_hits),
            "export_messages": sum(e.messages for e in self.exports),
            "desktop_found": bool(self.desktop and self.desktop.found),
            "cowork_cost_usd": d.get("cost_usd", 0),
        }


# --------------------------------------------------------------------------- #
# export triage + reconstruction
# --------------------------------------------------------------------------- #

def iter_json_array(fp, chunk: int = 1 << 20) -> Iterator[Any]:
    """Stream the objects of a top-level JSON array without loading the file (exports can exceed 100 MB)."""
    dec = json.JSONDecoder()
    buf = ""
    started = False
    eof = False
    pos = 0
    while True:
        if not eof and (len(buf) - pos) < max(1, chunk // 4):
            data = fp.read(chunk)
            if not data:
                eof = True
            else:
                buf = buf[pos:] + (data if isinstance(data, str) else data.decode("utf-8", "replace"))
                pos = 0
        if not started:
            i = pos
            while i < len(buf) and buf[i] in " \t\r\n\ufeff":
                i += 1
            if i >= len(buf):
                if eof:
                    return
                pos = i
                continue
            if buf[i] != "[":
                return
            started, pos = True, i + 1
        while pos < len(buf) and buf[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(buf):
            if eof:
                return
            continue
        if buf[pos] == "]":
            return
        try:
            obj, end = dec.raw_decode(buf, pos)
        except ValueError:
            if eof:
                return
            more = fp.read(chunk)
            if not more:
                eof = True
                continue
            buf = buf[pos:] + (more if isinstance(more, str) else more.decode("utf-8", "replace"))
            pos = 0
            continue
        pos = end
        yield obj


def _blocks_to_md(msg: dict, red: Redactor, include_thinking: bool) -> list[str]:
    out: list[str] = []
    blocks = msg.get("content")
    if isinstance(blocks, list) and blocks:
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text"):
                out.append(red.apply(b["text"]))
            elif t == "thinking" and include_thinking and (b.get("thinking") or b.get("text")):
                out += ["<details><summary>💭 thinking</summary>", "", "> " + red.apply(str(b.get("thinking") or b.get("text"))).replace("\n", "\n> "),
                        "", "</details>"]
            elif t == "tool_use":
                inp = json.dumps(b.get("input"), ensure_ascii=False, default=str)
                if b.get("name") == "artifacts" and isinstance(b.get("input"), dict):
                    a = b["input"]
                    out += [f"<details><summary>🧩 artifact — {red.apply(str(a.get('title') or a.get('id') or ''))}</summary>", "",
                            "```", red.apply(str(a.get("content") or ""))[:8000], "```", "", "</details>"]
                else:
                    out += [f"<details><summary>🔧 {b.get('name', 'tool')}</summary>", "", "```json", red.apply(inp)[:3000], "```", "", "</details>"]
            elif t == "tool_result":
                out += ["<details><summary>↩ tool result</summary>", "", "```", red.apply(content_text(b.get("content")))[:3000], "```", "", "</details>"]
    elif msg.get("text"):
        out.append(red.apply(msg["text"]))
    return out


def render_conversation_md(conv: dict, red: Redactor, include_thinking: bool = True) -> str:
    msgs = conv.get("chat_messages") or []
    L = [f"# {red.apply(conv.get('name') or '(untitled conversation)')}", ""]
    rows = [("Conversation", f"`{red.session_id(conv.get('uuid', ''))}`"), ("Created", str(conv.get("created_at", ""))[:19].replace("T", " ")),
            ("Updated", str(conv.get("updated_at", ""))[:19].replace("T", " ")), ("Messages", str(len(msgs)))]
    if conv.get("model"):
        rows.append(("Model", str(conv["model"])))
    L += ["| Field | Value |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows] + [""]
    if conv.get("summary"):
        L += ["> **Summary:** " + red.apply(conv["summary"])[:600], ""]
    for m in msgs:
        who = "👤 **You**" if m.get("sender") == "human" else "🤖 **Claude**"
        L += [f"### {who} — {str(m.get('created_at', ''))[:19].replace('T', ' ')}", ""]
        body = _blocks_to_md(m, red, include_thinking)
        L += body or ["_(empty)_"]
        L.append("")
        for a in m.get("attachments") or []:
            if isinstance(a, dict):
                L += [f"<details><summary>📎 attachment — {red.apply(str(a.get('file_name', '')))} ({a.get('file_size', '?')} B)</summary>", "",
                      "```", red.apply(str(a.get("extracted_content") or ""))[:3000], "```", "", "</details>", ""]
        files = [str(f.get("file_name", "")) for f in (m.get("files") or []) if isinstance(f, dict)]
        if files:
            L += ["_🖼 files: " + ", ".join(red.apply(f) for f in files) + "_", ""]
    return "\n".join(L).rstrip() + "\n"


def _sha256(path: Path, cap: int = 1 << 30) -> str:
    h = hashlib.sha256()
    done = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk or done > cap:
                    break
                h.update(chunk)
                done += len(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _scan_conversations(fp, art: ExportArtifact, red: Redactor, extract_dir: Optional[Path], include_thinking: bool,
                        cancel: Optional[Callable[[], bool]], catalog: dict) -> None:
    seen_uuids: list[str] = []
    for i, conv in enumerate(iter_json_array(fp), 1):
        if cancel and cancel():
            raise RecoveryCancelled()
        if not isinstance(conv, dict):
            continue
        art.conversations += 1
        uid = str(conv.get("uuid") or "")
        if uid:
            seen_uuids.append(uid)
        msgs = conv.get("chat_messages") or []
        art.messages += len(msgs)
        created = str(conv.get("created_at") or "")
        updated = str(conv.get("updated_at") or "")
        if created and (not art.first or created < art.first):
            art.first = created
        if updated and updated > art.last:
            art.last = updated
        if conv.get("model"):
            art.models[str(conv["model"])] += 1
        text_blob = [str(conv.get("name") or ""), str(conv.get("summary") or "")]
        for m in msgs:
            if not isinstance(m, dict):
                continue
            art.attachments += len(m.get("attachments") or []) + len(m.get("files") or [])
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    art.tool_uses += 1
            block_text = [str(b["text"]) for b in (m.get("content") or []) if isinstance(b, dict) and b.get("text")]
            text_blob.extend(block_text or [str(m.get("text") or "")])      # blocks repeat the flat text - count once
            for a in m.get("attachments") or []:
                if isinstance(a, dict):
                    text_blob.append(str(a.get("extracted_content") or ""))
        art.pii_hits.update(count_markers("\n".join(text_blob)))
        catalog[uid] = {"title": str(conv.get("name") or ""), "created": created, "updated": updated,
                        "messages": len(msgs), "model": str(conv.get("model") or "")}
        if extract_dir is not None:
            extract_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{art.conversations:04d}_{slugify(red.apply(conv.get('name') or 'chat'), 80)}.md"
            (extract_dir / fname).write_text(render_conversation_md(conv, red, include_thinking), encoding="utf-8")
            art.extracted += 1
    art.chat_thread_refs = seen_uuids[:2000]


def triage_export(path: Path, *, red: Optional[Redactor] = None, extract_dir: Optional[Path] = None,
                  include_thinking: bool = True, cancel: Optional[Callable[[], bool]] = None,
                  catalog: Optional[dict] = None) -> ExportArtifact:
    path = Path(path)
    red = red or Redactor("standard")
    catalog = catalog if catalog is not None else {}
    art = ExportArtifact(path=path)
    try:
        art.size_bytes = path.stat().st_size
    except OSError as exc:
        art.error = str(exc)
        return art
    art.sha256 = _sha256(path)
    ext = path.suffix.lower()
    conv_dir = (extract_dir / "conversations") if extract_dir is not None else None
    try:
        if ext == ".zip" or zipfile.is_zipfile(path):
            art.kind = "claude-export-zip"
            with zipfile.ZipFile(path) as zf:
                art.entries = [i.filename for i in zf.infolist()][:400]
                names = {Path(i.filename).name.lower(): i.filename for i in zf.infolist() if not i.is_dir()}
                if "conversations.json" not in names:
                    art.kind = "zip (no conversations.json - not a Claude export?)"
                if "conversations.json" in names:
                    with zf.open(names["conversations.json"]) as raw:
                        _scan_conversations(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"), art, red, conv_dir,
                                            include_thinking, cancel, catalog)
                for key in ("projects.json",):
                    if key in names:
                        with zf.open(names[key]) as raw:
                            for proj in iter_json_array(io.TextIOWrapper(raw, encoding="utf-8", errors="replace")):
                                if isinstance(proj, dict):
                                    art.projects += 1
                                    docs = proj.get("docs") or []
                                    art.project_docs += len(docs)
                                    art.pii_hits.update(count_markers("\n".join(str(d.get("content") or "") for d in docs if isinstance(d, dict))))
                                    if extract_dir is not None and docs:
                                        pdir = extract_dir / "projects" / slugify(red.apply(proj.get("name") or "project"), 60)
                                        pdir.mkdir(parents=True, exist_ok=True)
                                        for j, d in enumerate(docs, 1):
                                            if isinstance(d, dict):
                                                (pdir / f"{j:02d}_{slugify(str(d.get('filename') or 'doc'), 60)}.md").write_text(
                                                    f"# {red.apply(str(d.get('filename') or ''))}\n\n```\n{red.apply(str(d.get('content') or ''))}\n```\n", encoding="utf-8")
                                                art.extracted += 1
                for key in ("users.json", "user.json"):
                    if key in names:
                        with zf.open(names[key]) as raw:
                            art.pii_hits.update(count_markers(raw.read(2_000_000).decode("utf-8", "replace")))
        elif ext == ".json":
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096).lstrip()
                fh.seek(0)
                if head.startswith("["):
                    art.kind = "conversations-json"
                    _scan_conversations(fh, art, red, conv_dir, include_thinking, cancel, catalog)
                else:
                    data = json.load(fh)
                    if isinstance(data, dict) and "chat_messages" in data:
                        art.kind = "single-conversation-json"
                        _scan_conversations(io.StringIO(json.dumps([data])), art, red, conv_dir, include_thinking, cancel, catalog)
                    else:
                        art.kind = "json (unrecognised)"
        elif ext in (".md", ".markdown", ".txt"):
            art.kind = "markdown-export"
            text = path.read_text(encoding="utf-8", errors="replace")
            art.pii_hits.update(count_markers(text))
            art.messages = len(re.findall(r"(?im)^#+\s*(?:human|you|user|claude|assistant)\b", text))
            art.chat_thread_refs = sorted(set(UUID_RE.findall(text)))[:200]
        else:
            art.kind = "other"
    except RecoveryCancelled:
        raise
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        art.error = f"{type(exc).__name__}: {exc}"
    if extract_dir is not None and art.extracted:
        art.extract_dir = str(extract_dir)
    return art


# --------------------------------------------------------------------------- #
# browser footprint
# --------------------------------------------------------------------------- #

def _dir_bytes(p: Path) -> int:
    total = 0
    if p.is_dir():
        for r, _d, files in os.walk(p):
            for f in files:
                try:
                    total += os.stat(os.path.join(r, f)).st_size
                except OSError:
                    pass
    return total


def _file_contains(folder: Path, needle: bytes, suffixes=(".ldb", ".log", ".sst"), cap: int = 64 * 1024 * 1024) -> bool:
    read = 0
    if not folder.is_dir():
        return False
    for f in sorted(folder.rglob("*")):
        if f.is_file() and f.suffix.lower() in suffixes:
            try:
                data = f.read_bytes()
            except OSError:
                continue
            read += len(data)
            if needle in data:
                return True
            if read > cap:
                break
    return False


def _chrome_time(v: Any) -> str:
    try:
        t = int(v) / 1_000_000 - _CHROME_EPOCH_OFFSET
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t > 0 else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def scan_browser_profile(browser: str, profile: Path) -> BrowserProfile:
    bp = BrowserProfile(browser=browser, profile=profile.name, path=str(profile))
    hist = profile / "History"
    if hist.is_file():
        tmpdir = tempfile.mkdtemp(prefix="cantata_hist_")
        try:
            tmp = Path(tmpdir) / "History"
            shutil.copy2(hist, tmp)
            for side in ("History-wal", "History-journal"):
                if (profile / side).is_file():
                    shutil.copy2(profile / side, Path(tmpdir) / side)
            con = sqlite3.connect(f"file:{tmp.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                cols = {r[1] for r in con.execute("PRAGMA table_info(urls)")}
                if {"url", "title", "visit_count", "last_visit_time"} <= cols:
                    rows = con.execute("SELECT url, title, visit_count, last_visit_time FROM urls WHERE url LIKE '%claude.ai%' "
                                       "OR url LIKE '%claude.com%'").fetchall()
                    for url, title, visits, last in rows:
                        m = _URL_RE.match(str(url))
                        if not m:
                            continue
                        bp.history_hits += int(visits or 1)
                        lv = _chrome_time(last)
                        if lv > bp.last_visit:
                            bp.last_visit = lv
                        kind = "project" if m.group(1) == "projects" else m.group(1)
                        bp.kinds[kind] += int(visits or 1)
                        tid = m.group(2)
                        if tid and m.group(1) in ("chat", "project", "projects", "code"):
                            e = bp.thread_ids.setdefault(tid, {"title": "", "visits": 0, "last": "", "kind": kind})
                            e["visits"] += int(visits or 1)
                            e["title"] = e["title"] or _TITLE_TAIL.sub("", str(title or "")).strip()
                            e["last"] = max(e["last"], lv)
            finally:
                con.close()
        except (sqlite3.Error, OSError) as exc:
            bp.error = f"History: {exc}"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    idb = profile / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"
    bp.indexeddb_bytes = _dir_bytes(idb) + _dir_bytes(profile / "IndexedDB" / "https_claude.ai_0.indexeddb.blob")
    if idb.is_dir():
        found = set()
        for f in idb.glob("*.log"):                       # write-ahead logs are not snappy-compressed
            try:
                found.update(m.decode().lower() for m in re.findall(rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                                                                   f.read_bytes()[:16 * 1024 * 1024]))
            except OSError:
                continue
        bp.wal_uuids = len(found)
    bp.local_storage_hit = _file_contains(profile / "Local Storage" / "leveldb", b"https://claude.ai")
    bp.service_worker_hit = _file_contains(profile / "Service Worker" / "Database", b"claude.ai")
    bp.extension_bytes = _dir_bytes(profile / "Local Extension Settings" / paths.CLAUDE_IN_CHROME_EXT_ID)
    return bp


def scan_browsers(browsers: Optional[list] = None, cancel: Optional[Callable[[], bool]] = None) -> list[BrowserProfile]:
    out = []
    cancel = cancel or (lambda: False)
    for name, root in (browsers if browsers is not None else paths.browser_user_data_dirs()):
        for prof in paths.browser_profiles(Path(root)):
            if cancel():
                raise RecoveryCancelled()
            out.append(scan_browser_profile(name, prof))
    return out


def _iter_store_records(store) -> Iterator:
    """Yield records from an IndexedDB object store without dying on one bad record.

    Chromium garbage-collects externally-serialized blob files while the LevelDB
    record that points at them survives. ``ccl_chromium_reader`` only tolerates the
    resulting ``FileNotFoundError`` when a ``bad_deserializer_data_handler`` is
    supplied; without one it propagates out of the generator and every record *after*
    the evicted blob is silently dropped.

    Passing a handler turns "abort the whole store" into "skip one entry". The handler
    itself must not raise, or the library re-raises (see read_record_precursor).
    """
    swallowed: list[BaseException] = []

    def _swallow(key: Any, data: Any) -> None:
        swallowed.append(FileNotFoundError("indexeddb blob evicted"))

    try:
        yield from store.iterate_records(errors_to_stdout=False,
                                         bad_deserializer_data_handler=_swallow)
    except Exception as exc:                      # last-resort: keep partial results
        if swallowed:
            return
        raise exc


def _as_int(value: Any, default: int = 0) -> int:
    """Best-effort int. Live LevelDB records carry floats, ints, None -- and the occasional
    non-numeric string. A ValueError here would abort the entire IndexedDB scan, so it must
    never propagate."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _conv_rank(conv: dict) -> tuple:
    """Sort key for choosing between duplicate conversation records: newest wins.

    Ordered by recency first, then by message count, then by whether tree metadata is
    present, so a richer record still wins a tie between two equally-dated copies.
    """
    tree = conv.get("tree") if isinstance(conv.get("tree"), dict) else {}
    has_body = 1 if (tree or conv.get("has_tree")) else 0            # `has_tree` is set by the metadata scanner
    recency = max(_as_int(conv.get(k)) for k in ("conversationUpdatedAt", "writtenAt", "fetchedAt"))
    return (recency, _as_int(conv.get("messageCount")), has_body, str(conv.get("updated_at") or ""))


def scan_claude_ai_indexeddb(sources: Optional[Iterable[tuple[str, Path]]] = None,
                             log: Optional[Callable[[str, str], None]] = None,
                             source_stats: Optional[list] = None,
                             cancel: Optional[Callable[[], bool]] = None) -> list[dict]:
    """Scan every local claude.ai IndexedDB (Claude Desktop *and* Chromium browsers).

    The conversation cache does not live only in Claude Desktop: claude.ai on the web
    keeps the same ``claude-conversation-store`` / ``trees`` object store inside the
    browser profile. After an account switch (or a Desktop reinstall) the Desktop copy
    can be wiped while the browser's survives - scanning only the Desktop dir then
    reports every conversation as "server only" even though the bodies are right there.

    Returns a list of dicts with keys: uuid, name, summary, model, created_at,
    updated_at, messageCount, approxBytes, product, accountUuid, orgUuid, source
    and ``has_tree`` (True when a full ``tree.chat_messages`` body was cached).
    """
    log = log or (lambda m, lvl="info": None)
    cancel = cancel or (lambda: False)
    conversations: list[dict] = []

    try:
        from ccl_chromium_reader import ccl_chromium_indexeddb
    except ImportError:
        log("ccl_chromium_reader not installed - skipping IndexedDB scan", "warn")
        return conversations

    sources = list(sources) if sources is not None else paths.claude_ai_indexeddb_dirs()
    if not sources:
        log("No claude.ai IndexedDB directories found", "info")
        return conversations

    for label, leveldb_path in sources:
        if not leveldb_path.is_dir():
            continue
        blob_path = leveldb_path.parent / leveldb_path.name.replace(".leveldb", ".blob")
        log(f"Scanning IndexedDB: {leveldb_path}", "info")
        n_here = 0
        with_body_here = 0
        try:
            wrapper = ccl_chromium_indexeddb.WrappedIndexDB(str(leveldb_path), str(blob_path))
            for db_id_meta in wrapper.database_ids:
                # We want the claude-conversation-store database
                if db_id_meta.name != "claude-conversation-store":
                    continue
                db = wrapper[db_id_meta.dbid_no]
                for store_name in list(db.object_store_names):
                    store = db[store_name]
                    for record in _iter_store_records(store):
                        if cancel():
                            raise RecoveryCancelled()
                        try:
                            value = record.value
                            if not isinstance(value, dict) or "conversationUuid" not in value:
                                continue
                            tree = value.get("tree") if isinstance(value.get("tree"), dict) else {}
                            conv = {
                                "uuid": value.get("conversationUuid", ""),
                                "product": value.get("product", ""),
                                "accountUuid": value.get("accountUuid", ""),
                                "orgUuid": value.get("orgUuid", ""),
                                "fetchedAt": value.get("fetchedAt", 0),
                                "conversationUpdatedAt": value.get("conversationUpdatedAt", 0),
                                "approxBytes": value.get("approxBytes", 0),
                                "messageCount": value.get("messageCount", 0),
                                "lastOpenedAt": value.get("lastOpenedAt", 0),
                                "writtenAt": value.get("writtenAt", 0),
                                "source": label,
                                "has_tree": bool(tree.get("chat_messages")),
                            }
                            if tree:
                                conv["name"] = tree.get("name", "")
                                conv["summary"] = tree.get("summary", "")
                                conv["model"] = tree.get("model", "")
                                conv["created_at"] = tree.get("created_at", "")
                                conv["updated_at"] = tree.get("updated_at", "")
                                if not conv["messageCount"]:
                                    conv["messageCount"] = len(tree.get("chat_messages") or [])
                            conversations.append(conv)
                            n_here += 1
                            if conv["has_tree"]:
                                with_body_here += 1
                        except Exception:
                            continue
        except RecoveryCancelled:
            raise
        except Exception as e:
            log(f"Error parsing {leveldb_path}: {e}", "warn")
            continue
        if source_stats is not None:
            source_stats.append((label, str(leveldb_path), n_here, with_body_here))
        if n_here == 0:
            log(f"{label}: no conversation records in this IndexedDB "
                f"(store present but empty - a signed-out account switch re-creates it)", "warn")

    # Deduplicate by UUID.
    #
    # This used to keep whichever record had the most *keys* (`len(conv) > len(seen[uuid])`),
    # which only ever separated "has tree metadata" (15 keys) from "does not" (10). Among
    # same-shape records the comparison was always False, so the FIRST version LevelDB
    # yielded won -- which can be a stale copy. Prefer the newest instead.
    seen: dict[str, dict] = {}
    for conv in conversations:
        uuid = conv.get("uuid", "")
        if not uuid:
            continue
        prev = seen.get(uuid)
        if prev is None or _conv_rank(conv) > _conv_rank(prev):
            seen[uuid] = conv

    result = list(seen.values())
    with_body = sum(1 for c in result if c.get("has_tree"))
    log(f"Found {len(result)} conversations in IndexedDB ({with_body} with a recoverable body)", "info")
    return result


# Older internal name; the scanner now covers browsers as well as Claude Desktop.
scan_desktop_indexeddb = scan_claude_ai_indexeddb



def extract_indexeddb_conversations(log: Optional[Callable[[str, str], None]] = None,
                                    sources: Optional[Iterable[tuple[str, Path]]] = None,
                                    progress: Optional[Callable[[float, str], None]] = None,
                                    cancel: Optional[Callable[[], bool]] = None) -> list[dict]:
    """Extract FULL conversations (with chat_messages) from claude.ai IndexedDB.

    Covers Claude Desktop *and* Chromium browsers - the same ``trees`` store lives in
    each. Decode stack: LevelDB -> Blink IndexedDB key encoding -> V8 SerializedScriptValue
    -> conversation JSON (tree.chat_messages).

    ``cancel`` is polled between sources, records and conversations so the GUI's Cancel
    button can interrupt the (potentially very large) rebuild instead of appearing dead.

    Returns a list of dicts with keys: uuid, name, model, created_at, updated_at,
    platform, thinking_mode, messages (list), stop_reasons (Counter), approxBytes.
    """
    log = log or (lambda m, lvl="info": None)
    progress = progress or (lambda f, label="": None)
    cancel = cancel or (lambda: False)
    out: list[dict] = []

    try:
        from ccl_chromium_reader import ccl_chromium_indexeddb
    except ImportError:
        log("ccl_chromium_reader not installed - skipping full IndexedDB extraction", "warn")
        return out

    sources = list(sources) if sources is not None else paths.claude_ai_indexeddb_dirs()
    if not sources:
        log("No claude.ai IndexedDB directories found", "info")
        return out

    n_sources = max(1, len(sources))
    for i, (label, leveldb_path) in enumerate(sources):
        if cancel():
            raise RecoveryCancelled()
        if not leveldb_path.is_dir():
            continue
        blob_path = leveldb_path.parent / leveldb_path.name.replace(".leveldb", ".blob")
        log(f"Extracting IndexedDB: {label} -> {leveldb_path}", "info")
        progress(0.05 + 0.6 * i / n_sources, f"Reading {label} (IndexedDB)")

        try:
            wrapper = ccl_chromium_indexeddb.WrappedIndexDB(str(leveldb_path), str(blob_path))
        except Exception as e:
            log(f"Error opening {leveldb_path}: {e}", "warn")
            continue

        best: dict[str, dict] = {}
        try:
            for db_id_meta in wrapper.database_ids:
                if db_id_meta.name != "claude-conversation-store":
                    continue
                db = wrapper[db_id_meta.dbid_no]
                if "trees" not in db.object_store_names:
                    continue
                store = db["trees"]

                for record in _iter_store_records(store):
                    if cancel():
                        raise RecoveryCancelled()
                    try:
                        value = record.value
                        if not isinstance(value, dict):
                            continue
                        uuid = value.get("conversationUuid", "")
                        tree = value.get("tree", {})
                        if not uuid or not isinstance(tree, dict) or not tree.get("chat_messages"):
                            continue                       # no cached body -> nothing to rebuild
                        msgs = tree.get("chat_messages", [])
                        prev = best.get(uuid)
                        if prev is None or len(msgs) > len(prev["tree"].get("chat_messages", [])):
                            best[uuid] = {"meta": value, "tree": tree}
                    except RecoveryCancelled:
                        raise
                    except Exception:
                        continue
        except RecoveryCancelled:
            raise
        except Exception as e:
            log(f"Error iterating {leveldb_path}: {e}", "warn")

        n_best = max(1, len(best))
        for j, (uuid, conv) in enumerate(best.items()):
            if cancel():
                raise RecoveryCancelled()
            progress(0.65 + 0.25 * (i + j / n_best) / n_sources, f"Rebuilding {label} (IndexedDB)")
            tree = conv["tree"]
            msgs = sorted(tree.get("chat_messages", []), key=lambda m: m.get("index", 0))
            stops: dict[str, int] = {}
            for m in msgs:
                sr = m.get("stop_reason", "")
                if sr:
                    stops[sr] = stops.get(sr, 0) + 1
            out.append({
                "uuid": uuid,
                "name": tree.get("name", "") or "untitled",
                "summary": tree.get("summary", ""),
                "model": tree.get("model", ""),
                "created_at": tree.get("created_at", ""),
                "updated_at": tree.get("updated_at", ""),
                "platform": tree.get("platform", ""),
                "thinking_mode": tree.get("effective_thinking_mode", ""),
                "is_starred": tree.get("is_starred", False),
                "is_archived": tree.get("is_archived", False),
                "settings": tree.get("settings", {}),
                "messages": msgs,
                "stop_reasons": stops,
                "approxBytes": conv["meta"].get("approxBytes", 0),
                "accountUuid": conv["meta"].get("accountUuid", ""),
                "orgUuid": conv["meta"].get("orgUuid", ""),
                "source": label,
            })

    # Deduplicate across sources (Desktop + browsers share uuids) — keep the richest copy,
    # but *record* the alternates instead of dropping them. The same uuid can legitimately
    # appear several times with different content (a regenerated/edited conversation, or the
    # same chat cached on a different account with a different model). Keeping only the
    # longest silently threw the others away; they are now preserved under ``versions``.
    merged: dict[str, dict] = {}
    for c in out:
        prev = merged.get(c["uuid"])
        if prev is None:
            merged[c["uuid"]] = c
            continue
        src = c.get("source", "")
        if len(c.get("messages", [])) > len(prev.get("messages", [])):
            c["also_in"] = sorted(set(prev.get("also_in", []) + [prev.get("source", "")]) - {src})
            c.setdefault("versions", []).extend(prev.get("versions", []))
            c["versions"].append(prev)
            merged[c["uuid"]] = c
        else:
            prev.setdefault("also_in", [])
            if src and src not in prev["also_in"]:
                prev["also_in"].append(src)
            prev.setdefault("versions", []).append(c)

    for c in merged.values():
        c["also_in"] = [s for s in c.get("also_in", []) if s]

    log(f"Extracted {len(merged)} full conversations from IndexedDB"
        + (f" ({sum(1 for c in merged.values() if c.get('versions'))} with alternate copies preserved)"
           if any(c.get("versions") for c in merged.values()) else ""), "info")
    return list(merged.values())


def render_message_content(content) -> str:
    """Render a Claude message content array (text/thinking/tool_use/tool_result) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, default=str, ensure_ascii=False)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type", "")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            for s in block.get("summaries", []) or []:
                if isinstance(s, dict) and s.get("summary"):
                    parts.append(f"\n<thinking_summary>{s['summary']}</thinking_summary>\n")
            th = block.get("thinking", "")
            if th:
                parts.append(f"\n<thinking>\n{th}\n</thinking>\n")
        elif btype == "tool_use":
            parts.append(f"\n<tool_use name=\"{block.get('name','')}\">\n"
                         f"{json.dumps(block.get('input', {}), indent=2, default=str, ensure_ascii=False)}\n</tool_use>\n")
        elif btype == "tool_result":
            parts.append(f"\n<tool_result>\n"
                         f"{json.dumps(block.get('content', ''), default=str, ensure_ascii=False)}\n</tool_result>\n")
        else:
            parts.append(json.dumps(block, default=str, ensure_ascii=False))
    return "\n".join(p if isinstance(p, str) else str(p) for p in parts)


def _unique_path(out_dir: Path, fname: str, used: dict) -> Path:
    """Return a path in ``out_dir`` that no earlier conversation in this run has taken.

    The naive name is ``<date>_<title>_<uuid8>.md``. That is **not** unique: the date is
    day-granular, the title is shared by regenerated copies, and ``uuid8`` is only the first
    8 hex chars. Two records for the same conversation written on the same day (or two
    different conversations whose uuids share a prefix) produced the *same* filename, and the
    second ``write_text`` silently clobbered the first - conversations were lost while the
    summary still counted them. ``used`` is per-run state so a fresh run is not perturbed by
    files an earlier run left behind.
    """
    if fname not in used:
        used[fname] = 1
        return out_dir / fname
    stem, dot, ext = fname.rpartition(".")
    while True:
        used[fname] += 1
        cand = f"{stem}__{used[fname]}{dot}{ext}"
        if cand not in used:
            used[cand] = 1
            return out_dir / cand


def write_indexeddb_conversations(conversations: list[dict], out_dir: Path,
                                  redact: Optional[Callable[[str], str]] = None,
                                  cancel: Optional[Callable[[], bool]] = None) -> list[Path]:
    """Write extracted IndexedDB conversations as Markdown files. Returns written paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    redact = redact or (lambda s: s)
    cancel = cancel or (lambda: False)
    written: list[Path] = []
    index_rows: list[tuple] = []
    all_stops: dict[str, int] = {}
    non_normal: list[tuple] = []
    sources_seen: set[str] = set()
    used_names: dict = {}

    for conv in sorted(conversations, key=lambda c: c.get("updated_at", ""), reverse=True):
        if cancel():
            raise RecoveryCancelled()
        name = conv.get("name") or "untitled"
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in name)[:80].strip() or "untitled"
        fname = f"{conv.get('updated_at','')[:10]}_{safe}_{conv['uuid'][:8]}.md"
        fpath = _unique_path(out_dir, fname, used_names)

        L: list[str] = [f"# {name}", ""]
        L.append(f"- **UUID**: `{conv['uuid']}`")
        L.append(f"- **Model**: {conv.get('model','')}")
        L.append(f"- **Created**: {conv.get('created_at','')}")
        L.append(f"- **Updated**: {conv.get('updated_at','')}")
        L.append(f"- **Messages**: {len(conv.get('messages', []))}")
        L.append(f"- **Platform**: {conv.get('platform','')}")
        L.append(f"- **Thinking mode**: {conv.get('thinking_mode','')}")
        L.append(f"- **Starred**: {conv.get('is_starred', False)}  **Archived**: {conv.get('is_archived', False)}")
        L.append(f"- **Approx bytes**: {conv.get('approxBytes', 0)}")
        if conv.get("source"):
            L.append(f"- **Source**: {conv['source']}")
        if conv.get("also_in"):
            L.append(f"- **Also stored in**: {', '.join(conv['also_in'])}")
        if conv.get("summary"):
            L.append(f"- **Summary**: {conv['summary']}")
        L += ["", "---", ""]

        for m in conv.get("messages", []):
            sender = m.get("sender", "?")
            who = {"human": "👤 **Human**", "assistant": "🤖 **Claude**"}.get(sender, f"**{sender}**")
            L.append(f"### {who}  `{m.get('index','?')}`  _{m.get('created_at','')}_")
            L.append("")
            sr = m.get("stop_reason", "")
            if sr:
                all_stops[sr] = all_stops.get(sr, 0) + 1
                if sr != "end_turn":
                    non_normal.append((name, m.get("index"), sender, sr, m.get("created_at", "")))
                    L.append(f"> ⚠️ **stop_reason: `{sr}`**")
                    if sr == "refusal":
                        L.append("> ")
                        L.append("> _Safeguard pause. UI shows: \"Paused / Edit and retry with Sonnet 5.5 — "
                                 "safeguards flagged this message. This sometimes happens with safe, normal conversations. "
                                 "Continue with Sonnet 4.6, send feedback, or learn more. Details: [reasoning_extraction]\"_")
                    L.append("")
            body = render_message_content(m.get("content", m.get("text", "")))
            if not body.strip():
                body = m.get("text", "") or ("_(empty message — no content blocks)_" if not sr else "")
            L.append(redact(body))
            L.append("")
            atts = m.get("attachments", []) or []
            if atts:
                L.append(f"_attachments: {len(atts)}_")
                for a in atts:
                    if not isinstance(a, dict):
                        continue
                    label = a.get("file_name") or a.get("file_type") or a.get("id") or "attachment"
                    size = a.get("file_size")
                    L.append(f"  - `{label}` ({a.get('file_type','')}{', ' + str(size) + ' bytes' if size else ''})")
                    ec = a.get("extracted_content")
                    if ec:
                        L.append("")
                        L.append("    <details><summary>extracted content</summary>")
                        L.append("")
                        for line in str(ec).splitlines()[:400]:
                            L.append(f"    {line}")
                        L.append("")
                        L.append("    </details>")
                L.append("")
            L += ["---", ""]

        fpath.write_text("\n".join(L), encoding="utf-8")
        written.append(fpath)
        index_rows.append((conv.get("updated_at", "")[:16], conv.get("model", ""),
                           len(conv.get("messages", [])), name, fname))
        sources_seen.add(conv.get("source", "") or "-")

        # Alternate copies of the same conversation (regenerated / edited / cached on another
        # account). They are real content that the dedup used to discard, so write them too.
        for k, alt in enumerate(conv.get("versions", []) or [], 1):
            if cancel():
                raise RecoveryCancelled()
            alt_name = alt.get("name") or name
            alt_safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in alt_name)[:80].strip() or "untitled"
            alt_fname = f"{alt.get('updated_at','')[:10]}_{alt_safe}_{alt['uuid'][:8]}__v{k}.md"
            alt_path = _unique_path(out_dir, alt_fname, used_names)
            A: list[str] = [f"# {alt_name}  (alternate copy {k})", ""]
            A.append(f"- **UUID**: `{alt['uuid']}`  (same conversation, different stored copy)")
            A.append(f"- **Model**: {alt.get('model','')}")
            A.append(f"- **Created**: {alt.get('created_at','')}  **Updated**: {alt.get('updated_at','')}")
            A.append(f"- **Messages**: {len(alt.get('messages', []))}")
            A.append(f"- **Source**: {alt.get('source','')}")
            A += ["", "---", ""]
            for m in alt.get("messages", []):
                sender = m.get("sender", "?")
                who = {"human": "👤 **Human**", "assistant": "🤖 **Claude**"}.get(sender, f"**{sender}**")
                A.append(f"### {who}  `{m.get('index','?')}`  _{m.get('created_at','')}_")
                A.append("")
                body = render_message_content(m.get("content", m.get("text", "")))
                if not body.strip():
                    body = m.get("text", "") or "_(empty message — no content blocks)_"
                A.append(redact(body))
                A += ["", "---", ""]
            alt_path.write_text("\n".join(A), encoding="utf-8")
            written.append(alt_path)
            index_rows.append((alt.get("updated_at", "")[:16], alt.get("model", ""),
                               len(alt.get("messages", [])), f"{alt_name} (v{k})", alt_path.name))

    # Index file. `out_dir` may already hold files from an earlier run into the same folder
    # (the GUI reuses "Extract to"). Overwriting INDEX.md with just this run's rows made those
    # files invisible - 408 conversations on disk under an index that listed 4. Scan what is
    # actually there and index that, marking which entries this run produced.
    idx = out_dir / "INDEX.md"
    this_run = {r[4] for r in index_rows}
    disk_rows: list[tuple] = []
    seen_names: set[str] = set()
    for f in sorted(out_dir.glob("*.md")):
        if f.name == "INDEX.md" or f.name in seen_names:
            continue
        seen_names.add(f.name)
        head = f.read_text(encoding="utf-8", errors="replace")[:1200]
        u = re.search(r"- \*\*UUID\*\*: `([^`]+)`", head)
        t = re.match(r"#\s+(.+)", head)
        n = re.search(r"- \*\*Messages\*\*: (\d+)", head)
        dtm = re.search(r"- \*\*Updated\*\*: ([0-9T:\-]{16})", head)
        mdl = re.search(r"- \*\*Model\*\*: ([^\r\n]*)", head)
        srcm = re.search(r"- \*\*Source\*\*: ([^\r\n]*)", head)
        disk_rows.append((dtm.group(1) if dtm else "", mdl.group(1) if mdl else "",
                          int(n.group(1)) if n else -1,
                          (t.group(1) if t else "").replace("  (alternate copy", " (alt"), f.name,
                          u.group(1)[:8] if u else "", srcm.group(1) if srcm else ""))
    disk_rows.sort(key=lambda r: r[0], reverse=True)

    I = ["# Claude Desktop Conversations (extracted from IndexedDB)", "",
         f"Total: **{len(disk_rows)}** conversation file(s) in this folder"
         + (f" ({len(this_run)} written by this run)" if this_run else ""), ""]
    if sources_seen:
        I.append(f"Sources: {', '.join(sorted(s for s in sources_seen if s))}")
        I.append("")
    I += ["| Updated | Model | Msgs | Title | File |", "|---|---|---|---|---|"]
    for updated, model, n, name, fname, _u8, _src in disk_rows:
        star = "" if fname in this_run else " ·"
        I.append(f"| {updated} | {model} | {n if n >= 0 else '-'} | {redact(name)}{star} | [{fname}]({fname}) |")
    if any(r[4] not in this_run for r in disk_rows):
        I += ["", "_Entries without a trailing · predate this run._"]

    if all_stops:
        I += ["", "## stop_reason taxonomy (all messages)", "",
              "Every assistant message carries a `stop_reason`. This is where UI state like "
              "\"Paused\", \"Edit and retry\", and user-stop live.", "",
              "| stop_reason | Meaning | Count |", "|---|---|---|"]
        meanings = {
            "end_turn": "Normal completion",
            "refusal": "**Safeguard flagged** — UI shows \"Paused / Edit and retry with Sonnet 5.5\"",
            "user_canceled": "User pressed Stop (the \"quit\" state)",
            "stop_sequence": "Model hit a configured stop sequence",
            "max_tokens": "Hit token limit",
        }
        for k, v in sorted(all_stops.items(), key=lambda x: -x[1]):
            I.append(f"| `{k}` | {meanings.get(k, '—')} | {v} |")

    if non_normal:
        I += ["", "## Non-normal stops (safeguards / cancels)", "",
              "| Conversation | Msg# | Sender | stop_reason | When |", "|---|---|---|---|---|"]
        for name, idx_n, sender, sr, when in non_normal:
            I.append(f"| {redact(name)} | {idx_n} | {sender} | `{sr}` | {when[:19]} |")

    idx.write_text("\n".join(I), encoding="utf-8")
    written.append(idx)
    return written


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #

def _iso(ts: str) -> str:
    return ts


def run_webchat(config_dir: Path, exports: Iterable[Path] = (), *, desktop_dirs: Optional[list] = None,
                browsers: Optional[list] = None, scan_browser: bool = True, scan_transcripts: bool = True,
                scan_indexeddb: bool = True,
                extract_dir: Optional[Path] = None, redaction_level: str = "standard", include_thinking: bool = True,
                log: Optional[Callable[[str, str], None]] = None, progress: Optional[Callable[[float, str], None]] = None,
                cancel: Optional[Callable[[], bool]] = None) -> WebChatReport:
    log = log or (lambda m, lvl="info": None)
    progress = progress or (lambda f, label="": None)
    cancel = cancel or (lambda: False)
    config_dir = Path(config_dir)
    # `Path("")` is `.` -- an empty source would silently scan the CWD and report "0
    # recoverable" instead of failing loudly. Callers fill this in, but never trust a
    # caller-provided path to be non-empty.
    if str(config_dir) in ("", "."):
        config_dir = scanner.detect_default_source()
    report = WebChatReport(source_dir=config_dir)
    red = Redactor(redaction_level, home_dirs=[Path.home()])

    progress(0.02, "Scanning Claude Desktop…")
    info = desktop_mod.scan_desktop(desktop_dirs, log=log)
    report.desktop = info
    code_map: dict[str, Path] = {}
    if (config_dir / "projects").is_dir():
        for f in (config_dir / "projects").glob("*/*.jsonl"):
            code_map[f.stem] = f
    threads: dict[str, ChatThread] = {}
    n_sessions = max(1, len(info.sessions))
    for i, ds in enumerate(info.sessions, 1):
        if cancel():
            raise RecoveryCancelled()
        progress(0.05 + 0.30 * i / n_sessions, f"Desktop session {i}/{n_sessions}")
        t = ChatThread(thread_id=ds.local_id.replace("local_", ""), title=ds.title or ds.initial_message[:80], source_kind=ds.kind,
                       created_at=ds.created_at, updated_at=ds.last_activity, model=ds.model, archived=ds.archived,
                       cli_session_id=ds.cli_session_id, folders=ds.folders, error=ds.error, scheduled=ds.scheduled_task)
        if ds.audit.get("results"):
            t.cost_usd = ds.audit.get("total_cost_usd")
        tpath: Optional[Path] = None
        if ds.kind == "code-tab":
            tpath = code_map.get(ds.cli_session_id)
        elif ds.transcripts:
            main = [tf for tf in ds.transcripts if tf.kind == "cowork"]
            tpath = (main[0] if main else ds.transcripts[0]).path
        if tpath is not None:
            t.local_transcript = str(tpath)
            if scan_transcripts:
                try:
                    rec = parse_transcript(tpath)
                    t.turns, t.messages, t.tools = len(rec.turns), sum(len(tn.messages) + (1 if tn.prompt else 0) for tn in rec.turns), rec.tool_count
                    t.title = t.title or rec.display_title
                except Exception as exc:
                    report.warnings.append(f"Could not parse {tpath.name}: {exc}")
        elif ds.kind == "code-tab" and ds.cli_session_id:
            report.warnings.append(f"Code-tab wrapper {ds.local_id[:14]} points to transcript {ds.cli_session_id[:8]} which is not on disk (swept or deleted).")
        threads[f"{ds.kind}:{t.thread_id}"] = t

    export_catalog: dict[str, dict] = {}
    exp_list = [Path(p) for p in exports]
    for j, p in enumerate(exp_list, 1):
        progress(0.35 + 0.35 * (j - 1) / max(1, len(exp_list)), f"Export {p.name}")
        log(f"Triage export: {p}", "info")
        ed = (Path(extract_dir) / f"export_{j:02d}_{slugify(p.stem, 40)}") if extract_dir else None
        art = triage_export(p, red=red, extract_dir=ed, include_thinking=include_thinking, cancel=cancel, catalog=export_catalog)
        report.exports.append(art)
        log(f"  {art.kind}: {art.conversations} conversation(s), {art.messages} message(s), {art.projects} project(s)"
            + (f", extracted {art.extracted} file(s)" if art.extracted else ""), "ok" if not art.error else "warn")
        if art.error:
            report.warnings.append(f"{p.name}: {art.error}")
    for uid, meta in export_catalog.items():
        t = threads.get(f"export:{uid}") or ChatThread(thread_id=uid, source_kind="export")
        t.title, t.created_at, t.updated_at = t.title or meta["title"], t.created_at or meta["created"], t.updated_at or meta["updated"]
        t.model, t.in_export, t.export_messages = t.model or meta["model"], True, meta["messages"]
        threads[f"export:{uid}"] = t

    if scan_browser:
        progress(0.72, "Scanning browser profiles…")
        report.browsers = scan_browsers(browsers, cancel=cancel)
        for bp in report.browsers:
            if bp.history_hits or bp.indexeddb_bytes or bp.extension_bytes:
                log(f"{bp.browser}/{bp.profile}: {bp.history_hits} claude.ai history visit(s), {bp.indexeddb_bytes // 1024} KB IndexedDB"
                    + (" — Claude-in-Chrome extension store present" if bp.extension_bytes else ""), "info")
            if bp.error:
                report.warnings.append(f"{bp.browser}/{bp.profile}: {bp.error}")
            if bp.extension_bytes:
                report.warnings.append(f"{bp.browser}/{bp.profile}: the Claude in Chrome extension keeps OAuth/API tokens unencrypted in its LevelDB store "
                                       f"({bp.extension_bytes:,} bytes) - contents were NOT read.")
        by_id: dict[str, ChatThread] = {t.thread_id: t for t in threads.values()}
        for bp in report.browsers:
            for tid, meta in bp.thread_ids.items():
                t = by_id.get(tid)
                if t is None:
                    kind = {"project": "browser-project", "projects": "browser-project", "code": "browser-code"}.get(meta.get("kind"), "browser")
                    t = ChatThread(thread_id=tid, title=meta["title"], source_kind=kind, updated_at=meta["last"])
                    threads[f"browser:{tid}"] = t
                    by_id[tid] = t
                t.browser_visits += meta["visits"]
                t.title = t.title or meta["title"]
                t.updated_at = t.updated_at or meta["last"]

    # Scan every claude.ai IndexedDB (Claude Desktop *and* browsers) for conversation
    # metadata. The extractor reads the same set, so the catalog now agrees with what
    # ends up in the per-conversation files instead of calling them "server only".
    idb_conversations = []
    if scan_indexeddb:
        progress(0.85, "Scanning claude.ai IndexedDB…")
        idb_sources: list = []
        idb_conversations = scan_claude_ai_indexeddb(paths.claude_ai_indexeddb_dirs(browsers), log=log,
                                                     source_stats=idb_sources, cancel=cancel)
        report.idb_conversations = idb_conversations
        report.idb_sources = idb_sources
    if idb_conversations:
        by_id: dict[str, ChatThread] = {t.thread_id: t for t in threads.values()}
        for conv in idb_conversations:
            uuid = conv.get('uuid', '')
            if not uuid:
                continue
            has_body = bool(conv.get('has_tree'))
            t = by_id.get(uuid)
            if t is None:
                t = ChatThread(
                    thread_id=uuid,
                    title=conv.get('name', ''),
                    source_kind='chat-local',
                    created_at=conv.get('created_at', ''),
                    updated_at=conv.get('updated_at', ''),
                    model=conv.get('model', ''),
                    messageCount=conv.get('messageCount', 0),
                )
                threads[f"idb:{uuid}"] = t
                by_id[uuid] = t
            if has_body:
                t.in_indexeddb = True
                t.indexeddb_source = t.indexeddb_source or conv.get('source', '')
            # Update with IndexedDB metadata if not already set
            if not t.title and conv.get('name'):
                t.title = conv['name']
            if not t.model and conv.get('model'):
                t.model = conv['model']
            if not t.created_at and conv.get('created_at'):
                t.created_at = conv['created_at']
            if not t.updated_at and conv.get('updated_at'):
                t.updated_at = conv['updated_at']
            if not t.messageCount and conv.get('messageCount'):
                t.messageCount = conv['messageCount']
        with_body = sum(1 for c in idb_conversations if c.get('has_tree'))
        log(f"IndexedDB: found {len(idb_conversations)} conversation(s), {with_body} with a recoverable body", "info")

    report.threads = sorted(threads.values(), key=lambda t: t.updated_at or t.created_at or "", reverse=True)
    if not info.found:
        report.warnings.append("No Claude Desktop data directory found.")
    # Tell the user plainly how much of what they actually opened is still recoverable. The
    # usual cause of a big gap is signing out / switching accounts: that wipes the claude.ai
    # IndexedDB conversation cache and re-creates it for the new account, and the old bodies
    # are then server-side only until they are re-opened in a browser.
    cov = report.coverage()
    if cov["visited"] and cov["visited_missing"]:
        report.warnings.append(
            f"{cov['visited_missing']} of {cov['visited']} conversations seen in browser history have no "
            f"recoverable local body (only {cov['visited_recoverable']} do). The claude.ai IndexedDB cache "
            f"holds only conversations still resident for the *signed-in* account - signing out or switching "
            f"accounts wipes it. Re-open a conversation in the browser to re-cache its body, or import a "
            f"Claude export archive (Settings -> Privacy -> Export data) for the full history.")
    progress(1.0, "Done")
    return report
