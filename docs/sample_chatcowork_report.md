# Claude Chat & Cowork — Local Archaeology Report

**Generated**: 2026-09-30 10:47 UTC
**Tool**: Cantata 0.1.0
**Claude Code dir**: `/tmp/cantata_demo_rq8uy05g/home/.claude`
**Redaction level**: `standard`

> This report inventories what **Claude.ai Chat** and **Claude Desktop** leave on this machine: Cowork transcripts and audit logs, Code-tab wrappers, browser traces and export archives. Chat *bodies* live on Anthropic's servers — only what was synced locally (Cowork) or downloaded (exports) is recoverable here.

---

## Headlines

- **Threads catalogued**: 8 (4 with a recoverable body, 4 server-side only)
- **Cowork / local-chat sessions**: 2 · **Code-tab wrappers**: 2 · with local transcript: 2
- **Browser profiles with claude.ai data**: 1 (27 history visits, 2 KB IndexedDB) · ⚠ 1 Claude-in-Chrome token store(s)
- **Export archives triaged**: 2 (6 messages, 1 contain PII markers)
- **Cowork spend recorded in audit logs**: $8.64

---

## How Claude marks data locally

| Artifact | Where | What it holds | Recoverable body? |
|---|---|---|---|
| Cowork manifest | `<Claude>/local-agent-mode-sessions/<acct>/<org>/local_<uuid>.json` | title, model, folders, `cliSessionId`, **account name + e-mail** | No — index |
| Cowork transcript | `…/local_<uuid>/.claude/projects/-sessions-<name>/<cliSessionId>.jsonl` | full turns, tool calls, tokens | **Yes** |
| Cowork audit log | `…/local_<uuid>/audit.jsonl` (HMAC-chained) | events, permission decisions, **cost & duration** | Partly |
| Code-tab wrapper | `<Claude>/claude-code-sessions/<acct>/<org>/local_<uuid>.json` | cwd, worktree, title, `cliSessionId` | No — points to `~/.claude/projects` |
| Cowork memory | `…/memory/` · `…/spaces/<id>/memory/` | Markdown notes Claude keeps about you | **Yes** |
| Browser history | `<browser>/<profile>/History` | `claude.ai/chat/<uuid>` visits + titles | No — metadata |
| IndexedDB / Local Storage | `<profile>/IndexedDB/https_claude.ai_0…` | web-app cache (snappy LevelDB) | No — binary |
| Claude in Chrome | `<profile>/Local Extension Settings/fcoeoabg…` | **plaintext OAuth/API tokens** | — (never read) |
| Export archive | wherever you saved it (`.zip`) | `conversations.json`, `projects.json`, `users.json` | **Yes** |

---

## Thread Catalog

_8 threads, newest first._

| Thread | Origin | Title | Last active | Body | Turns/Msgs | Visits |
|---|---|---|---|---|---|---|
| `c0de0000` | 🛠 Code tab | Login bug (desktop) | 2026-09-30 08:47 | local transcript | 4/12 | - |
| `deadbeef` | 🤝 Cowork | Failed start | 2026-09-29 10:47 | server only | - | - |
| `7876b7f6` | 🤝 Cowork | Review customer persona research | 2026-09-28 11:01 | local transcript | 2/5 | - |
| `d1111111` | 🗂 Project (web) | Research notes | 2026-09-28 10:47 | server only | - | 2 |
| `c1111111` | 🌐 claude.ai | Trip itinerary ideas | 2026-09-26 10:47 | server only | - | 3 |
| `b1111111` | 📦 Export | Recipe ideas | 2026-09-21 10:52 | export archive | 2 | - |
| `a1111111` | 📦 Export | Quarterly planning | 2026-09-20 11:47 | export archive | 2 | 7 |
| `c0de0000` | 🛠 Code tab | Old refactor (transcript swept) | 2026-08-22 10:47 | server only | - | - |

---

## Conversations Recoverable Locally

_Use the **Recover** page (Cowork is included by default) for the full transcript; Code-tab sessions live in `~/.claude/projects`._

