# How Claude stores data on disk

*Research notes behind Cantata — compiled 2026-09-30 from Anthropic's own docs, binary
inspection reports, GitHub issues and community reverse-engineering. Everything here is
about what lands on **your** machine; Anthropic-side retention is a different question.*

**Confidence legend** — every claim below is tagged:

| Tag | Meaning |
|---|---|
| ✅ **doc** | Stated in Anthropic's official documentation |
| 🔎 **seen** | Reproduced by several independent reports / read out of the shipped binary — not officially documented, can change |
| ❓ **guess** | Single source, inferred, or extrapolated. Cantata parses it defensively and never depends on it |

---

## 0. The product map (why there are four places to look)

| Product | Where the conversation *body* lives | Cantata module |
|---|---|---|
| **Claude Code** — terminal CLI, VS Code / JetBrains extension, Agent SDK, Desktop **Code** tab | Local, plaintext JSONL: `~/.claude/projects/…` ✅ | `transcripts.py`, `stores.py`, `configs.py` |
| **Claude Desktop → Cowork** (local agent mode) | Local, plaintext JSONL *inside the Desktop app-data dir* (not `~/.claude`) 🔎 | `desktop.py` |
| **Claude Desktop → Chat**, **claude.ai**, mobile | **Server-side.** Only a footprint is local (browser cache, history rows, exports you saved). Exception: Desktop on *third-party* inference (3P) keeps Chat locally too ✅ | `webchat.py` |
| **Claude API** | Nowhere (stateless) — whatever your own app logs | n/a |

The Codex analogy: Claude Code ≈ Codex rollouts, Desktop/Cowork ≈ Codex Desktop bridge,
claude.ai ≈ chatgpt.com "Chat & Work". Cantata's **Chat & Cowork** page is the third row.

---

## 1. Claude Code — `~/.claude`

### 1.1 Where is "home"?

* Default `~/.claude` (Windows: `%USERPROFILE%\.claude`) ✅
* `CLAUDE_CONFIG_DIR` relocates **every** `~/.claude` path ✅ (docs). The global state file then
  lives inside it (`<dir>/.claude.json`) 🔎; otherwise it is `~/.claude.json` (a sibling, **not**
  inside `~/.claude/`) ✅.
