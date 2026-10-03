"""Claude Desktop / claude.ai IndexedDB poller for the live engine.

This is what makes LIVE capture *old conversations you flick through*, not just
new Claude Code sessions. Claude Desktop persists every conversation it renders
into a Chromium IndexedDB store; opening a conversation in the sidebar causes
the client to write/refresh its record. Polling that store therefore picks up
both genuinely new turns AND conversations that were merely opened.

Store layout, decode stack and every field used here were established by direct
extraction — see ``CLAUDE_STORAGE_FORMAT_VERIFIED.md``. Summary of the parts
that matter to this module:

* ``https_claude.ai_0.indexeddb.leveldb`` is the conversation store. **The
  sibling ``.blob`` directory must be passed too**, or records whose values are
  stored externally raise ``FileNotFoundError``.
* A **missing blob aborts iteration by default**, silently dropping every
  conversation after that point. ``bad_deserializer_data_handler`` must be
  passed to survive it.
* The same conversation UUID appears repeatedly (one record per write); keep the
  one with the most ``chat_messages``.
* ``stop_reason`` alone cannot classify a stop — ``stopped: true`` on a block
  and ``structured_content.end_conversation`` are also tells.
* Timestamps can carry **8 fractional digits**, which Python's
  ``fromisoformat()`` rejects; parse with a regex.

Everything here is READ-ONLY. Nothing is written back to the store.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# stop taxonomy — shared with the standalone trap
# --------------------------------------------------------------------------- #

STOP_MODES = {
    "normal": "Completed normally.",
    "hard_refusal": "Server rejected the request: `content: []`, nothing generated. "
                    "The client renders the pause panel from the empty message.",
    "stopped_in_words": "The model declined in prose and stopped mid-message. `stop_reason` is a "
                        "normal `end_turn`; the tell is a block-level `stopped: true` with a null "
                        "`stop_timestamp`. No pause panel, no end_conversation.",
    "user_canceled": "Stop button pressed. Partial text survives unless pressed before the first block.",
    "stop_sequence": "A configured stop sequence fired mid-tool-loop.",
    "ended_by_tool": "Permanently closed by the `end_conversation` tool. `stop_reason` stays "
                     "`end_turn`; the tell is a tool_result with "
                     "`structured_content.end_conversation: true`.",
    "regenerated": "The ACTIVE reply at a branch point: an earlier reply to the same prompt was "
                   "replaced by this one (regenerate, or an edit that forked the thread).",
    "superseded": "An EARLIER reply that a regeneration or edit replaced. It is not on the "
                  "conversation's active chain, but it is kept because its timings are real and "
                  "comparable against the reply that replaced it.",
}


def classify_stop(stop_reason: str, *, stopped: bool = False, ended: bool = False) -> str:
    if stop_reason == "refusal":
        return "hard_refusal"
    if stop_reason == "user_canceled":
        return "user_canceled"
    if stop_reason == "stop_sequence":
        return "stop_sequence"
    if stopped:
        return "stopped_in_words"
    if ended:
        return "ended_by_tool"
    return "normal"


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_ts(ts: Any) -> Optional[datetime]:
    """Parse an ISO-8601 stamp, tolerating sub-microsecond fractional seconds.

    Claude writes both ``...05.589811Z`` (6 digits) and ``...05.58981102Z`` (8).
    ``datetime.fromisoformat()`` on Python 3.10 accepts only 3 or 6 digits and
    raises ``ValueError`` otherwise — which silently drops the block, and with
    it every timing derived from that block.
    """
    if not ts or not isinstance(ts, str):
        return None
    m = _TS_RE.match(ts.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro)
    except ValueError:
        return None
    if not tz or tz == "Z":
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    tz = tz[1:].replace(":", "")
    return dt.replace(tzinfo=timezone(sign * timedelta(hours=int(tz[:2]), minutes=int(tz[2:4]))))


def _ms(a: Optional[datetime], b: Optional[datetime]) -> Optional[int]:
    if a is None or b is None:
        return None
    return int(round((b - a).total_seconds() * 1000))


# --------------------------------------------------------------------------- #
# branches: regeneration and edit
# --------------------------------------------------------------------------- #
#
# `chat_messages` is a FLAT list, not a path. A regeneration (or an edit that
# forks the thread) does not append a turn — it adds a SECOND child to the same
# parent, and the client marks which one is live via `selectedChildByUuid`.
# `current_leaf_message_uuid` walks back through those selections.
#
# Verified on this machine: a regenerated conversation holds BOTH the
# superseded reply and the active one in `chat_messages`, with identical
# `parent_message_uuid`. Treating the flat list as linear reports the old reply
# as if it were a separate turn in the same thread — wrong, and it corrupts
# every "turns in this conversation" count and every per-conversation latency
# series derived from it.

def branch_parents(tree: dict) -> dict[str, list[str]]:
    """``{parent_uuid: [child_uuid, ...]}`` for every parent with >1 child.

    Only multi-child parents are returned: a single-child parent is a normal
    append, not a fork.
    """
    out: dict[str, list[str]] = {}
    for parent, kids in (tree.get("childrenByParentUuid") or {}).items():
        if isinstance(kids, list) and len(kids) > 1:
            out[parent] = [str(k) for k in kids]
    return out


def _active_chain(tree: dict) -> list[str]:
    """UUIDs on the conversation's live path, oldest first.

    Follows `parent_message_uuid`, falling back to the `childrenByParentUuid`
    inverse for messages that omit it (the first human turn has no parent).
    """
    msgs = tree.get("chat_messages") or []
    by_uuid = {m.get("uuid"): m for m in msgs if isinstance(m, dict) and m.get("uuid")}
    if not by_uuid:
        return []
    parent_of: dict[str, str] = {}
    for p, kids in (tree.get("childrenByParentUuid") or {}).items():
        for k in (kids if isinstance(kids, list) else []):
            parent_of[str(k)] = p
    leaf = tree.get("current_leaf_message_uuid") or tree.get("serverLeafUuid")
    chain: list[str] = []
    seen: set[str] = set()
    cur = leaf
    while cur and cur in by_uuid and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = by_uuid[cur].get("parent_message_uuid") or parent_of.get(cur)
    chain.reverse()
    return chain


def branch_state(tree: dict) -> dict[str, str]:
    """``{message_uuid: "active" | "superseded"}`` for every message at a fork.

    Messages that are not part of any fork are omitted. ``superseded`` means the
    message is retained in the store but is NOT on the live chain — it was
    replaced by a regeneration or an edit.
    """
    forks = branch_parents(tree)
    if not forks:
        return {}
    on_chain = set(_active_chain(tree))
    sel = tree.get("selectedChildByUuid") or {}
    out: dict[str, str] = {}
    for parent, kids in forks.items():
        for k in kids:
            # selectedChildByUuid is authoritative; the leaf chain is the fallback
            # for the last fork where the selection may not be written yet.
            active = (sel.get(parent) == k) or (k in on_chain)
            out[k] = "active" if active else "superseded"
    return out


# --------------------------------------------------------------------------- #
# store discovery
# --------------------------------------------------------------------------- #

def indexeddb_stores() -> list[tuple[str, Path]]:
    """Every ``https_claude.ai_*`` leveldb dir: Claude Desktop (MSIX) + browsers.

    Under MSIX ``%APPDATA%\\Claude`` is virtualised into
    ``AppData\\Local\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\Claude``,
    so the naive path finds nothing. Browsers keep an independent copy and the
    two are NOT identical — scan both and merge on UUID.
    """
    import os

    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))

    candidates: list[tuple[str, Path]] = []
    for pkg in sorted(local.glob("Packages/Claude_*")):
        candidates.append(("Desktop", pkg / "LocalCache" / "Roaming" / "Claude" / "IndexedDB"))
    candidates += [("Desktop", roaming / "Claude" / "IndexedDB"),
                   ("Desktop", local / "Claude" / "IndexedDB")]

    for base in (local, roaming):
        for prod in ("BraveSoftware/Brave-Browser", "Google/Chrome", "Microsoft/Edge",
                     "Chromium", "Vivaldi", "Opera Software/Opera Stable"):
            root = base / prod / "User Data"
            if root.is_dir():
                for prof in sorted(root.iterdir()):
                    if prof.is_dir() and (prof.name == "Default" or prof.name.startswith("Profile ")):
                        candidates.append((f"{prod.split('/')[0]}:{prof.name}",
                                           prof / "IndexedDB"))
    # Linux / macOS
    candidates += [("Desktop", home / ".config" / "Claude" / "IndexedDB"),
                   ("Desktop", home / "Library" / "Application Support" / "Claude" / "IndexedDB")]

    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, base in candidates:
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for e in entries:
            if not (e.is_dir() and e.name.startswith("https_claude.ai_") and e.name.endswith(".leveldb")):
                continue
            key = str(e.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((label, e))
    return out


def _blob_dir(leveldb: Path) -> Path:
    """The ``.blob`` sibling. Passing it is mandatory; see the module docstring."""
    return leveldb.parent / leveldb.name.replace(".leveldb", ".blob")


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #

def _walk_conversations(leveldb: Path, on_error: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Extract every conversation from one store, keeping the richest record per UUID.

    The ``meta`` store carries ``writtenAt`` / ``lastOpenedAt`` which the ``trees``
    store does not.  Both are read and merged so the fingerprint can detect
    changes via ``writtenAt``.
    """
    from ccl_chromium_reader import ccl_chromium_indexeddb

    blob = _blob_dir(leveldb)
    best: dict[str, dict] = {}
    meta_by_uuid: dict[str, dict] = {}
    try:
        wrapper = ccl_chromium_indexeddb.WrappedIndexDB(str(leveldb), str(blob))
    except Exception as e:
        if on_error:
            on_error(f"{leveldb.name}: cannot open ({type(e).__name__}: {e})")
        return []

    # `bad_deserializer_data_handler` is what keeps ONE evicted blob from
    # aborting the whole iteration and silently dropping everything after it.
    swallow = lambda *a, **k: None                                   # noqa: E731

    for db_meta in wrapper.database_ids:
        if db_meta.name != "claude-conversation-store":
            continue
        try:
            db = wrapper[db_meta.dbid_no]
        except Exception:
            continue

        for store_name, sink in (("trees", best), ("meta", meta_by_uuid)):
            try:
                store = db[store_name]
            except Exception:
                continue
            try:
                records = store.iterate_records(errors_to_stdout=False,
                                                bad_deserializer_data_handler=swallow)
                for rec in records:
                    try:
                        v = rec.value
                    except Exception:
                        continue                      # skipped blob / undecodable
                    if not isinstance(v, dict):
                        continue
                    uid = v.get("conversationUuid") or v.get("uuid") or ""
                    if not uid:
                        continue
                    if store_name == "meta":
                        # `meta` carries lastOpenedAt, which `trees` does not.
                        cur = meta_by_uuid.get(uid)
                        if cur is None or (v.get("writtenAt") or 0) > (cur.get("writtenAt") or 0):
                            meta_by_uuid[uid] = v
                        continue
                    tree = v.get("tree")
                    if not isinstance(tree, dict):
                        continue
                    prev = sink.get(uid)
                    if prev is None or len(tree.get("chat_messages") or []) > len(
                            (prev.get("tree") or {}).get("chat_messages") or []):
                        sink[uid] = {"record": v, "tree": tree, "source": leveldb.name}
            except Exception as e:
                if on_error:
                    on_error(f"{leveldb.name}[{store_name}]: {type(e).__name__}: {e}")

    # Merge meta records into the tree records so the fingerprint can see writtenAt.
    for uid, conv in best.items():
        meta = meta_by_uuid.get(uid)
        if meta:
            conv["meta"] = meta
    return list(best.values())