| Thread | Origin | Title | Model | Transcript | Turns | Tools | Cost |
|---|---|---|---|---|---|---|---|
| `c0de0000` | 🛠 Code tab | Login bug (desktop) | claude-opus-4-5 | `11111111-aaaa-4bbb-8ccc-000000` | 4 | 3 | - |
| `7876b7f6` | 🤝 Cowork | Review customer persona research | claude-opus-4-5 | `4368fcb8-373d-4ba7-9952-3beee2` | 2 | 1 | $8.64 |

---

## Claude Desktop

- **Data directories**: `/tmp/cantata_demo_rq8uy05g/wc/desktop/Claude` (standard)
- **Sessions**: 2 Cowork/chat · 2 Code-tab · **audit logs**: 1 · **memory files**: 3 · **extensions**: 1
- **MCP servers** (`claude_desktop_config.json`): `filesystem` (env keys: API_TOKEN)
- **`config.json` keys** (values hidden; may hold encrypted tokens): `locale`, `oauth:tokenCache`, `theme`
- **VM / binaries**: `vm_bundles` (1 entries)
- **Logs**: `main.log` 149 B, `mcp.log` 35 B

### Cowork sessions

| Session | Title | Model | Folders | Account | Transcripts | Audit | Uploads/Outputs | Error |
|---|---|---|---|---|---|---|---|---|
| `deadbeef` | Failed start | claude-opus-4-5 | - | [EMAIL] | 0 | - | 0/0 | RPC error -1: failed to ensure virtiofs… |
| `7876b7f6` | Review customer persona research | claude-opus-4-5 | /home/[USER]/Documents/reports | [EMAIL] | 2 | 5 lines | 1/1 | - |

### Cowork memory

- project: `project_notes.md`
- account: `CLAUDE.md`, `memory/preferences.md`

---

## Browser Traces

| Profile | History visits | Page kinds | Last visit | IndexedDB | Local Storage | Claude-in-Chrome store |
|---|---|---|---|---|---|---|
| Chromium / Default | 27 | new×11, chat×10, recents×4, project×2 | 2026-09-30 10:47 | 2.1 KB | ✓ | 128 B |

_IndexedDB / Local Storage hold claude.ai's offline cache in snappy-compressed LevelDB — titles and fragments may exist, but they are not plaintext-recoverable without the browser. History rows confirm **which** conversations you opened and when._

---

## Export Archives

### `claude-export.zip`

- **Path**: `/tmp/cantata_demo_rq8uy05g/wc/exports/claude-export.zip`
- **Kind**: claude-export-zip · **Size**: 1,462 B
- **SHA-256**: `5059fab1b68edda3…`
- **Contents**: 2 conversations · 4 messages · 1 attachments/files · 1 tool uses · 1 projects (1 docs)
- **Date range**: 2026-09-20 10:47 → 2026-09-21 10:52
- **Models**: `claude-opus-4-5`×1
- **⚠ PII / secret markers inside**: anthropic-key×1, email×4, phone×1 (counts only — values not shown)
- **Reconstructed**: 3 redacted Markdown file(s) in `/tmp/cantata_demo_rq8uy05g/out/Cantata_chats/export_01_claude-export`
- **Conversation ids**: `a1111111`, `b1111111`
- **Entries**:
  - `conversations.json`
  - `projects.json`
  - `users.json`

### `single-conversation.json`

- **Path**: `/tmp/cantata_demo_rq8uy05g/wc/exports/single-conversation.json`
- **Kind**: single-conversation-json · **Size**: 1,215 B
- **SHA-256**: `df5563fdc8fc8cc1…`
- **Contents**: 1 conversations · 2 messages · 0 attachments/files · 0 tool uses
- **Date range**: 2026-09-21 10:47 → 2026-09-21 10:52
- **Reconstructed**: 1 redacted Markdown file(s) in `/tmp/cantata_demo_rq8uy05g/out/Cantata_chats/export_02_single-conversation`
- **Conversation ids**: `b1111111`

---

## Warnings

- Code-tab wrapper local_c0de0000 points to transcript 99999999 which is not on disk (swept or deleted).
- Chromium/Default: the Claude in Chrome extension keeps OAuth/API tokens unencrypted in its LevelDB store (128 bytes) - contents were NOT read.

---

_Report generated locally by Cantata 0.1.0 — no data left this machine. Chat bodies remain on Anthropic's servers; this report covers the local footprint only._