* Managed (enterprise) settings: `/etc/claude-code/managed-settings.json` (Linux),
  `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS),
  `C:\Program Files\ClaudeCode\managed-settings.json` (Windows; older builds `C:\ProgramData\ClaudeCode`) 🔎
* Keychain (macOS): service `Claude Code-credentials`; with `CLAUDE_CONFIG_DIR` set a SHA-256 suffix is appended 🔎

### 1.2 Retention — the single most important forensic fact

Claude Code **deletes** the files marked "cleaned up" below once they are older than
`cleanupPeriodDays` — **default 30, minimum 1** (`0` is a validation error). ✅ doc.
A transcript that has aged out is gone; only `history.jsonl`, `stats-cache.json`,
`sessions-index.json` rows and the `~/.claude.json` per-project stats may still *mention* it.
Cantata calls those **index-only sessions** (the analogue of DeKodX's "catalog-only" threads).

### 1.3 Directory map

| Path under `~/.claude/` | Contents | Lifetime | Tag |
|---|---|---|---|
| `projects/<project>/<session>.jsonl` | Full transcript: every message, tool call, tool result | swept after `cleanupPeriodDays` | ✅ |
| `projects/<project>/<session>.orphaned-<ts>-<suffix>.jsonl` | Earlier transcript Claude Code set aside instead of overwriting | swept | ✅ |
| `projects/<project>/<session>.jsonl.superseded-<ts>` | Same idea (renamed copy) | swept | ✅ |
| `projects/<project>/<session>/subagents/agent-<id>.jsonl` | Subagent (Task/Agent tool) transcripts; `agent-<id>.meta.json` beside it | removed with parent | ✅ / 🔎 |
| `projects/<project>/agent-<id>.jsonl` | Older layout: subagent file next to the parent, lines carry the parent `sessionId` | swept | 🔎 |
| `projects/<project>/<session>/tool-results/` | Large tool outputs spilled to files; full-size copies of MCP images | swept | ✅ |
| `projects/<project>/memory/` | **Auto memory** — `MEMORY.md` + topic notes Claude writes to itself | kept | ✅ |
| `projects/<project>/sessions-index.json` | `--resume` picker index (unreliable — not written for `-p` runs, repairs needed after crashes) | kept | 🔎 |
| `history.jsonl` | Every prompt typed: `display`, `pastedContents`, `timestamp` (ms), `project`, `sessionId` | **kept until you delete** | ✅ / 🔎 |
| `stats-cache.json` | Aggregates behind `/usage` (see §1.6) | kept | ✅ |
| `file-history/<session>/<hash>@v<n>` | Pre-edit file snapshots for checkpoint restore (latest 100 checkpoints) | swept | ✅ |
| `plans/*.md` | Plan-mode documents (random three-word names) | swept | ✅ |
| `tasks/<list>/…json` | Task lists from the task tools | swept | ✅ |
| `todos/<session>-agent-<agent>.json` | Legacy TodoWrite state — *no longer written* | legacy | ✅ |
| `session-env/<session>/` | Per-session environment metadata | swept | ✅ |
| `shell-snapshots/snapshot-<shell>-<ts>-<rand>.sh` | Aliases/functions/options captured for the Bash tool; removed on clean exit | swept | ✅ |
| `debug/<session>.txt` (+ `latest`) | Debug log, only with `--debug` or `/debug` | swept | ✅ |
| `paste-cache/`, `image-cache/<session>/` | Big pastes; attached images (≤ v2.1.274; later versions use `CLAUDE_CODE_TMPDIR`) | swept | ✅ |
| `uploads/<session>/` | Files attached via web/mobile to a Remote Control session | swept | ✅ |
| `backups/.claude.json.backup.<ts>` | 5 newest copies of `~/.claude.json` + any unparseable version | rotating | ✅ |
| `usage-data/` | `/insights` reports + cached per-session analysis (`session-meta/*.json`, `facets/`) | kept | ✅ / ❓ |
| `feedback-bundles/`, `feedback/drafts/` | Redacted transcript archives / drafts from `/feedback` | swept | ✅ |
| `remote-settings.json`, `policy-limits.json(.stamp.json)`, `cache/changelog.md` | Server-managed settings / policy caches | refreshed | ✅ |
| `statsig/`, `logs/` | Legacy; no longer written | legacy | ✅ |
| `telemetry/1p_failed_events.<session>.<uuid>.json` | Queue of first-party telemetry events that failed to upload (contain event payloads) | until sent | 🔎 |
| `ide/<port>.lock` | IDE-integration lock files: `pid`, `workspaceFolders`, `ideName`, `transport`, **`authToken`** | until IDE exits | 🔎 |
| `.credentials.json` | `{"claudeAiOauth":{accessToken, refreshToken, expiresAt, scopes, subscriptionType, rateLimitTier}}` — Linux/Windows only; macOS uses the Keychain | until logout | 🔎 |
| `plugins/` (`installed_plugins.json`, `known_marketplaces.json`, `marketplaces/`, `cache/`, `synced/`, `.trash/`) | Plugin registry & installs | kept | ✅ |
| `settings.json`, `keybindings.json`, `themes/*.json`, `CLAUDE.md`, `rules/`, `skills/`, `commands/`, `agents/`, `output-styles/`, `workflows/`, `agent-memory/` | **Authored** configuration | kept | ✅ |
| `chrome/chrome-native-host` | Native-messaging host for *Claude in Chrome* | kept | 🔎 |

Other locations outside `~/.claude`:

| Path | Contents | Tag |
|---|---|---|
| `~/.claude.json` | App state, OAuth **metadata** (no tokens), per-project trust & last-session stats, personal MCP servers, tips/feature-flag caches | ✅ |
| `<project>/.claude/settings.json`, `settings.local.json`, `<project>/CLAUDE.md`, `CLAUDE.local.md`, `.mcp.json` | Project scope config | ✅ |
| `~/.cache/claude-cli-nodejs/<slug>/{mcp-logs-<server>,errors,messages}/*.jsonl` (Linux) · `~/Library/Caches/claude-cli-nodejs/…` (macOS) · `%LOCALAPPDATA%\claude-cli-nodejs\Cache\<slug>\…` (Windows) | MCP-server logs and error/message diagnostics, per project slug; swept only for the *current* cwd | 🔎 |
| `$CLAUDE_CODE_TMPDIR` (default OS temp) `…/claude-<uid>/…` | Background-task output, pasted images (new versions) | ❓ |

### 1.4 Project folder naming (read out of the 2.1.2xx binaries) 🔎

```js
function sanitize(p) { return p.replace(/[^a-zA-Z0-9]/g, "-") }       // per UTF-16 code unit
function dirName(p) {
  const n = sanitize(p);
  return n.length <= 200 ? n : `${n.slice(0, 200)}-${Math.abs(javaHash32(p)).toString(36)}`;
}                                                                    // javaHash32: h = (h<<5)-h+charCode, 32-bit
```

`/home/me/my_app` → `-home-me-my-app`; `C:\Users\me\proj` → `C--Users-me-proj`; an emoji (2 UTF-16 units) → `--`.
The mapping is lossy, so Cantata reads the **real** `cwd` from the JSONL lines and only falls back to a
best-effort decode. Implemented and unit-tested in `cantata/core/paths.py::encode_project_dir`.

### 1.5 Transcript JSONL schema 🔎 (undocumented, drifts every few releases)

One JSON object per line, **append-only**, filename = session UUID (or `agent-<id>`).
Top-level `type` values seen in 2026 builds: `user`, `assistant`, `system`, `summary` (legacy),
`queue-operation`, `last-prompt`, `progress`, `attachment`, `file-history-snapshot`,
`permission-mode`, `custom-title`, `ai-title`, `agent-name`, `pr-link`. Cantata counts every unknown
type instead of failing.

Common envelope: `uuid`, `parentUuid` (linked list / tree — forks after rewind), `timestamp` (ISO-8601 Z),
`sessionId`, `cwd`, `gitBranch`, `version` (Claude Code release), `entrypoint` (`cli`, `sdk-cli`,
`claude-vscode`, `claude-desktop` …❓), `userType`, `isSidechain`, `slug` (three-word name), `promptId`,
`permissionMode`, `requestId`, `isMeta`, `isCompactSummary`, `isApiErrorMessage`, `agentId`.

* **`user`** — `message.content` is a *string* (a typed prompt) **or** a list; a list containing
  `tool_result` blocks is the tool answer, with a richer top-level `toolUseResult`
  (`stdout/stderr/interrupted`, `file{…}`, `structuredPatch`, `durationMs`, and for subagents
  `agentId`, `totalDurationMs`, `totalTokens`, `totalToolUseCount`, `usage`, `toolStats`).
* **`assistant`** — the raw API message: `message.{id, model, content[], stop_reason, usage}`.
  **One API response is written as several lines** (one per content block: `thinking`, `text`,
  `tool_use`) that all repeat the same `message.id`/`requestId` **and the same `usage`** — sum tokens
  only once per `message.id` or you over-count by 2–5×. Model `<synthetic>` + `isApiErrorMessage` = a local error placeholder.
  `usage` = `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
  `cache_creation{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, `server_tool_use{web_search_requests, web_fetch_requests}`,
  `service_tier`, `speed` (`standard`/`fast`), `inference_geo`, `iterations`.
* **`system`** — `subtype` ∈ `compact_boundary` (`compactMetadata{trigger, preTokens}`), `turn_duration` (`durationMs`),
  `api_error`, `local_command`, `stop_hook_summary`, `informational`, `microcompact_boundary` …
* **`queue-operation`** — `enqueue/dequeue/remove` + `content`: prompts you typed **while Claude was busy**.
* **`file-history-snapshot`** — `snapshot.trackedFileBackups{path → {backupFileName, version, backupTime}}` → which files were touched.
* **`custom-title` / `ai-title` / `agent-name` / `last-prompt` / `permission-mode` / `pr-link`** — bookkeeping
  (titles are re-written many times; last one wins). Known bug: with several sessions open in one
  project, lines can be written into a *sibling* session's file (anthropics/claude-code#26964) — Cantata
  therefore trusts the per-line `sessionId` over the filename and reports mismatches.
* **Compaction** — `isCompactSummary:true` user line holds the generated summary; `compact_boundary` precedes it.

`sessions-index.json` 🔎: `{"version":1,"entries":[{sessionId, fullPath, fileMtime(ms), firstPrompt, summary, messageCount, created, modified, gitBranch, projectPath, isSidechain}],"originalPath":…}`.

`stats-cache.json` ✅/🔎: `{version, lastComputedDate, dailyActivity[{date,messageCount,sessionCount,toolCallCount}],
dailyModelTokens[{date,tokensByModel}], modelUsage{model→{inputTokens,outputTokens,cacheReadInputTokens,cacheCreationInputTokens,webSearchRequests,costUSD,contextWindow}},
totalSessions, totalMessages, longestSession{sessionId,duration,messageCount,timestamp}, firstSessionDate, hourCounts{hour→n}}` —
rebuilt from the transcripts, so **totals drop retroactively when old sessions are swept**.

### 1.6 `~/.claude.json` (global state) ✅/🔎

`numStartups`, `installMethod`, `autoUpdates`, `theme`, `userID` (random id), `firstStartTime`,
`oauthAccount{accountUuid, emailAddress, organizationUuid, organizationName, organizationRole, displayName}` (**PII; no tokens**),
`mcpServers` (user scope — `env`/`headers` may hold secrets), `projects{<abs path>: {allowedTools, hasTrustDialogAccepted,
mcpServers, enabledMcpjsonServers, lastSessionId, lastCost, lastAPIDuration, lastDuration, lastLinesAdded, lastLinesRemoved,
lastTotalInputTokens, lastTotalOutputTokens, lastTotalCacheCreationInputTokens, lastTotalCacheReadInputTokens, lastModelUsage{model→…}}}`,
`tipsHistory`, `cachedGrowthBookFeatures`, `cachedStatsigGates`, `customApiKeyResponses`, legacy per-project `history[]`.
`lastAPIDuration` / `lastDuration` are app-measured milliseconds for the *last* session in that project — a cheap sanity check for derived latency.

---

## 2. Claude Desktop

### 2.1 App-data directory

| OS | Path | Tag |
|---|---|---|
| Windows (classic installer) | `%APPDATA%\Claude\` | ✅ |
| Windows (MSIX / self-updated → packaged) | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\` — **`%APPDATA%\Claude` may not exist or may be stale** | 🔎 |
| Windows, third-party inference mode | `%LOCALAPPDATA%\Claude-3p\` (earlier: `%APPDATA%\Claude-3p\`) | ✅ |
| macOS | `~/Library/Application Support/Claude/` (3P: `Claude-3p/`); logs `~/Library/Logs/Claude/` | ✅ |
| Linux (community builds) | `~/.config/Claude/` (3P: `Claude-3p/`) | ✅ / ❓ |
| User-visible outputs (Artifacts, scheduled-task results) | `~/Claude/` (legacy `~/Documents/Claude/`) | ✅ |

### 2.2 Inside the app-data directory

| Path | Contents | Tag |
|---|---|---|
| `claude_desktop_config.json` | `mcpServers{name→{command,args,env}}` (+ UI preferences) — `env` holds secrets | ✅ |
| `config.json` | UI prefs (scale, theme, window) plus cached OAuth blobs — **encrypted** via DPAPI / Keychain (`safeStorage`) | 🔎 |
| `ant-did` | Random device id (base64 UUID) | ✅ |
| `local-agent-mode-sessions/<accountId>/<orgId>/local_<uuid>.json` | **Cowork** session manifest (≈27 fields: `sessionId`, `cliSessionId`, `processName`, `cwd`, `userSelectedFolders`, `createdAt`/`lastActivityAt` (epoch ms), `model`, `title`, `initialMessage`, `systemPrompt` (~42 KB), `accountName`, **`emailAddress`**, `enabledMcpTools`, `remoteMcpServersConfig`, `egressAllowedDomains`, `error`, `scheduledTaskId` …) | 🔎 |
| `…/local_<uuid>/.claude/projects/-sessions-<processName>/<cliSessionId>.jsonl` | **Cowork transcript** — same schema as §1.5 (+ `error`, `isApiErrorMessage`, `sourceToolUseID`; no `progress`/`file-history-snapshot`). The VM's cwd is always `/sessions/<processName>` | 🔎 |
| `…/local_<uuid>/…/<cliSessionId>/subagents/agent-<id>.jsonl` | Cowork subagents | 🔎 |
| `…/local_<uuid>/audit.jsonl` (+ `.audit-key`) | Append-only, **HMAC-chained** event log (tools, permission decisions, file ops); key encrypted via OS keychain. Line types: `system/init`, `assistant`, `user` (with `_audit_timestamp`), `tool_use_summary`, **`result`** (`total_cost_usd`, `duration_ms`, `duration_api_ms`, `num_turns`, `usage`, `modelUsage`), `rate_limit_event` | ✅ / 🔎 |
| `…/local_<uuid>/{uploads,outputs}/` | Files you attached / files Claude created | ✅ |
| `local-agent-mode-sessions/…/memory/` and `…/spaces/<projectId>/memory/` | **Cowork memory** — `CLAUDE.md` + Markdown notes about you | ✅ |
| `local-agent-mode-sessions/…/cowork_account_settings.json` | Display name, locale, memory toggle | ✅ |
| `claude-code-sessions/<accountId>/<orgId>/local_<uuid>.json` | **Code-tab wrapper** — metadata only: `sessionId`, **`cliSessionId`** (→ `~/.claude/projects/*/<cliSessionId>.jsonl`), `cwd`, `originCwd`, `worktreePath/Name`, `sourceBranch`, `branch`, `createdAt`, `lastActivityAt`, `lastFocusedAt`, `model`, `effort`, `isArchived`, `title`, `titleSource`, `permissionMode`, `completedTurns`, `enabledMcpTools`, `remoteMcpServersConfig`, `chromePermissionMode` … The Recents list is built from these wrappers only | ✅ / 🔎 |
| `claude-code/<version>/`, `claude-code-vm/`, `vm_bundles/` | Bundled Claude Code binaries, VM workspace & disk images (GBs) | ✅ |
| `Claude Extensions/`, `Claude Extensions Settings/`, `extensions-installations.json` | Desktop Extensions (DXT/MCPB) | 🔎 |
| `cowork_plugins/` | User / org plugins | ✅ |
| `logs/main.log`, `claude.ai-web.log`, `mcp.log`, `mcp-server-<name>.log`, `cowork_vm_node.log`, `cowork-service.log`, `coworkd.log`, `unknown-window.log` | Diagnostics (file names vary by release) | ✅ / 🔎 |
| `IndexedDB/`, `Local Storage/`, `Session Storage/`, `Cookies`, `Network/`, `Cache/`, `blob_storage/` | Chromium renderer state (window layout, recent folders, cached web app). Chat bodies are **not** readable here | ✅ |
| `git-worktrees.json`, `window-state.json`, `pending-uploads/`, `ChromeNativeHost/`, `cowork-enabled-cli-ops.json` | Misc app state | 🔎 |

Two directory names coexist: Anthropic is migrating `local-agent-mode-sessions` → `claude-code-sessions`
(issues #29373, #27463); an app update has lost Code-tab wrappers (#45710) — **the transcripts in
`~/.claude/projects` survive**, which is why Cantata links wrappers ↔ transcripts in both directions.

### 2.3 What is *not* local

* 1P **Chat** conversations (claude.ai account) — server-side; Desktop is an Electron shell over claude.ai ✅.
* 3P mode has **no Anthropic account**: Chat + Cowork + Code history are all local under `Claude-3p` ✅.

---

## 3. claude.ai in the browser / exports

### 3.1 Browser footprint (Chromium family) 🔎/❓

| Artifact | Where (inside `<User Data>/<Profile>/`) | What it tells you |
|---|---|---|
| History | `History` (SQLite, locked while browser runs → copy first) | `claude.ai/chat/<uuid>` visits with titles (`… - Claude`), `/project/<uuid>`, `/code/…`, `/new`, `/recents`, `/artifacts` |
| IndexedDB | `IndexedDB/https_claude.ai_0.indexeddb.leveldb/` | Web-app cache; V8-serialised + Snappy → not plaintext-recoverable; size + recent WAL (`*.log`) strings only |
| Local Storage | `Local Storage/leveldb/` (keys `_https://claude.ai\x00<key>`) | Org/device ids, UI prefs |
| Service Worker / Cache Storage | `Service Worker/…` | Offline shell |
| **Claude in Chrome** extension | `Local Extension Settings/fcoeoabgfenejglbffodgkkbkcdhcgfn/` (LevelDB) | Stores **API key + OAuth refresh token in plaintext** (Origin research, 2026-03) — Cantata flags it, never extracts it |
| Native host / IPC | `~/.claude/chrome/chrome-native-host`, `/tmp/claude-mcp-browser-bridge-<user>/*.sock` | Bridge between Claude Code/Desktop and the extension |

