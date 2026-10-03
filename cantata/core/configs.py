"""Settings, global config, credentials *state*, MCP servers.

Secrets are **never** copied out: env / header values are reduced to their key names, OAuth tokens to
"present + expiry", and the ``oauthAccount`` block goes through the redactor's person/id masking.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import paths
from .redaction import Redactor

SETTINGS_FILES = (("user", "settings.json"), ("user-local", "settings.local.json"))


def _load(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _summarize_settings(scope: str, path: Path, data: dict) -> dict:
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    hooks = []
    for event, groups in (data.get("hooks") or {}).items() if isinstance(data.get("hooks"), dict) else []:
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks", []) if isinstance(g.get("hooks"), list) else []:
                if isinstance(h, dict):
                    hooks.append({"event": event, "matcher": str(g.get("matcher", "")), "type": str(h.get("type", "")),
                                  "command": str(h.get("command") or h.get("url") or h.get("prompt") or "")[:300]})
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    return {
        "scope": scope, "path": str(path), "exists": True,
        "model": data.get("model"), "cleanupPeriodDays": data.get("cleanupPeriodDays"),
        "permissions": {"allow": [str(x) for x in perms.get("allow", [])], "deny": [str(x) for x in perms.get("deny", [])],
                        "ask": [str(x) for x in perms.get("ask", [])], "defaultMode": perms.get("defaultMode")},
        "hooks": hooks, "env_keys": sorted(env.keys()),
        "otel_enabled": str(env.get("CLAUDE_CODE_ENABLE_TELEMETRY", "")) in ("1", "true"),
        "statusLine": bool(data.get("statusLine")), "other_keys": sorted(k for k in data if k not in
                                                                          ("permissions", "hooks", "env", "model", "cleanupPeriodDays", "statusLine")),
    }


def read_settings(config_dir: Path, project_paths: Iterable[str] = ()) -> list[dict]:
    out: list[dict] = []
    for scope, name in SETTINGS_FILES:
        p = Path(config_dir) / name
        data = _load(p)
        out.append(_summarize_settings(scope, p, data) if isinstance(data, dict)
                   else {"scope": scope, "path": str(p), "exists": False})
    for mp in paths.managed_settings_candidates():
        data = _load(mp)
        if isinstance(data, dict):
            out.append(_summarize_settings("managed", mp, data))
    seen = set()
    for pp in project_paths:
        if not pp or not Path(pp).is_absolute() or pp in seen:
            continue
        seen.add(pp)
        for scope, rel in (("project", ".claude/settings.json"), ("project-local", ".claude/settings.local.json")):
            p = Path(pp) / rel
            data = _load(p)
            if isinstance(data, dict):
                s = _summarize_settings(f"{scope}:{paths.project_label(pp)}", p, data)
                out.append(s)
    return out


def _mcp_entry(name: str, cfg: Any, scope: str) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else {}
    return {"name": name, "scope": scope, "type": str(cfg.get("type") or ("http" if cfg.get("url") else "stdio")),
            "command": " ".join([str(cfg.get("command", ""))] + [str(a) for a in cfg.get("args", []) if not str(a).startswith("-")][:3]).strip(),
            "url": str(cfg.get("url", "")), "env_keys": sorted(env), "header_keys": sorted(headers)}


def read_global_json(path: Optional[Path]) -> dict:
    """Sanitised view of ``~/.claude.json`` (no secrets, account block masked later by the redactor)."""
    if not path:
        return {"exists": False}
    data = _load(path)
    if not isinstance(data, dict):
        return {"exists": False, "path": str(path)}
    acct = data.get("oauthAccount") if isinstance(data.get("oauthAccount"), dict) else {}
    projects = {}
    for pp, info in (data.get("projects") or {}).items() if isinstance(data.get("projects"), dict) else []:
        if not isinstance(info, dict):
            continue
        projects[pp] = {
            "trusted": info.get("hasTrustDialogAccepted"), "last_session": info.get("lastSessionId"),
            "last_cost_usd": info.get("lastCost"), "last_duration_ms": info.get("lastDuration"),
            "last_api_duration_ms": info.get("lastAPIDuration"),
            "lines_added": info.get("lastLinesAdded"), "lines_removed": info.get("lastLinesRemoved"),
            "last_input_tokens": info.get("lastTotalInputTokens"), "last_output_tokens": info.get("lastTotalOutputTokens"),
            "model_usage": info.get("lastModelUsage") if isinstance(info.get("lastModelUsage"), dict) else {},
            "allowed_tools": len(info.get("allowedTools") or []),
            "mcp_servers": sorted((info.get("mcpServers") or {}).keys()) if isinstance(info.get("mcpServers"), dict) else [],
            "legacy_history": len(info.get("history") or []),
        }
    mcp = [_mcp_entry(n, c, "user") for n, c in (data.get("mcpServers") or {}).items()] if isinstance(data.get("mcpServers"), dict) else []
    return {
        "exists": True, "path": str(path), "num_startups": data.get("numStartups"),
        "install_method": data.get("installMethod"), "auto_updates": data.get("autoUpdates"),
        "theme": data.get("theme"), "first_start": data.get("firstStartTime"),
        "has_user_id": bool(data.get("userID")), "onboarded": data.get("hasCompletedOnboarding"),
        "account": {k: acct.get(k) for k in ("emailAddress", "organizationName", "organizationRole", "displayName",
                                              "accountUuid", "organizationUuid") if acct.get(k)},
        "projects": projects, "mcp_servers": mcp, "tips_seen": len(data.get("tipsHistory") or {}),
        "feature_flags_cached": len(data.get("cachedGrowthBookFeatures") or {}) + len(data.get("cachedStatsigGates") or {}),
        "top_level_keys": sorted(data.keys()),
    }


def read_credentials(config_dir: Path) -> dict:
    """Presence + expiry only. Token values are never read into the result."""
    p = Path(config_dir) / ".credentials.json"
    data = _load(p)
    out: dict = {"file": p.is_file(), "keychain_note": "macOS stores these in the Keychain (service 'Claude Code-credentials')."}
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if isinstance(oauth, dict):
        exp = oauth.get("expiresAt")
        out.update({
            "has_access_token": bool(oauth.get("accessToken")), "has_refresh_token": bool(oauth.get("refreshToken")),
            "subscription": oauth.get("subscriptionType"), "rate_limit_tier": oauth.get("rateLimitTier"),
            "scopes": list(oauth.get("scopes") or []),
            "expires_at": datetime.fromtimestamp(exp / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if isinstance(exp, (int, float)) else None,
        })
    return out


def effective_mcp_servers(global_info: dict, project_mcp_files: Iterable[Path] = ()) -> list[dict]:
    out = list(global_info.get("mcp_servers", []))
    for f in project_mcp_files:
        data = _load(f)
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if isinstance(servers, dict):
            out += [_mcp_entry(n, c, f"project:{f.parent.name}") for n, c in servers.items()]
    return out


def project_mcp_files(project_paths: Iterable[str]) -> list[Path]:
    files = []
    for pp in project_paths:
        if pp and Path(pp).is_absolute():
            f = Path(pp) / ".mcp.json"
            try:
                if f.is_file():
                    files.append(f)
            except OSError:
                pass
    return files


def read_desktop_config(root: Path) -> dict:
    """``claude_desktop_config.json`` + ``config.json`` key names (no values)."""
    out: dict = {"claude_desktop_config": None, "config_json_keys": [], "mcp_servers": []}
    data = _load(Path(root) / "claude_desktop_config.json")
    if isinstance(data, dict):
        out["claude_desktop_config"] = str(Path(root) / "claude_desktop_config.json")
        out["mcp_servers"] = [_mcp_entry(n, c, "desktop") for n, c in (data.get("mcpServers") or {}).items()] \
            if isinstance(data.get("mcpServers"), dict) else []
        out["preferences"] = sorted((data.get("preferences") or {}).keys()) if isinstance(data.get("preferences"), dict) else []
    cfg = _load(Path(root) / "config.json")
    if isinstance(cfg, dict):
        out["config_json_keys"] = sorted(cfg.keys())
    return out
