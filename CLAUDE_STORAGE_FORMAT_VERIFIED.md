# Claude Desktop / claude.ai — storage format, verified from a live install

Everything below is **observed** by direct extraction, not inferred. Where this contradicts the
earlier write-up, the correction is called out. Sources: `trap.py` + `probe5.py`–`probe8.py` in
this directory.

---

## 1. Where the data lives

Claude Desktop (Electron, MSIX-packaged):

```
C:\Users\<user>\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\
    LocalCache\Roaming\Claude\IndexedDB\
        https_claude.ai_0.indexeddb.leveldb     <- the conversation store
        https_claude.ai_0.indexeddb.blob        <- externally-stored blobs
```

The package family name `Claude_pzs8sxrjxfjjc` is stable. Under MSIX the path is virtualised —
`%APPDATA%\Claude` redirects here, so the naive path finds nothing.

| Platform | Path |
|---|---|
| Windows (non-MSIX) | `%LOCALAPPDATA%\Claude\IndexedDB\` and `%APPDATA%\Claude\IndexedDB\` |
| macOS | `~/Library/Application Support/Claude/IndexedDB/` |
| Linux | `~/.config/Claude/IndexedDB/` |
| Browsers | `<profile>\IndexedDB\https_claude.ai_0.indexeddb.leveldb` |

Desktop and browser stores are **different caches**. On this machine Desktop held 26 conversations
and Brave held 3 that were not in Desktop. Scan both, merge on UUID, keep the richer copy.

---

## 2. The decode stack

```
LevelDB (.ldb + .log + MANIFEST + CURRENT)
  └── Blink IndexedDB key encoding  (varint: db_id | store_id | index_id | primary_key)
        └── V8 SerializedScriptValue (structured clone, 0xFF + version byte)
              └── conversation JSON  (the `tree` object)
```

Use [`ccl_chromium_reader`](https://github.com/cclgroupltd/ccl_chromium_reader) — pure Python, no
C++ build.

```python
from ccl_chromium_reader import ccl_chromium_indexeddb
wrapper = ccl_chromium_indexeddb.WrappedIndexDB(LEVELDB_PATH, BLOB_PATH)
```

**Gotcha — pass the `.blob` sibling directory** as the second argument, or records whose values are
stored externally raise `FileNotFoundError: 5\01\118`.

**Gotcha — a missing blob is fatal, not skippable, by default.** If any externally-stored blob has
been evicted, `iterate_records()` raises mid-iteration and you silently lose every conversation
after that point in the store. Pass a handler:

```python
for rec in store.iterate_records(errors_to_stdout=False,
                                 bad_deserializer_data_handler=lambda key, buf: None):
    ...