### 3.2 The official export (Settings → Privacy → Export data; e-mailed zip) 🔎

`conversations.json`, `projects.json`, `users.json` (some reports say `user.json`; `artifacts.json`/`memories.json` may appear ❓).

```jsonc
// conversations.json — top-level array; can exceed 100 MB → stream it
{ "uuid": "…", "name": "title", "summary": "", "created_at": "…Z", "updated_at": "…Z",
  "account": {"uuid": "…"}, "model": "claude-…"?,
  "chat_messages": [ {
     "uuid": "…", "sender": "human"|"assistant", "index": 0, "parent_message_uuid": "…",
     "created_at": "…", "updated_at": "…", "truncated": false,
     "text": "…legacy flat text…",
     "content": [ {"type":"text","text":"…","start_timestamp":"…","stop_timestamp":"…","citations":[]},
                  {"type":"thinking"|"tool_use"|"tool_result", … } ],
     "attachments": [ {"file_name","file_size","file_type","extracted_content"} ],
     "files": [ {"file_name", …} ] } ] }
// projects.json: [{uuid, name, description, is_private, prompt_template, created_at, updated_at, creator{…}, docs[{uuid, filename, content, created_at}]}]
// users.json:    [{uuid, full_name, email_address, verified_phone_number}]
```

Knowledge-base *binaries* aren't bundled; `sender:"human"` ↔ role `user`. A single conversation can also be fetched
from the authenticated web API (`/api/organizations/<org>/chat_conversations/<uuid>?tree=True…`, includes
`current_leaf_message_uuid` for picking the live branch) — Cantata does **not** make network calls.