def _norm_message(msg: dict) -> dict:
    """Normalise one message into the shape the timing model consumes."""
    blocks = [b for b in (msg.get("content") or []) if isinstance(b, dict)]
    n_stopped = sum(1 for b in blocks if b.get("stopped"))
    ended = False
    tools: list[str] = []
    tool_meta: dict = {}
    for b in blocks:
        if b.get("type") == "tool_use" and b.get("tool_origin") == "first_party":
            tools.append(str(b.get("name")))
        sc = b.get("structured_content")
        if isinstance(sc, dict) and sc.get("end_conversation") is True:
            ended = True
        meta = b.get("meta")
        if isinstance(meta, dict):
            for k in ("error_type", "output_format_category"):
                if meta.get(k):
                    tool_meta[k] = meta[k]
    return {
        "index": msg.get("index"),
        "sender": msg.get("sender"),
        "stop_reason": msg.get("stop_reason") or "",
        "created_at": msg.get("created_at") or "",
        "input_mode": msg.get("input_mode") or "",
        "blocks": blocks,
        "n_stopped": n_stopped,
        "stopped": bool(n_stopped),
        "ended_conversation": ended,
        "first_party_tools": tools,
        "tool_meta": tool_meta,
        "stop_mode": classify_stop(msg.get("stop_reason") or "", stopped=bool(n_stopped), ended=ended),
        "text_len": sum(len(b.get("text") or "") for b in blocks
                        if b.get("type") == "text" and isinstance(b.get("text"), str)),
    }


