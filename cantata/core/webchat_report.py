"""Markdown renderer for the Chat & Cowork report."""

from __future__ import annotations

from .redaction import Redactor
from .report import clip, dt, dur, human_bytes, n, stamp, table
from .webchat import ChatThread, ExportArtifact, WebChatReport

_BADGE = {"cowork": "🤝 Cowork", "chat-local": "💬 Chat (local)", "code-tab": "🛠 Code tab", "export": "📦 Export",
          "browser": "🌐 claude.ai", "browser-project": "🗂 Project (web)", "browser-code": "☁ Code on web"}


def build_webchat_report(rep: WebChatReport, red: Redactor, version: str = "", include_exports: bool = True) -> str:
    st = rep.stats()
    L = ["# Claude Chat & Cowork — Local Archaeology Report", "",
         f"**Generated**: {stamp()}", f"**Tool**: Cantata {version}", f"**Claude Code dir**: `{red.path(str(rep.source_dir))}`",
         f"**Redaction level**: `{red.level}`", "",
         "> This report inventories what **Claude.ai Chat** and **Claude Desktop** leave on this machine: Cowork transcripts and "
         "audit logs, Code-tab wrappers, browser traces and export archives. Chat *bodies* live on Anthropic's servers — "
         "only what was synced locally (Cowork) or downloaded (exports) is recoverable here.", "", "---", ""]

    L += ["## Headlines", ""]
    L += [f"- **Threads catalogued**: {st['threads']} ({st['recoverable']} with a recoverable body, {st['server_only']} server-side only)",
          f"- **Cowork / local-chat sessions**: {st['cowork']} · **Code-tab wrappers**: {st['code_tab']} · with local transcript: {st['with_transcript']}",
          f"- **Browser profiles with claude.ai data**: {st['browsers']} ({st['browser_visits']} history visits, {st['indexeddb_kb']:,} KB IndexedDB)"
          + (f" · ⚠ {st['extension_stores']} Claude-in-Chrome token store(s)" if st["extension_stores"] else ""),
          f"- **Export archives triaged**: {st['exports']} ({st['export_messages']:,} messages"
          + (f", {st['export_pii_files']} contain PII markers" if st["export_pii_files"] else "") + ")"]
    if st.get("indexeddb_threads"):
        L.append(f"- **Recoverable from a local IndexedDB cache**: {st['indexeddb_threads']} conversation(s) "
                 "(Claude Desktop / Chromium browser)")
    if st.get("visited"):
        gap = st["visited_missing"]
        L.append(f"- **Coverage vs. what you opened**: {st['visited_recoverable']} of {st['visited']} conversations seen in "
                 f"browser history have a local body"
                 + (f" — **{gap} are server-side only**" if gap else " (complete)"))
    if st["cowork_cost_usd"]:
        L.append(f"- **Cowork spend recorded in audit logs**: ${st['cowork_cost_usd']:.2f}")
    L += ["", "---", ""]

    # Per-source cache state. A store that exists but holds zero records is the signature of a
    # signed-out / account-switched session: Chromium recreates the LevelDB empty on next launch.
    if rep.idb_sources:
        L += ["## IndexedDB cache state", "",
              "| Source | Records | With a body | Path |", "|---|---|---|---|"]
        for label, path, n_rec, n_body in rep.idb_sources:
            mark = "⚠ **empty**" if n_rec == 0 else str(n_rec)
            L.append(f"| {label} | {mark} | {n_body} | `{red.path(path)}` |")
        empties = [s[0] for s in rep.idb_sources if s[2] == 0]
        if empties:
            L += ["", f"> ⚠ **{', '.join(empties)}** holds a `claude-conversation-store` database with **no conversation "
                  "records**. This is what a signed-out or account-switched claude.ai session leaves behind: Chromium "
                  "re-creates the store on next launch and only re-populates it as conversations are opened. "
                  "The conversation bodies were never on disk for this account, so nothing was lost by the extraction."]
        L += ["", "---", ""]

    L += ["## How Claude marks data locally", "", "| Artifact | Where | What it holds | Recoverable body? |", "|---|---|---|---|",
          "| Cowork manifest | `<Claude>/local-agent-mode-sessions/<acct>/<org>/local_<uuid>.json` | title, model, folders, `cliSessionId`, **account name + e-mail** | No — index |",
          "| Cowork transcript | `…/local_<uuid>/.claude/projects/-sessions-<name>/<cliSessionId>.jsonl` | full turns, tool calls, tokens | **Yes** |",
          "| Cowork audit log | `…/local_<uuid>/audit.jsonl` (HMAC-chained) | events, permission decisions, **cost & duration** | Partly |",
          "| Code-tab wrapper | `<Claude>/claude-code-sessions/<acct>/<org>/local_<uuid>.json` | cwd, worktree, title, `cliSessionId` | No — points to `~/.claude/projects` |",
          "| Cowork memory | `…/memory/` · `…/spaces/<id>/memory/` | Markdown notes Claude keeps about you | **Yes** |",
          "| Browser history | `<browser>/<profile>/History` | `claude.ai/chat/<uuid>` visits + titles | No — metadata |",
          "| IndexedDB `claude-conversation-store` | `<profile>/IndexedDB/https_claude.ai_0…` — Desktop **and** Chromium browsers | **full cached conversation trees + message bodies** (LevelDB) | **Yes** — via `ccl_chromium_reader` |",
          "| Local Storage / Service Worker | `<profile>/Local Storage`, `…/Service Worker` | app state (presence, flags) | No — metadata |",
          "| Claude in Chrome | `<profile>/Local Extension Settings/fcoeoabg…` | **plaintext OAuth/API tokens** | — (never read) |",
          "| Export archive | wherever you saved it (`.zip`) | `conversations.json`, `projects.json`, `users.json` | **Yes** |", "", "---", ""]

    L += ["## Thread Catalog", "", f"_{len(rep.threads)} threads, newest first._", ""]
    rows = []
    for t in rep.threads:
        rows.append([f"`{red.session_id(t.thread_id)}`", _BADGE.get(t.source_kind, t.source_kind), clip(red.apply(t.title), 46),
                     dt(t.updated_at or t.created_at), t.body_location,
                     f"{t.turns}/{t.messages}" if t.local_transcript else (str(t.export_messages) if t.in_export else "-"),
                     str(t.browser_visits) if t.browser_visits else "-"])
    L += table(["Thread", "Origin", "Title", "Last active", "Body", "Turns/Msgs", "Visits"], rows) if rows else ["_(no threads found)_"]
    L += ["", "---", ""]

    rec = [t for t in rep.threads if t.local_transcript]
    if rec:
        L += ["## Conversations Recoverable Locally", "",
              "_Use the **Recover** page (Cowork is included by default) for the full transcript; Code-tab sessions live in `~/.claude/projects`._", ""]
        L += table(["Thread", "Origin", "Title", "Model", "Transcript", "Turns", "Tools", "Cost"],
                   [[f"`{red.session_id(t.thread_id)}`", _BADGE.get(t.source_kind, ""), clip(red.apply(t.title), 40), t.model or "-",
                     f"`{red.path(t.local_transcript).replace(chr(92), '/').split('/')[-1][:30]}`", t.turns, t.tools,
                     f"${t.cost_usd:.2f}" if t.cost_usd else "-"] for t in rec]) + ["", "---", ""]

    d = rep.desktop
    L += ["## Claude Desktop", ""]
    if d and d.found:
        L += [f"- **Data directories**: " + "; ".join(f"`{red.path(v['path'])}` ({v['flavour']})" for v in d.variants)]
        s = d.stats()
        L += [f"- **Sessions**: {s['cowork_sessions']} Cowork/chat · {s['code_tab_sessions']} Code-tab · **audit logs**: {s['audit_logs']} · "
              f"**memory files**: {s['memory_files']} · **extensions**: {s['extensions']}"]
        if d.output_dirs:
            L.append("- **User-visible outputs**: " + ", ".join(f"`{red.path(p)}`" for p in d.output_dirs))
        for c in d.configs:
            if c.get("mcp_servers"):
                L.append(f"- **MCP servers** (`claude_desktop_config.json`): " + ", ".join(f"`{m['name']}` (env keys: {', '.join(m['env_keys']) or '-'})" for m in c["mcp_servers"]))
            if c.get("config_json_keys"):
                L.append(f"- **`config.json` keys** (values hidden; may hold encrypted tokens): " + ", ".join(f"`{k}`" for k in c["config_json_keys"]))
        if d.vm_entries:
            L.append("- **VM / binaries**: " + ", ".join(f"`{v['name']}` ({v['entries']} entries)" for v in d.vm_entries))
        if d.logs:
            L.append("- **Logs**: " + ", ".join(f"`{l['name']}` {human_bytes(l['bytes'])}" for l in d.logs[:14]))
        L.append("")
        cw = [x for x in d.sessions if x.kind in ("cowork", "chat-local")]
        if cw:
            L += ["### Cowork sessions", ""]
            L += table(["Session", "Title", "Model", "Folders", "Account", "Transcripts", "Audit", "Uploads/Outputs", "Error"],
                       [[f"`{red.session_id(x.local_id.replace('local_', ''))}`", clip(red.apply(x.title), 34), x.model or "-",
                         clip(", ".join(red.path(f) for f in x.folders), 30) or "-", red.person(x.email or x.account_name) if (x.email or x.account_name) else "-",
                         len(x.transcripts), f"{x.audit.get('lines', 0)} lines" if x.audit_path else "-", f"{x.uploads}/{x.outputs}",
                         clip(red.apply(x.error), 40) or "-"] for x in cw]) + [""]
        if d.memory:
            L += ["### Cowork memory", ""] + [f"- {m['scope']}: " + ", ".join(f"`{f}`" for f in m["files"][:8]) for m in d.memory] + [""]
    else:
        L += ["_(no Claude Desktop data directory found)_", ""]
    L += ["---", ""]

    L += ["## Browser Traces", ""]
    if rep.browsers and any(b.history_hits or b.indexeddb_bytes or b.extension_bytes for b in rep.browsers):
        rows = []
        for b in rep.browsers:
            if not (b.history_hits or b.indexeddb_bytes or b.extension_bytes):
                continue
            rows.append([f"{b.browser} / {b.profile}", b.history_hits, ", ".join(f"{k}×{v}" for k, v in b.kinds.most_common(4)) or "-",
                         dt(b.last_visit), human_bytes(b.indexeddb_bytes) if b.indexeddb_bytes else "-",
                         "✓" if b.local_storage_hit else "-", human_bytes(b.extension_bytes) if b.extension_bytes else "-"])
        L += table(["Profile", "History visits", "Page kinds", "Last visit", "IndexedDB", "Local Storage", "Claude-in-Chrome store"], rows)
        L += ["", "_IndexedDB / Local Storage hold claude.ai's offline cache in snappy-compressed LevelDB — titles and fragments may exist, "
                  "but they are not plaintext-recoverable without the browser. History rows confirm **which** conversations you opened and when._"]
    else:
        L += ["_(no claude.ai data found in Chromium profiles)_"]

    # Conversations whose full body was cached in a local claude.ai IndexedDB
    idb_threads = [t for t in rep.threads if t.in_indexeddb]
    if idb_threads:
        L += ["", "---", "", "## IndexedDB Conversations (recoverable bodies)", ""]
        L += ["_Full conversation trees cached in the local `claude-conversation-store` — Claude Desktop **and** Chromium "
              "browsers are scanned. The extractor rebuilds each as redacted Markdown (parsed with `ccl_chromium_reader`)._", ""]
        L += table(["Thread", "Origin", "Title", "Model", "Messages", "Cached in", "Last Updated"],
                   [[f"`{red.session_id(t.thread_id)}`", _BADGE.get(t.source_kind, t.source_kind), clip(red.apply(t.title), 46),
                     t.model or "-", str(t.messageCount) if t.messageCount else "-", t.indexeddb_source or "-",
                     dt(t.updated_at)] for t in idb_threads])

    # The complement: conversations you demonstrably opened (browser history) but whose body is
    # not on this machine any more. Without this the report reads as if the extraction failed.
    missing = [t for t in rep.threads if t.browser_visits and t.catalog_only]
    if missing:
        L += ["", "---", "", "## Opened but server-side only", "",
              f"_{len(missing)} conversation(s) appear in browser history (so they were opened here) but no local copy "
              "of the body survives. These are recoverable only from the server: re-open each in the browser, or import "
              "a Claude export archive._", ""]
        L += table(["Thread", "Title", "Visits", "Last opened"],
                   [[f"`{red.session_id(t.thread_id)}`", clip(red.apply(t.title), 60) or "_(untitled)_",
                     t.browser_visits, dt(t.updated_at)] for t in missing[:200]])
        if len(missing) > 200:
            L.append(f"\n_… and {len(missing) - 200} more._")
    L += ["", "---", ""]

    if include_exports and rep.exports:
        L += ["## Export Archives", ""]
        for a in rep.exports:
            _export_block(a, L, red)
        L += ["---", ""]

    L += ["## Warnings", ""] + ([f"- {red.apply(w)}" for w in rep.warnings[:100]] or ["_(none)_"]) + ["", "---", "",
          f"_Report generated locally by Cantata {version} — no data left this machine. Chat bodies remain on Anthropic's servers; "
          "this report covers the local footprint only._", ""]
    return "\n".join(L).rstrip() + "\n"