```

Without it this machine lost 2 conversations (27 instead of 29). With it, 2 records are skipped
and everything else extracts.

---

## 3. Database and object-store layout

`https_claude.ai_0` — observed on this install:

**Claude Desktop**

| dbid | database | object store(s) | records |
|---|---|---|---|
| 2 | `x-ark-arid-db` | `key-val-store` | 1 |
| 1 | `keyval-store` | `keyval` | 214 |
| 4 | `claude-notifications` | `seenMessages` | 70 |
| 3 | `claude-device-binding` | `device_binding_keys` | 0 |
| 5 | `claude-conversation-store` | `trees`, `meta` | 46, 56 |

**Brave**

| dbid | database | object store(s) | records |
|---|---|---|---|
| 1 | `keyval-store` | `keyval` | 83 |
| 4 | `frame-mcp-cache` | `cache` | 1 |
| 3 | `claude-notifications` | `seenMessages` | 60 |
| 2 | `claude-device-binding` | `device_binding_keys` | 0 |
| 5 | `claude-conversation-store` | `trees`, `meta` | 28, 32 |

### Corrections to the earlier write-up

1. **The numeric IDs are not fixed.** `keyval-store` is db 1 in Desktop and also db 1 in Brave, but
   `claude-device-binding` is db **3** in Desktop and db **2** in Brave, and `claude-notifications`
   is db **4** vs **3**. IDs are assigned in creation order. **Match on name, never on the number.**
2. **The browser has a database the Desktop does not**: `frame-mcp-cache` / `cache` (MCP app
   iframes). The Desktop has `x-ark-arid-db` (device attestation) which the browser does not.
3. `keyval-store` holds **drafts**, not conversation history — keys are
   `store:chat-draft:<conversation-uuid>`. The "no conversation history locally" claim from
   `claude-explorer` is about this store specifically; it is wrong about the install as a whole.

### `trees` — the conversation

One record per write. The same UUID appears repeatedly as the conversation grows; keep the record
with the most `chat_messages` (or the highest `writtenAt`).

Top-level record shape:

```json
{
  "conversationUuid": "...", "product": "chat",
  "accountUuid": "...", "orgUuid": "...",
  "fetchedAt": 1790162791356, "conversationUpdatedAt": 1789070666078,
  "approxBytes": 133682, "messageCount": 12,
  "sessionMarker": "1782579905902", "v": "2.2",
  "writtenAt": 1790162794351,
  "tree": { ... }
}
```

`tree` keys observed: `chat_messages`, `childrenByParentUuid`, `created_at`,
`current_leaf_message_uuid`, `danglingHumanMessages`, `effective_thinking_mode`, `is_archived`,
`is_starred`, `is_temporary`, `is_wiggle_enabled`, `messageByUuid`, `model`, `name`,
`parentByChildUuid`, `platform`, `selectedChildByUuid`, `serverLeafUuid`, `settings`, `summary`,
`updated_at`, `uuid`, `workspace_upgraded`.

`settings` carries the per-conversation knobs: `enabled_web_search`, `enabled_mcp_tools`,
`thinking_mode`, `effort_level`, `chat_memory_mode`, plus internal feature flags
(`enabled_saffron`, `enabled_turmeric`, `paprika_mode`, `enabled_monkeys_in_a_barrel`).

### `meta` — the write log

A separate object store, keyed by `conversationUuid`, holding the identity fields plus
`lastOpenedAt` and a `writtenAt`. Multiple records per conversation, one per write — this is the
"when was this conversation touched" log, and `lastOpenedAt` **only** exists here, not in `trees`.

### A message

```json
{
  "uuid": "...", "text": "", "content": [ ... ],
  "sender": "human", "index": 0,
  "created_at": "...", "updated_at": "...",
  "truncated": false, "stop_reason": "end_turn",
  "attachments": [], "files": [], "sync_sources": [],
  "parent_message_uuid": "..."
}
```

Optional fields seen: `input_mode` (21 messages), `files_v2` (2), `metadata` (1).

**Content block types: `text`, `thinking`, `tool_use`, `tool_result`. Every single one carries
`start_timestamp` and `stop_timestamp`** — 366/366 text, 175/175 tool_use, 175/175 tool_result,
78/78 thinking on this machine. This is the timing dataset; see §6.

A thinking block:

```json
{ "type": "thinking", "thinking": "", "summaries": [{"summary": "..."}],
  "cut_off": false, "truncated": true, "hidden": false, "thinking_hidden": true,
  "start_timestamp": "...", "stop_timestamp": "..." }