def analyse_message(msg: dict) -> dict:
    """Per-response latencies, all relative to the first block's start.

    Every content block carries ``start_timestamp``/``stop_timestamp`` (100%
    coverage observed), so the whole timing surface is derivable:

      ttft_ms              first block start -> first block stop (an UPPER BOUND:
                           the first token is not persisted)
      thinking_ms          -> last thinking block stop
      thinking_to_tool_ms  last thinking stop before the first tool -> first tool
      first_tool_ms        -> first tool_use start
      first_text_ms        -> first text block start
      tool_wall_ms         sum(tool_use stop -> its tool_result start)
      stream_gaps_ms       sum of inter-block gaps (model think-time)
      response_ms          first block start -> last block stop
    """
    n = _norm_message(msg)
    rec: dict[str, Any] = {k: n[k] for k in
                           ("index", "sender", "stop_reason", "created_at", "input_mode",
                            "stop_mode", "stopped", "ended_conversation", "text_len")}
    rec["blocks"] = len(n["blocks"])
    if n["n_stopped"]:
        rec["n_stopped"] = n["n_stopped"]
    if n["first_party_tools"]:
        rec["first_party_tools"] = sorted(set(n["first_party_tools"]))
    if n["tool_meta"]:
        rec["tool_meta"] = n["tool_meta"]

    blocks = n["blocks"]
    if not blocks:
        return rec
    starts = [parse_ts(b.get("start_timestamp")) for b in blocks]
    stops = [parse_ts(b.get("stop_timestamp")) for b in blocks]
    t0 = next((s for s in starts if s), None)
    if t0 is None:
        return rec

    rec["ttft_ms"] = _ms(starts[0], stops[0])
    last_stop = next((s for s in reversed(stops) if s), None)
    if last_stop is None:
        # A `stopped` block never gets a stop_timestamp; fall back to the last
        # start so the duration is still "time until last observable activity".
        last_stop = next((s for s in reversed(starts) if s), None)
    rec["response_ms"] = _ms(t0, last_stop)
    rec["n_unterminated"] = sum(1 for b in blocks
                                if b.get("start_timestamp") and not b.get("stop_timestamp"))

    thinking_stop = thinking_before_tool = first_tool = first_text = None
    tool_wall = gaps = 0
    pending = prev_stop = None
    counts = {"text": 0, "thinking": 0, "tool_use": 0, "tool_result": 0}
    for b, s, e in zip(blocks, starts, stops):
        bt = b.get("type", "")
        if bt in counts:
            counts[bt] += 1
        if bt == "text" and first_text is None:
            first_text = s
        elif bt == "thinking" and e:
            thinking_stop = e
            if first_tool is None:
                thinking_before_tool = e
        elif bt == "tool_use":
            if first_tool is None:
                first_tool = s
            pending = e
        elif bt == "tool_result":
            if pending and s:
                tool_wall += max(0, _ms(pending, s) or 0)
            pending = None
        if prev_stop and s:
            gaps += max(0, _ms(prev_stop, s) or 0)
        if e:
            prev_stop = e

    rec.update(n_text=counts["text"], n_thinking=counts["thinking"],
               n_tool_use=counts["tool_use"], n_tool_result=counts["tool_result"])
    rec["thinking_ms"] = _ms(t0, thinking_stop)
    rec["first_tool_ms"] = _ms(t0, first_tool)
    rec["first_text_ms"] = _ms(t0, first_text)
    rec["tool_wall_ms"] = tool_wall or None
    rec["stream_gaps_ms"] = gaps or None
    rec["thinking_to_tool_ms"] = (_ms(thinking_before_tool, first_tool)
                                  if thinking_before_tool and first_tool else None)
    return rec