def _export_block(a: ExportArtifact, L: list[str], red: Redactor) -> None:
    L += [f"### `{a.path.name}`", "", f"- **Path**: `{red.path(str(a.path))}`", f"- **Kind**: {a.kind} · **Size**: {a.size_bytes:,} B"]
    if a.sha256:
        L.append(f"- **SHA-256**: `{a.sha256[:16]}…`")
    if a.error:
        L.append(f"- **Error**: {red.apply(a.error)}")
    if a.conversations or a.messages:
        L.append(f"- **Contents**: {a.conversations} conversations · {a.messages:,} messages · {a.attachments} attachments/files · {a.tool_uses} tool uses"
                 + (f" · {a.projects} projects ({a.project_docs} docs)" if a.projects else ""))
    if a.first:
        L.append(f"- **Date range**: {dt(a.first)} → {dt(a.last)}")
    if a.models:
        L.append("- **Models**: " + ", ".join(f"`{m}`×{c}" for m, c in a.models.most_common(6)))
    if a.pii_hits:
        L.append("- **⚠ PII / secret markers inside**: " + ", ".join(f"{k}×{v}" for k, v in sorted(a.pii_hits.items())) + " (counts only — values not shown)")
    if a.extracted:
        L.append(f"- **Reconstructed**: {a.extracted} redacted Markdown file(s) in `{red.path(a.extract_dir)}`")
    if a.chat_thread_refs:
        L.append("- **Conversation ids**: " + ", ".join(f"`{red.session_id(r)}`" for r in a.chat_thread_refs[:10]) + (" …" if len(a.chat_thread_refs) > 10 else ""))
    if a.entries:
        L += ["- **Entries**:"] + [f"  - `{red.apply(clip(e, 90))}`" for e in a.entries[:12]]
        if len(a.entries) > 12:
            L.append(f"  - … {len(a.entries) - 12} more")
    L.append("")