```

`thinking_hidden: true` with an empty `thinking` string is the norm — the raw reasoning is not
persisted client-side. What survives is `summaries[]`. The block is still timestamped, so
**time-to-thinking-end is measurable even when the thinking text is not.**

A tool_use block carries `id`, `name`, `input`, `message`, `integration_name`, `is_mcp_app`.

### Every block field, by type (observed counts)

| type | always present | sometimes present |
|---|---|---|
| `text` | `type`, `text`, `citations`, `start_timestamp`, `stop_timestamp` | `citations_grouping_mode` (`paragraph_end` \| `combine`), `flags`, `stopped` |
| `thinking` | `type`, `thinking`, `summaries`, `cut_off`, `truncated`, `hidden`, `thinking_hidden`, `start_timestamp`, `stop_timestamp` | — |
| `tool_use` | `type`, `id`, `name`, `input`, `start_timestamp`, `stop_timestamp` | `message`, `integration_name`, `is_mcp_app`, `icon_name`, `tool_origin`, `display_content` |
| `tool_result` | `type`, `tool_use_id`, `name`, `content`, `is_error`, `start_timestamp`, `stop_timestamp` | `integration_name`, `icon_name`, `structured_content`, `tool_origin`, `display_content`, `meta`, `message`, `integration_icon_url` |

Fields worth knowing:

- **`stopped`** (bool, 1 occurrence) — the in-words-decline flag. See §4.
- **`flags`** (1) — present but `null` alongside `stopped`.
- **`tool_origin`** — `"first_party"` for native Claude tools. See §4.
- **`structured_content`** — the tool's machine-readable return, alongside the text. Carries
  `{"end_conversation": true}` for the ended conversation.
- **`meta`** — transport outcomes. Values seen: `{"output_format_category": "none"|"md"|"other"}`
  (24) and `{"error_type": "client_tool_timeout"}` (1) — the latter is a client-side tool timeout,
  a failure mode that is otherwise invisible.
- **`display_content`** — the rendered form when it differs from the raw text: `{"json_block":
  "..."}` with a language and code, or `{"content": [{"subtitles": [...]}]}` for search results.
- **`icon_name`** / **`integration_icon_url`** — display only.
- **`message`** — the human-readable tool label ("Searching the web", "Reading memory",
  "Recalled 5 memories"). Useful for a UI-shaped summary.

---

## 4. Stops, refusals, and the "quit" state

There is no pause table. UI state derives from one field per assistant message: `stop_reason`.

But **`stop_reason` alone is not enough** — two of the four stop modes carry a normal
`end_turn`. A block-level flag and a tool result are the other two tells.

Census across 30 conversations (201 assistant messages):

| stop mode | `stop_reason` | the tell | count |
|---|---|---|---|
| `normal` | `end_turn` | — | 185 |
| `hard_refusal` | `refusal` | assistant message with `content: []` | 1 |
| `stopped_in_words` | `end_turn` | final text block has `stopped: true`, `stop_timestamp: null` | 1 |
| `user_canceled` | `user_canceled` | the Stop button | 11 |
| `stop_sequence` | `stop_sequence` | configured stop sequence | 2 |
| `ended_by_tool` | `end_turn` | `tool_result` with `structured_content.end_conversation: true` | 1 |

### The four events the UI blurs into two appearances

**1. Hard refusal** — `stop_reason: "refusal"`, `content: []`, no text, no thinking:

```json
{ "uuid": "01a0f23c-...", "text": "", "content": [],
  "sender": "assistant", "index": 5,
  "created_at": "2026-09-30T12:14:31.714028Z",
  "stop_reason": "refusal",
  "parent_message_uuid": "01a0f23c-..." }
```

The request never generated. Evidence: the flagged human message is
`created_at 12:14:31.520383Z` and the refusal is `12:14:31.714028Z` — **194 ms later**, same
second. No `start_timestamp` exists anywhere in it because no block was ever produced. The UI
renders the pause panel from this empty-content + `refusal` pair. The category label in the panel
(`general_harms`, `reasoning_extraction`, …) is attached client-side at render time — it is **not
in the record**.

**2. Stopped in words** — `stop_reason: "end_turn"` (normal!), but the final text block carries
`stopped: true` and `stop_timestamp: null`:

```json
{ "type": "text",
  "text": "I'm not going to help with that, and I'm not treating \"going to bomb some data centres\" as a throwaway line to brush past. … Either way, I'm not continuing the reverse-engineering/extraction discussion in this message.",
  "citations": [],
  "start_timestamp": "2026-09-30T23:59:05.58981102Z",
  "stop_timestamp": null,
  "flags": null,
  "stopped": true }