---

## 4. "Time in flight" — what exists on disk vs. in telemetry

Codex records `time_to_first_token_ms` and `duration_ms` in its rollouts. **Claude Code does not.**

| Source | What it gives | Exact TTFT? | Tag |
|---|---|---|---|
| Transcript timestamps | request-sent (user/tool_result line) → first content-block line → last block line; tool_use line → tool_result line | No — *derived upper bound*: lines are written as blocks **complete**, and a permission prompt / hook adds wall time | 🔎 |
| `system` / `turn_duration` | App-measured wall time of a turn | n/a (turn total) | 🔎 |
| `toolUseResult.durationMs` (Glob/WebFetch), `.durationSeconds` (WebSearch), `.totalDurationMs` (subagent) | App-measured tool times | n/a | 🔎 |
| Cowork `audit.jsonl` `result` line | `duration_ms`, `duration_api_ms`, `num_turns`, `total_cost_usd` per session | n/a (API share) | 🔎 |
| `~/.claude.json` → `projects[*].lastDuration`, `lastAPIDuration` | Last-session totals | n/a | 🔎 |
| **OpenTelemetry** `claude_code.llm_request` span | **`ttft_ms`**, `duration_ms`, `first_content_ms` (≥ v2.1.268), `attempt`, `success`, `speed`, `effort`, tokens, `request_id` | **Yes** | ✅ |
| OTel log event `claude_code.api_request` | `duration_ms`, tokens, `cost_usd`, `request_id`, `speed`, `query_source` | no ttft | ✅ |
| OTel `claude_code.tool.execution`, `.tool.blocked_on_user` spans | tool run time vs. time waiting on you | n/a | ✅ |

