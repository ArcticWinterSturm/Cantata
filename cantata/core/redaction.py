"""PII / secret redaction.

Levels
------
``standard``  hard secrets + e-mail addresses + home-directory paths / user names.
              Session ids trimmed to 8 chars, tool-call ids to 12.
``paranoid``  standard + every absolute path, URL, IP / MAC address, phone number;
              any UUID trimmed to 4 chars; ids trimmed to 4 / 6 chars.
``none``      keep prompts, paths and e-mails verbatim.

**Hard secrets are redacted at every level, including ``none``**: Anthropic / OpenAI /
cloud API keys, OAuth access+refresh tokens, JWTs, bearer tokens, private keys, credential
JSON blobs, ``KEY=value`` style secrets, URL credentials, and account / organisation /
device identifiers.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

LEVELS = ("standard", "paranoid", "none")

_EMAIL_ALLOW = re.compile(r"(?i)^(?:noreply|no-reply)@(?:anthropic\.com|github\.com)$|@users\.noreply\.github\.com$")
_NON_SECRET_VALUES = {"true", "false", "null", "none", "undefined", "yes", "no"}
_COUNT_NAME = re.compile(
    r"(?i)(?:^|[_.\-])(?:max|min|input|output|total|cache|reasoning|prompt|completion|context|num|budget)"
    r"(?:[_.\-]\w+)*[_.\-]?tokens?$|^tokens?_?(?:used|count|limit|budget)$")

_SECRET_NAME = (r"(?:api[_\-]?key|secret|token|password|passwd|passphrase|private[_\-]?key|access[_\-]?key|"
                r"client[_\-]?secret|auth[_\-]?key|credential)s?")

# (label, compiled regex, replacement)  - ordered: specific before generic
_HARD: list[tuple[str, re.Pattern, Any]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
                               re.S), "[PRIVATE-KEY]"),
    ("oauth-blob", re.compile(r'("claudeAiOauth"\s*:\s*)\{[^{}]*\}', re.S), r'\1{"redacted": true}'),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), "[ANTHROPIC-KEY]"),
    ("api-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), "[API-KEY]"),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), "[GITHUB-TOKEN]"),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b"), "[AWS-KEY]"),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[GOOGLE-KEY]"),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[SLACK-TOKEN]"),
    ("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), "[STRIPE-KEY]"),
    ("token", re.compile(r"\b(?:npm_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}|SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,})"),
     "[TOKEN]"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[JWT]"),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]{16,}=*"), "Bearer [TOKEN]"),
    # [ \t]* (never \s*): \s* would swallow newlines and make a long run of blank lines quadratic
    ("auth-header", re.compile(r"(?im)^([ \t]*(?:authorization|proxy-authorization|x-api-key|api-key|cookie|set-cookie)"
                               r"[ \t]*:[ \t]*)(?!\[REDACTED\]).+$"), r"\1[REDACTED]"),
    ("url-credentials", re.compile(r"\b([a-z][a-z0-9+.\-]{1,20}://)([^/\s:@\"']+):([^/\s@\"']+)@"), r"\1[USER]:[PASS]@"),
    ("account-id", re.compile(r'(?i)("(?:accountUuid|organizationUuid|organization_uuid|orgId|organization_id|'
                              r'account_uuid|userID|user_id|deploymentOrganizationUuid|enduser\.id|anonymousId|'
                              r'deviceId|device_id|ant-did|stableId)"\s*:\s*)"[^"]+"'), r'\1"[ID]"'),
]

# name parts are length-bounded ({0,40}) so a long run of identifier characters cannot cause quadratic backtracking
_JSON_SECRET = re.compile(r'(?i)("[A-Za-z0-9_.\-]{0,40}' + _SECRET_NAME + r'[A-Za-z0-9_.\-]{0,40}"[ \t\r\n]{0,20}:[ \t\r\n]{0,20})"([^"\\]{6,})"')
_KV_SECRET = re.compile(r"(?i)\b([A-Za-z0-9_.\-]{0,40}" + _SECRET_NAME + r"[A-Za-z0-9_.\-]{0,40})([ \t]{0,20}[=:][ \t]{0,20})([\"']?)"
                        r"([^\s\"'`,;&<>{}\[\]]{6,})(\3)")
_SECRET_KW = re.compile(r"(?i)" + _SECRET_NAME)              # cheap linear prefilter for the two patterns above

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63})*\.[A-Za-z]{2,}\b")
_WIN_USER = re.compile(r"(?i)\b([A-Z]:[\\/]+Users[\\/]+)(?!\[USER\])([^\\/\s\"'<>|:*?]+)")
_POSIX_USER = re.compile(r"(?<![\w.\-])(/(?:home|Users)/)(?!\[USER\]|Shared\b|Public\b)([^/\s\"'<>|:*?]+)")
_URL = re.compile(r"\bhttps?://([A-Za-z0-9.\-_]+|\[[0-9a-fA-F:]+\])(?::\d+)?(?:/[^\s\"'<>)\]]*)?")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b")
_PHONE = re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\w)(?!\.\d)")
_TILDE_ABS = re.compile(r"(?<![\w~])~[\\/](?:[\w.\-~@+%]+[\\/])*[\w.\-~@+%]+[\\/]?")
_WIN_ABS = re.compile(r"\b[A-Za-z]:[\\/][^\s\"'<>|*?`]+")
_POSIX_ABS = re.compile(r"(?<![\w.:/\-~@])/(?:[\w.\-~@+%]+/)+[\w.\-~@+%]+/?")
_UUID = re.compile(r"\b([0-9a-fA-F]{4})[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


class Redactor:
    """Stateless apart from its level, home directories and counters."""

    def __init__(self, level: str = "standard", home_dirs: Iterable[Path | str] = ()):
        if level not in LEVELS:
            raise ValueError(f"unknown redaction level: {level!r}")
        self.level = level
        self.counts: Counter = Counter()
        homes = [str(h) for h in home_dirs if h]
        self._homes = sorted({h.rstrip("\\/") for h in homes if len(h.rstrip("\\/")) > 3},
                             key=len, reverse=True)

    # ------------------------------------------------------------------ text --
    def hard(self, text: str) -> str:
        """Hard secrets only (applied at *every* level, including ``none``)."""
        if not text:
            return text or ""
        for label, rx, repl in _HARD:
            text = rx.sub(self._counting(label, repl), text)
        if _SECRET_KW.search(text):
            text = _JSON_SECRET.sub(self._json_secret, text)
            text = _KV_SECRET.sub(self._kv_secret, text)
        return text

    def apply(self, text: Any) -> str:
        """Full redaction for free text according to the configured level."""
        if text is None:
            return ""
        text = str(text)
        if not text:
            return text
        text = self.hard(text)
        if self.level == "none":
            return text
        if self.level == "paranoid":
            text = _URL.sub(self._url_repl, text)
        if "@" in text:
            text = _EMAIL.sub(self._email_repl, text)
        text = self._scrub_homes(text)
        if self.level == "paranoid":
            text = _IPV4.sub(lambda m: self._bump("ip", "[IP]"), text)
            text = _MAC.sub(lambda m: self._bump("mac", "[MAC]"), text)
            text = _PHONE.sub(lambda m: self._bump("phone", "[PHONE]"), text)
            text = _TILDE_ABS.sub(self._abs_repl, text)
            text = _WIN_ABS.sub(self._abs_repl, text)
            text = _POSIX_ABS.sub(self._abs_repl, text)
            text = _UUID.sub(lambda m: self._bump("uuid", m.group(1) + "…"), text)
        return text

    # --------------------------------------------------------------- fields ---
    def path(self, value: Any) -> str:
        """Display form of a filesystem path."""
        text = self.hard(str(value or ""))
        if self.level == "none" or not text:
            return text
        if self.level == "paranoid":
            base = re.split(r"[\\/]", text.rstrip("\\/"))[-1]
            return f"[PATH]/{base}" if base else "[PATH]"
        return self._scrub_homes(text)

    def url(self, value: Any) -> str:
        text = self.hard(str(value or ""))
        if self.level == "paranoid":
            return _URL.sub(self._url_repl, text)
        return text

    def session_id(self, value: Any) -> str:
        text = str(value or "")
        if self.level == "none":
            return text
        return text[: 4 if self.level == "paranoid" else 8]

    def call_id(self, value: Any) -> str:
        text = str(value or "")
        if self.level == "none":
            return text
        return text[: 6 if self.level == "paranoid" else 12]

    def person(self, value: Any) -> str:
        """Names / e-mails that come from account metadata (manifests, exports)."""
        text = str(value or "")
        if not text or self.level == "none":
            return self.hard(text)
        return "[NAME]" if "@" not in text else "[EMAIL]"

    def scrub(self, obj: Any, _key: str = "") -> Any:
        """Recursively redact a JSON-like structure (config dicts, manifests)."""
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                ks = str(k)
                if _looks_secret_key(ks) and isinstance(v, (str, int, float)) and str(v) and not _benign(ks, v):
                    out[ks] = "[REDACTED]"
                elif re.fullmatch(r"(?i)(accountUuid|organizationUuid|userID|emailAddress|email|accountName|"
                                  r"displayName|full_name|email_address|organizationName|ant-did)", ks):
                    out[ks] = "[ID]" if "uuid" in ks.lower() or ks in ("userID", "ant-did") else self.person(v)
                else:
                    out[ks] = self.scrub(v, ks)
            return out
        if isinstance(obj, list):
            return [self.scrub(v, _key) for v in obj]
        if isinstance(obj, str):
            return self.apply(obj)
        return obj

    def summary(self) -> dict[str, int]:
        return dict(self.counts)

    # ------------------------------------------------------------- internals --
    def _bump(self, label: str, repl: str) -> str:
        self.counts[label] += 1
        return repl

    def _counting(self, label: str, repl: Any):
        def fn(m: re.Match) -> str:
            self.counts[label] += 1
            return m.expand(repl) if isinstance(repl, str) else repl(m)
        return fn

    def _json_secret(self, m: re.Match) -> str:
        key, value = m.group(1), m.group(2)
        if _benign(key, value):
            return m.group(0)
        self.counts["secret"] += 1
        return f'{key}"[REDACTED]"'

    def _kv_secret(self, m: re.Match) -> str:
        name, sep, q1, value, q2 = m.groups()
        if _benign(name, value) or value.startswith("[") or "REDACTED" in value:
            return m.group(0)
        self.counts["secret"] += 1
        return f"{name}{sep}{q1}[REDACTED]{q2}"

    def _email_repl(self, m: re.Match) -> str:
        if _EMAIL_ALLOW.search(m.group(0)):
            return m.group(0)
        return self._bump("email", "[EMAIL]")

    def _url_repl(self, m: re.Match) -> str:
        return self._bump("url", f"[URL:{m.group(1)}]")

    def _abs_repl(self, m: re.Match) -> str:
        text = m.group(0)
        if text.startswith("[") or "[PATH]" in text:
            return text
        base = re.split(r"[\\/]", text.rstrip("\\/"))[-1]
        self.counts["path"] += 1
        return f"[PATH]/{base}" if base else "[PATH]"

    def _scrub_homes(self, text: str) -> str:
        for home in self._homes:
            variants = {home, home.replace("\\", "/"), home.replace("/", "\\")}
            for v in variants:
                if v and v in text:
                    self.counts["home"] += text.count(v)
                    text = text.replace(v, "~")
        text = _WIN_USER.sub(lambda m: self._bump("user", m.group(1) + "[USER]"), text)
        text = _POSIX_USER.sub(lambda m: self._bump("user", m.group(1) + "[USER]"), text)
        return text


def _looks_secret_key(key: str) -> bool:
    return bool(re.search(r"(?i)" + _SECRET_NAME, key)) and not _COUNT_NAME.search(key)


def _benign(name: str, value: Any) -> bool:
    v = str(value).strip().strip("\"'")
    if not v or v.lower() in _NON_SECRET_VALUES:
        return True
    if re.fullmatch(r"[\d.,_]+", v):
        return True
    return bool(_COUNT_NAME.search(name.strip("\"' ")))


_MARKER_PATTERNS = [
    ("email", _EMAIL), ("phone", _PHONE), ("ipv4", _IPV4),
    ("anthropic-key", _HARD[2][1]), ("api-key", _HARD[3][1]), ("github-token", _HARD[4][1]), ("aws-key", _HARD[5][1]),
    ("google-key", _HARD[6][1]), ("slack-token", _HARD[7][1]), ("jwt", _HARD[10][1]), ("private-key", _HARD[0][1]),
]


def count_markers(text: str) -> Counter:
    """How many PII / secret-looking values ``text`` contains (counts only - values are never returned)."""
    out: Counter = Counter()
    if not text:
        return out
    for label, rx in _MARKER_PATTERNS:
        if label == "email":
            k = sum(1 for m in rx.finditer(text) if not _EMAIL_ALLOW.search(m.group(0)))
        else:
            k = sum(1 for _ in rx.finditer(text))
        if k:
            out[label] += k
    if out.get("api-key"):                       # the generic sk-... pattern also matches sk-ant-... keys
        out["api-key"] = max(0, out["api-key"] - out.get("anthropic-key", 0))
        if not out["api-key"]:
            del out["api-key"]
    return out