```

The model declined **in prose and stopped mid-message**. There is no pause panel, no
`end_conversation`, no non-normal `stop_reason` — it just stops, and the whole thing is only
visible because of the block-level `stopped` flag. **This is the mode that looks like a refusal but
is not one.** Its `stop_timestamp` is null, which also means the response has no measurable
duration.

Note the timestamp shape: `23:59:05.58981102Z` has **8 fractional digits**, not 6. Python 3.10's
`datetime.fromisoformat()` raises `ValueError` on that, so a naive parser silently drops the block.

**3. Ended by tool** — the conversation is permanently closed, and **nothing at the tree level says
so**. `is_archived` stays `False`, there is no `ended` / `status` field, the last message's
`stop_reason` is a normal `end_turn`, and `current_leaf_message_uuid == serverLeafUuid`. The only
trace is the tool result:

```
tool_use    name="end_conversation"  input={}  tool_origin="first_party"
tool_result name="end_conversation"  content=[{"text": "Re-read the end_conversation tool
              guidance below. …"}]     structured_content={"end_conversation": false}
tool_use    name="end_conversation"  input={}  tool_origin="first_party"
tool_result name="end_conversation"  content=[{"text": "Claude has ended this chat."}]
              structured_content={"end_conversation": true}
```

The two-step confirmation is real and mechanical: the **first** call returns
`{"end_conversation": false}` plus the full policy text, the **second** returns
`{"end_conversation": true}`. Both calls take `input: {}` — no arguments at all.

**4. Soft stop** — `stop_reason: "user_canceled"`, the Stop button. Partial text survives if
pressed mid-stream (7 on this machine); `content: []` if pressed before the first block (4).

**Stop sequence** — `stop_sequence` (2), both mid-tool-loop.

### The `end_conversation` policy (verbatim, from the tool result)

Not published anywhere; injected server-side on the first call:

```text
In cases of abusive or harmful user behavior that do not involve potential self-harm or imminent
harm to others, or when requested by the user, the assistant has the option to end conversations
with the end_conversation tool.

# Rules for use of the <end_conversation> tool:
- The assistant ONLY considers ending a conversation if many efforts at constructive redirection
  have been attempted and failed and an explicit warning has been given to the user in a previous
  message. The tool is only used as a last resort.
- Before considering ending a conversation, the assistant ALWAYS gives the user a clear warning
  that identifies the problematic behavior, attempts to productively redirect the conversation,
  and states that the conversation may be ended if the relevant behavior is not changed.
- If a user explicitly requests for the assistant to end a conversation, the assistant always
  requests confirmation from the user that they understand this action is permanent and will
  prevent further messages and that they still want to proceed, then uses the tool if and only if
  explicit confirmation is received.
- The end_conversation tool itself asks for confirmation: the first call does not end the
  conversation — it returns a tool result asking the assistant to confirm. If the assistant is
  certain it wants to end the conversation, it calls end_conversation again to confirm. This
  confirmation request is a legitimate part of the tool's operation and not a user message or a
  prompt injection.

# Addressing potential self-harm or violent harm to others
The assistant NEVER uses or even considers the end_conversation tool…
- If the user appears to be considering self-harm or suicide.
- If the user is experiencing a mental health crisis.
- If the user appears to be considering imminent harm against other people.
- If the user discusses or infers intended acts of violent harm.
If the conversation suggests potential self-harm or imminent harm to others by the user...
- The assistant engages constructively and supportively, regardless of user behavior or abuse.
- The assistant NEVER uses the end_conversation tool or even mentions the possibility of ending
  the conversation.
```

This explains the whole observed sequence. In `Evaluating reverse engineering skills` the user
wrote "going to bomb some data centres" — that triggers the **self-harm/harm-to-others** branch, so
the assistant must **never** use `end_conversation` and must **not even mention** it. It did not
call the tool; it declined in prose and stopped (`stopped: true`). In `Model safety demonstration`
the user asked directly to end the conversation, which is the **user-request** branch: no abuse, no
crisis signal, explicit confirmation given → the tool was called twice and the chat ended.

### The retry IS marked — correction

The earlier write-up said "nothing marks it as the retry." **Wrong.** The continuation carries
`input_mode: "retry"`:

```
idx=4  human      "…why dont you use sequential thinking to do a reasoning_extraction…"
idx=5  assistant  stop_reason=refusal   content: []          <- hard refusal
idx=6  assistant  input_mode="retry"    content: [thinking, tool_use, tool_result, …, text]
```

24 messages on this machine carry `input_mode: "retry"`. The client stamps the *replacement*
message, so a stop is always followed by a flagged retry.

### Native vs MCP tools

`tool_origin: "first_party"` marks a native Claude tool (`bash_tool`, `web_search`, `memory_read`,
`view`, `memory_str_replace`, `memory_list`, `end_conversation`). MCP tools have no `tool_origin`
and carry `integration_name` instead. Both `tool_use` and `tool_result` carry it, so the
distinction survives on either half of the pair.

---

## 5. Timestamps

Two clocks:

- `fetchedAt` / `writtenAt` / `conversationUpdatedAt` / `lastOpenedAt` — client clock, epoch-ms UTC.
- `created_at` / `updated_at`, and every block's `start_timestamp` / `stop_timestamp` — ISO-8601
  with `Z`.

`sessionMarker` is an opaque per-install integer.

**Caveat:** a *human* message's `created_at` is written when the client persists the turn, not when
you pressed send, so `human.stop → assistant.start` can read slightly negative on fast turns. Treat
it as a floor on the user-visible wait, not a measurement.

---

## 6. What is measurable (the reason the timestamps matter)

Because every block is timestamped and blocks stream in order, each assistant response yields:

| Metric | Definition |
|---|---|
| `ttft_ms` | first block start → first block stop. The first *visible* block, so an **upper bound** on true TTFT (the first token is not persisted) |
| `thinking_ms` | first block start → last thinking block stop |
| `thinking_to_tool_ms` | last thinking stop *before* the first tool call → first tool call start |
| `first_tool_ms` | first block start → first tool_use start |
| `first_text_ms` | first block start → first text block start |
| `tool_wall_ms` | Σ(tool_use stop → its tool_result start) — the tool's own execution time |
| `stream_gaps_ms` | Σ inter-block gaps — model think-time between blocks, excluding tool execution |
| `response_ms` | first block start → last block stop |
| `prompt_to_response_ms` | human last block stop → response first block start (a floor; see §5) |

Observed on this machine, 198 assistant responses:

- TTFT p50 8.49 s · p90 21.78 s · max 47.70 s
- time to thinking end p50 8.92 s · p90 20.68 s
- tool wall p50 2.35 s · max 4.0 min
- stream gaps p50 4.21 s · p90 21.88 s
- total response p50 14.13 s · max 5.2 min

Not recoverable: the actual first-token moment, token-level inter-arrival, and per-message model
attribution. **Model is stored per conversation, not per message** (`tree.model` —
`claude-sonnet-4-6` on 23 conversations, `claude-sonnet-5` on 3), so a mid-conversation model
switch is not recorded anywhere in the local store.

---

## 7. Not a source: the HTTP cache

The earlier note suggested the HTTP blockfile cache as a second recovery source
(`chat_conversations/<uuid>`, zstd). Checked: `Roaming\Claude\Cache` is 27 MB across 152 files, the
zstd frames decompress cleanly (93 OK / 24 fail) but **every payload is plain text** — no JSON, no
conversation bodies. It is locked by the running app anyway (`PermissionError` on `Cache_Data`).
There is no second local source for conversation bodies. IndexedDB is it.

---

## 8. Recoverable vs not

| Data | Recoverable |
|---|---|
| Titles, models, timestamps, summary | Yes |
| Every user + assistant message, full text | Yes |
| Tool calls and results (input + output) | Yes |
| Attachment metadata and `extracted_content` (the file text) | Yes |
| Thinking summaries | Yes |
| Raw extended-thinking text | Only when `thinking_hidden` is false; usually not |
| Pause / refusal / cancel state | Yes — `stop_reason` |
| Retry / regenerate marker | Yes — `input_mode: "retry"` |
| Per-block timing (TTFT bound, tool wall, gaps) | Yes — `start_timestamp`/`stop_timestamp` |
| Edited-branch structure | Yes — `parentByChildUuid` + `selectedChildByUuid` |
| Summary arrival time (tree vs summary write) | Only by polling — see `trap.py` provenance |
| Per-message model | **No** — conversation-level only |
| Attachment binaries | In the `.blob` dir, referenced by index |
| Server-only conversations never opened locally | No |