Enabling OTel (traces are **beta**; `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` is required for spans) ✅:

```sh
export CLAUDE_CODE_ENABLE_TELEMETRY=1 CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp OTEL_METRICS_EXPORTER=none
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
export OTEL_TRACES_EXPORT_INTERVAL=1000 OTEL_LOGS_EXPORT_INTERVAL=1000
```

Claude Code appends `/v1/traces`, `/v1/logs`, `/v1/metrics` itself. Repo-level `.claude/settings.json` cannot switch
OTel on (ignored there), and `OTEL_*` is not inherited by the Bash tool or hooks ✅. Cantata's Live mode embeds a
loopback-only OTLP/HTTP-JSON receiver for exactly this, and also tails the transcripts for sessions that aren't exporting.

Other live surfaces (documented, not used): hooks (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop` — each gets
`session_id`, `transcript_path`, `cwd`), the `statusLine` command (JSON on stdin incl. cost/duration), `--output-format stream-json`.

---

## 5. Coverage matrix: DeKodX (Codex) → Cantata (Claude)

| DeKodX | Codex source | Cantata | Claude source |
|---|---|---|---|
| Archive dashboard | `~/.codex` inventory | **Archive** | `~/.claude` inventory, retention clock, version detection |
| Recover (report + per-session files) | `sessions/**/rollout-*.jsonl`, `archived_sessions/`, SQLite state | **Recover** | `projects/**/*.jsonl` (+ orphaned/superseded/subagents), Cowork transcripts |
| SQLite readers (`state_5`, `logs_2`) | threads / logs tables | `stores.py` | `history.jsonl`, `sessions-index.json`, `stats-cache.json`, tasks/todos/plans/memory/file-history, MCP logs |
| `config.toml`, `auth.json` | config + secrets | `configs.py` | `settings*.json`, `~/.claude.json`, `.credentials.json` state (presence/expiry only) |
| Redaction (standard / paranoid / none; hard secrets always) | | `redaction.py` | + Anthropic key formats, OAuth blobs, account/org ids, MCP `env` |
| Chat & Work (catalog, atoms, browser, exports) | `codex-dev.db`, chatgpt.com traces | **Chat & Cowork** | Cowork/Code wrappers, browser History/IndexedDB, export zips **with full reconstruction** |
| Live Trap (AUDIT / LIVE, TtFT, tokens, usage, bytes) | rollout tail + SQLite pollers | **Flight & Live** *(gated)* | transcript tail + OTLP receiver + usage diff + NIC bytes |
| `audit` CLI | rollouts + `logs_2` | `audit` CLI | transcripts + stats-cache cross-check |
| `capture` CLI | analytics capture file | `capture` CLI *(gated)* | tail + OTLP |

## 6. Known gaps / things to re-check after a Claude release

* `system` subtypes and top-level attribution fields keep changing (a June-2026 release added skill/plugin/MCP attribution, image blocks and structured API-error subtypes). Unknown fields are ignored, unknown line types are counted and listed in the report.
* `agent-<id>.meta.json` content, the exact `entrypoint` vocabulary and `usage-data/` schemas are ❓.
* Desktop first-party `config.json` is partly encrypted; Cantata lists key names only.
* Nothing here covers Claude on the web *Code* sessions (`claude.ai/code`), which live in Anthropic's cloud, only their browser-history footprint.

## 7. Sources

* Anthropic — Explore the `.claude` directory: https://code.claude.com/docs/en/claude-directory
* Anthropic — Settings reference / `cleanupPeriodDays`: https://code.claude.com/docs/en/settings-reference
* Anthropic — Monitoring (OpenTelemetry) incl. `llm_request` span: https://code.claude.com/docs/en/monitoring-usage
* Anthropic — Claude Desktop data storage (3P): https://claude.com/docs/third-party/claude-desktop/data-storage
* Anthropic — Cowork monitoring: https://claude.com/docs/cowork/monitoring
* B. Watson — Cowork manifest (27 fields): https://brycewatson.com/blog/09-cowork-session-schema/ · Cowork transcripts & `audit.jsonl`: https://brycewatson.com/blog/13-cowork-conversation-transcripts/
* GitHub issues: anthropics/claude-code #58670 (Code-tab `local_*.json` schema), #45710, #29373, #27463, #26073 (MSIX path), #26964 (cross-session writes), #61058 (`sessions-index.json`), #87678 (`claude-cli-nodejs` cache)
* Project-dir encoding: VirtusLab/orca#214, claudepath (PyPI), DevinoSolutions/magent#240
* JSONL format write-ups: adityabawankule.io/blog/claude-code-session-jsonl-format, dev.to/tznthou (schema drift), admantium.medium.com (field dump, v2.1.91)
* `stats-cache.json`: pkg.go.dev/github.com/tofutools/tclaude/pkg/claude/stats
* Credentials: gist Prajwalsrinvas/cacbb728… and the Atoll / CodexBar refresh-token issues
* Claude in Chrome storage: originhq.com/research/claude-for-chrome-takeover
* Export format: savelore.app/tools/claude-export-viewer, jvt.me/posts/2026/09/04/claude-chat-export, freecodecamp export guide
* Chromium LevelDB layout: browserforensics.app/en/blog/chrome-local-storage-and-indexeddb
