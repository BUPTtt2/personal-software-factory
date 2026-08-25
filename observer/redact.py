from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_BEARER = re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s]+")
_ASSIGNMENT = re.compile(
    r'''(?ix)
    (?P<prefix>["']?(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|cookie|session)["']?\s*[:=]\s*)
    (?P<quote>["']?)(?P<value>[^\s,"'};]+)(?P=quote)
    ''',
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)
_STANDALONE_SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{16,}|sk_live_[A-Za-z0-9]{16,}|"
    r"(?:rk|pk)_live_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,}|"
    r"AIza[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{40,}(?![A-Za-z0-9])")
_PRIVATE_OUTPUT = re.compile(
    r"```|\bPRIVATE_[A-Z0-9_]+\b|\btool\s+(?:output|result)\b"
    r"|(?:^|\s)(?:def|class|function|const|let|var|import|from|export)\s+",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_KEYS = {
    "api_key", "apikey", "access_token", "refresh_token", "token", "password",
    "passwd", "secret", "cookie", "session",
}


@dataclass(frozen=True)
class RedactedText:
    text: str
    sha256: str
    was_redacted: bool


def _safe_controls(value: str) -> str:
    return "".join(char for char in value if char in "\n\t" or ord(char) >= 32)


def redact_text(value: str, max_chars: int = 500) -> RedactedText:
    raw = value if isinstance(value, str) else str(value)
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    if max_chars < 0:
        max_chars = 0
    try:
        safe = _safe_controls(raw)
        safe = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", safe)
        safe = _BEARER.sub(r"\1[REDACTED]", safe)
        safe = _ASSIGNMENT.sub(lambda match: f"{match.group('prefix')}[REDACTED]", safe)
        safe = _STANDALONE_SECRET.sub("[REDACTED TOKEN]", safe)
        safe = _URL_USERINFO.sub(r"\1[REDACTED]@", safe)
        changed = safe != raw
        suffix = "…[TRUNCATED]"
        if len(safe) > max_chars:
            safe = suffix[:max_chars] if max_chars <= len(suffix) else safe[: max_chars - len(suffix)] + suffix
            changed = True
        return RedactedText(safe, digest, changed)
    except Exception:
        return RedactedText("", digest, True)


def contains_sensitive_text(value: str) -> bool:
    return bool(
        _PRIVATE_KEY.search(value)
        or _BEARER.search(value)
        or _ASSIGNMENT.search(value)
        or _STANDALONE_SECRET.search(value)
        or _URL_USERINFO.search(value)
        or _contains_unknown_secret(value)
    )


def detect_sensitive_output(value: str) -> bool:
    """Detect content that must not cross compact public-summary boundaries."""
    return contains_sensitive_text(value) or bool(_PRIVATE_OUTPUT.search(value))


def _contains_unknown_secret(value: str) -> bool:
    for match in _LONG_TOKEN.finditer(value):
        candidate = match.group(0).rstrip("=")
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate):
            continue
        if any(char.isalpha() for char in candidate) and any(char.isdigit() for char in candidate):
            return True
    return False


def sanitize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else item))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except Exception:
        return ""
