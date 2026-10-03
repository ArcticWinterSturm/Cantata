## Time in Flight

> **In flight (derived)** = request-sent line → first completed content block. It is an *upper bound* on true time-to-first-token: Claude Code writes a block when it finishes streaming, and permission prompts / hooks before the request inflate it.
> **Stream** = first block → last block of one API response; **tokens/s** = output tokens ÷ that span (only meaningful for multi-block responses).
> **Tool wall** = tool_use line → tool_result line, so it includes any time spent waiting for *your* permission.
> Exact TTFT requires OpenTelemetry capture (`claude_code.llm_request.ttft_ms`) — use Live mode's receiver.

- **API responses analysed**: 13 across 6 sessions · **tool calls**: 6 · **turns with timing**: 9

### Per model

| Model | Responses | In flight p50 | p90 | p99 | Stream p50 | Total p50 | Out tok/s p50 |
|---|---|---|---|---|---|---|---|
| `claude-opus-4-5-20251101` | 10 | 1.1 s | 3.5 s | 4.0 s | 0 ms | 1.1 s | 167 |
| `claude-haiku-4-5-20251001` | 2 | 1.4 s | 1.6 s | 1.6 s | 0 ms | 1.4 s | - |
| `claude-sonnet-4-5-20250929` | 1 | 2.0 s | 2.0 s | 2.0 s | 0 ms | 2.0 s | - |

### Slowest 13 requests (by in-flight time)

| Session | Model | Sent | In flight | Stream | Out tok | Tool uses | Speed |
|---|---|---|---|---|---|---|---|
| `11111111` | `claude-opus-4-5-20251101` | 2026-09-30 07:49:12 | 4.0 s | 0 ms | 40 | 0 | standard |
| `11111111` | `claude-opus-4-5-20251101` | 2026-09-30 07:47:28 | 3.5 s | 0 ms | 120 | 0 | standard |
| `11111111` | `claude-opus-4-5-20251101` | 2026-09-30 07:47:17 | 2.5 s | 0 ms | 90 | 1 | fast |
| `11111111` | `claude-opus-4-5-20251101` | 2026-09-30 07:47:12 | 2.4 s | 900 ms | 150 | 1 | standard |
| `22222222` | `claude-sonnet-4-5-20250…` | 2026-09-02 10:47:12 | 2.0 s | 0 ms | 80 | 0 | - |
| `↳a1b2c3d` | `claude-haiku-4-5-202510…` | 2026-09-30 07:47:22 | 1.6 s | 0 ms | 30 | 0 | - |
| `↳a1b2c3d` | `claude-haiku-4-5-202510…` | 2026-09-30 07:47:21 | 1.2 s | 0 ms | 20 | 1 | - |
| `11111111` | `claude-opus-4-5-20251101` | 2026-09-30 07:47:19 | 1.1 s | 0 ms | 70 | 1 | standard |
| `44444444` | `claude-opus-4-5-20251101` | 2026-09-25 10:47:12 | 1.0 s | 0 ms | 10 | 1 | - |
| `44444444` | `claude-opus-4-5-20251101` | 2026-09-25 10:47:16 | 1.0 s | 0 ms | 6 | 0 | - |
| `↳deadbee` | `claude-opus-4-5-20251101` | 2026-09-25 10:47:13 | 1.0 s | 0 ms | 4 | 0 | - |
| `33333333` | `claude-opus-4-5-20251101` | 2026-09-28 10:47:12 | 1.0 s | 0 ms | 30 | 1 | - |
| `33333333` | `claude-opus-4-5-20251101` | 2026-09-28 10:47:13 | 700 ms | 0 ms | 5 | 0 | - |

### Tools

| Tool | Calls | Wall p50 | Wall p90 | Wall max | Reported p50 | Errors |
|---|---|---|---|---|---|---|
| `Task` | 2 | 5.0 s | 6.6 s | 7.0 s | 6.4 s | 0 |
| `Bash` | 1 | 2.0 s | 2.0 s | 2.0 s | - | 0 |
| `Write` | 1 | 300 ms | 300 ms | 300 ms | - | 0 |
| `Grep` | 1 | 200 ms | 200 ms | 200 ms | 180 ms | 0 |
| `Read` | 1 | 100 ms | 100 ms | 100 ms | - | 0 |

### Longest 8 turns — where the time went

_model = Σ request totals · tools = Σ tool wall (excl. subagent spawns) · other = hooks, permission waits, user think-time, overhead_

| Session | Turn | Prompt | Wall | Model | Tools | Other | App-reported | Requests |
|---|---|---|---|---|---|---|---|---|
| `11111111` | 2 | Now run the tests | 30.0 s | 0 ms | 0 ms | 30.0 s | - | 1 |
| `11111111` | 1 | Please fix the login bug. My email is [… | 19.5 s | 10.4 s | 2.1 s | 7.0 s | 19.6 s | 4 |
| `44444444` | 1 | Audit the repo | 5.0 s | 2.0 s | 0 ms | 3.0 s | - | 2 |
| `11111111` | 4 | Thanks - write the changelog entry | 4.0 s | 4.0 s | 0 ms | 0 ms | - | 1 |
| `↳a1b2c3d` | 1 | Find every caller of login() in the rep… | 3.0 s | 2.8 s | 200 ms | 0 ms | - | 2 |
| `22222222` | 1 | Explain the project layout | 2.0 s | 2.0 s | 0 ms | 0 ms | - | 1 |
| `33333333` | 1 | Add a health endpoint | 2.0 s | 1.7 s | 300 ms | 0 ms | - | 2 |
| `↳deadbee` | 1 | List deps | 1.0 s | 1.0 s | 0 ms | 0 ms | - | 1 |

### Last-session totals from `~/.claude.json`

| Project | Last duration | Last API time | API share |
|---|---|---|---|
| `~/projects/webapp` | 3 m 03 s | 41.2 s | 23% |
