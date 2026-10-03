"""Data models shared by the parsers, the report writers and the UI."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_TEXT = 60_000        # stored characters per message
MAX_TOOL_OUTPUT = 20_000  # stored characters per tool output


class RecoveryCancelled(Exception):
    """Raised by the cooperative cancel hook."""


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0
    web_fetches: int = 0

    @property
    def total(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)

    @property
    def cache_hit_ratio(self) -> Optional[float]:
        denom = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return (self.cache_read_tokens / denom) if denom else None

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.web_searches += other.web_searches
        self.web_fetches += other.web_fetches

    def merge_max(self, other: "TokenUsage") -> None:
        """Repeated lines of one API response carry the same usage; keep the max."""
        self.input_tokens = max(self.input_tokens, other.input_tokens)
        self.output_tokens = max(self.output_tokens, other.output_tokens)
        self.cache_read_tokens = max(self.cache_read_tokens, other.cache_read_tokens)
        self.cache_write_tokens = max(self.cache_write_tokens, other.cache_write_tokens)
        self.web_searches = max(self.web_searches, other.web_searches)
        self.web_fetches = max(self.web_fetches, other.web_fetches)

    def copy(self) -> "TokenUsage":
        return TokenUsage(self.input_tokens, self.output_tokens, self.cache_read_tokens,
                          self.cache_write_tokens, self.web_searches, self.web_fetches)

    @classmethod
    def from_api(cls, usage: Any) -> "TokenUsage":
        if not isinstance(usage, dict):
            return cls()
        server = usage.get("server_tool_use") or {}
        return cls(
            input_tokens=_i(usage.get("input_tokens")),
            output_tokens=_i(usage.get("output_tokens")),
            cache_read_tokens=_i(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_i(usage.get("cache_creation_input_tokens")),
            web_searches=_i(server.get("web_search_requests")) if isinstance(server, dict) else 0,
            web_fetches=_i(server.get("web_fetch_requests")) if isinstance(server, dict) else 0,
        )


def _i(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class ToolCall:
    call_id: str = ""
    name: str = ""
    summary: str = ""                 # one-line human description of the input
    input_text: str = ""              # redaction-ready rendering of the input
    output: str = ""
    is_error: bool = False
    started_at: str = ""
    finished_at: str = ""
    wall_ms: Optional[int] = None     # derived: tool_use line -> tool_result line
    reported_ms: Optional[int] = None  # app-measured (toolUseResult.durationMs etc.)
    agent_id: str = ""                # subagent this call spawned (Task / Agent tool)
    truncated: bool = False


@dataclass
class ApiRequest:
    """One API response ("request in flight") reconstructed from transcript lines."""
    message_id: str = ""
    request_id: str = ""
    model: str = ""
    sent_at: str = ""                 # timestamp of the user/tool_result line that triggered it
    first_at: str = ""                # first content-block line
    last_at: str = ""                 # last content-block line
    in_flight_ms: Optional[int] = None  # sent -> first block   (derived upper bound on TTFT)
    stream_ms: Optional[int] = None     # first block -> last block
    total_ms: Optional[int] = None      # sent -> last block
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str = ""
    speed: str = ""
    tool_uses: int = 0
    blocks: int = 0
    turn_index: int = 0
    source: str = "derived"           # derived | otel
    ttft_ms: Optional[int] = None     # exact, only from OTel


@dataclass
class Message:
    uuid: str = ""
    parent_uuid: str = ""
    role: str = "assistant"           # user | assistant | system
    kind: str = "text"                # prompt | text | meta | summary | system | command
    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    timestamp: str = ""
    last_timestamp: str = ""
    model: str = ""
    message_id: str = ""
    request_id: str = ""
    stop_reason: str = ""
    usage: Optional[TokenUsage] = None
    flags: set[str] = field(default_factory=set)
    subtype: str = ""
    truncated: bool = False


@dataclass
class Turn:
    index: int = 0
    prompt: Optional[Message] = None
    messages: list[Message] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""
    wall_ms: Optional[int] = None          # derived
    reported_ms: Optional[int] = None      # system/turn_duration
    usage: TokenUsage = field(default_factory=TokenUsage)
    models: set[str] = field(default_factory=set)
    requests: list[ApiRequest] = field(default_factory=list)
    interrupted: bool = False
    is_compact: bool = False
    note: str = ""

    @property
    def tool_count(self) -> int:
        return sum(len(m.tool_calls) for m in self.messages)

    @property
    def error_count(self) -> int:
        n = sum(1 for m in self.messages if "api_error" in m.flags)
        n += sum(1 for m in self.messages for t in m.tool_calls if t.is_error)
        return n


@dataclass
class SessionRecord:
    session_id: str = ""
    kind: str = "main"                 # main | subagent | orphaned | superseded | cowork | cowork-subagent
    origin: str = ""                   # cli | vscode | desktop-code | desktop-cowork | sdk | ...
    project_dir: str = ""              # encoded folder name under projects/
    project_path: str = ""             # real cwd (from the lines)
    source_path: str = ""
    size_bytes: int = 0
    title: str = ""
    custom_title: str = ""
    ai_title: str = ""
    summary: str = ""
    slug: str = ""
    first_prompt: str = ""
    agent_id: str = ""
    agent_type: str = ""
    agent_description: str = ""
    parent_session_id: str = ""
    parent_call_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    versions: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    git_branches: list[str] = field(default_factory=list)
    permission_modes: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    child_ids: list[str] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    usage_by_model: dict[str, TokenUsage] = field(default_factory=dict)
    usage_by_day: dict[str, TokenUsage] = field(default_factory=dict)
    line_count: int = 0
    corrupt_lines: int = 0
    type_counts: Counter = field(default_factory=Counter)
    tool_counts: Counter = field(default_factory=Counter)
    stop_reasons: Counter = field(default_factory=Counter)
    compactions: list[dict] = field(default_factory=list)
    api_errors: int = 0
    queued_prompts: list[str] = field(default_factory=list)
    tracked_files: list[str] = field(default_factory=list)
    pr_links: list[str] = field(default_factory=list)
    foreign_sessions: Counter = field(default_factory=Counter)   # lines whose sessionId != file id
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)          # manifest / wrapper / audit data
    # filled by flight.py (never rendered unless unlocked)
    requests: list[ApiRequest] = field(default_factory=list)

    @property
    def is_subagent(self) -> bool:
        return self.kind in ("subagent", "cowork-subagent")

    @property
    def prompt_count(self) -> int:
        return sum(1 for t in self.turns if t.prompt is not None and not t.is_compact)

    @property
    def tool_count(self) -> int:
        return sum(self.tool_counts.values())

    @property
    def display_title(self) -> str:
        for cand in (self.custom_title, self.ai_title, self.title, self.summary,
                     self.agent_description, self.first_prompt, self.slug):
            if cand and cand.strip():
                return " ".join(cand.split())[:160]
        return "(untitled)"


@dataclass
class VersionInfo:
    claude_code_versions: list[str] = field(default_factory=list)   # seen in transcripts, newest first
    entrypoints: list[str] = field(default_factory=list)
    desktop_version: str = ""
    config_format: str = ""
    install_method: str = ""
    cleanup_period_days: Optional[int] = None


@dataclass
class RecoveryOptions:
    LEVELS = ("standard", "paranoid", "none")

    include_transcripts: bool = True
    include_app_state: bool = True
    include_tokens: bool = True
    include_tools: bool = True
    include_thinking: bool = True
    include_logs: bool = True
    include_desktop: bool = True        # Cowork + Code-tab wrappers
    include_orphaned: bool = True       # .orphaned-* / .superseded-* transcripts
    log_limit: int = 1000
    redaction_level: str = "standard"
    per_sessions: bool = True
    include_subagents: bool = True
    flatten_subagents: bool = False
    include_flight: bool = False        # time-in-flight tables: GATED, off unless unlocked
    desktop_dirs: tuple = ()            # explicit Desktop app-data dirs (tests / forensic images)


@dataclass
class RecoveryResult:
    output_path: Any = None
    per_session_paths: list = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    sessions: list[SessionRecord] = field(default_factory=list)