def conversation_rows(conv: dict) -> list[dict]:
    """Timing rows for every message in one conversation.

    `chat_messages` is a flat list, so a regenerated or edited reply appears in
    it ALONGSIDE the reply it replaced. Each row is therefore tagged:

      branch      "active" | "superseded" | "" (no fork here)
      is_active   False for a superseded reply
      superseded_by / supersedes  the uuid of the other side of the fork

    A superseded row is still returned: its timings are real, and comparing them
    against the reply that replaced it is the point of keeping it.
    """
    tree = conv["tree"]
    msgs = tree.get("chat_messages") or []
    state = branch_state(tree)
    forks = branch_parents(tree)
    by_uuid = {m.get("uuid"): m for m in msgs if isinstance(m, dict) and m.get("uuid")}

    # For each forked child, record what replaced it / what it replaced.
    replaced_by: dict[str, str] = {}
    replaces: dict[str, str] = {}
    for parent, kids in forks.items():
        active = next((k for k in kids if state.get(k) == "active"), "")
        for k in kids:
            if k == active:
                continue
            replaced_by[k] = active
            if active:
                replaces[active] = k

    rows = []
    prev_human_end: Optional[datetime] = None
    for m in sorted(msgs, key=lambda x: x.get("index") or 0):
        rec = analyse_message(m)
        uid = m.get("uuid") or ""
        rec["conversation_uuid"] = tree.get("uuid") or conv["record"].get("conversationUuid")
        rec["conversation_name"] = tree.get("name") or ""
        rec["model"] = tree.get("model") or ""
        rec["message_uuid"] = uid
        if uid in state:
            # `stop_mode` keeps the TRUE stop classification; branch state is
            # orthogonal. A superseded hard_refusal is still a hard_refusal, and
            # overwriting the field would silently erase the refusal census.
            rec["branch"] = state[uid]
            rec["is_active"] = state[uid] == "active"
            rec["was"] = rec.get("stop_mode") or "normal"
            if state[uid] == "superseded":
                rec["replaced_by"] = replaced_by.get(uid, "")
                rec["superseded_by_created_at"] = (
                    (by_uuid.get(replaced_by.get(uid, "")) or {}).get("created_at") or "")
            else:
                rec["replaces"] = replaces.get(uid, "")
                rec["replaces_created_at"] = (
                    (by_uuid.get(replaces.get(uid, "")) or {}).get("created_at") or "")
        else:
            rec["branch"] = ""
            rec["is_active"] = True
        if m.get("sender") == "human":
            stops = [parse_ts(b.get("stop_timestamp")) for b in (m.get("content") or [])
                     if isinstance(b, dict)]
            prev_human_end = next((s for s in reversed(stops) if s), None)
        else:
            starts = [parse_ts(b.get("start_timestamp")) for b in (m.get("content") or [])
                      if isinstance(b, dict)]
            first = next((s for s in starts if s), None)
            # A floor, not a true wait: the human timestamp is written when the
            # client persists the turn, not when send was pressed.
            rec["prompt_to_response_ms"] = _ms(prev_human_end, first)
        rows.append(rec)
    return rows


def conversation_fingerprint(conv: dict) -> dict:
    """Cheap change-detector for one conversation.

    ``trees`` gets a new record on every write, so (message count, writtenAt) is
    enough to know whether anything needs re-reading.  ``writtenAt`` lives in the
    ``meta`` store (merged into ``conv["meta"]`` by ``_walk_conversations``), not
    in the tree record.
    """
    tree = conv["tree"]
    rec = conv["record"]
    meta = conv.get("meta") or {}
    msgs = tree.get("chat_messages") or []
    forks = branch_parents(tree)
    return {
        "uuid": tree.get("uuid") or rec.get("conversationUuid"),
        "name": tree.get("name") or "",
        "model": tree.get("model") or "",
        "n_messages": len(msgs),
        "n_branches": len(forks),
        "leaf": tree.get("current_leaf_message_uuid") or "",
        "written_at": meta.get("writtenAt") or rec.get("writtenAt") or 0,
        "updated_at": tree.get("updated_at") or "",
        "has_summary": bool(tree.get("summary")),
        "summary": tree.get("summary") or "",
        "is_archived": bool(tree.get("is_archived")),
        "thinking_mode": tree.get("effective_thinking_mode") or "",
        "source": conv.get("source") or "",
    }


def scan_all(on_error: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Every conversation from every store, richest copy per UUID (Desktop wins ties)."""
    merged: dict[str, dict] = {}
    for label, leveldb in indexeddb_stores():
        for conv in _walk_conversations(leveldb, on_error):
            fp = conversation_fingerprint(conv)
            uid = fp["uuid"]
            if not uid:
                continue
            conv["store_label"] = label
            prev = merged.get(uid)
            if prev is None or fp["n_messages"] > conversation_fingerprint(prev)["n_messages"]:
                if prev is not None:
                    conv["also_in"] = sorted(set((prev.get("also_in") or []) + [prev.get("store_label", "")]))
                merged[uid] = conv
            elif prev is not None:
                prev.setdefault("also_in", [])
                if label not in prev["also_in"]:
                    prev["also_in"].append(label)
    return list(merged.values())
