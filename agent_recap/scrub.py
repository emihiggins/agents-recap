"""Redact credential-shaped text.

This runs before excerpts are written to the persistent store, before they are
sent to any model, and before they are rendered into HTML. The store is the case
that matters most: a leaked credential there outlives the session it came from.
"""

from __future__ import annotations

import re

PLACEHOLDER = "«redacted»"

# Ordered: more specific patterns first, so a keyed assignment does not get
# half-eaten by the generic long-blob rule.
_PATTERNS: list[re.Pattern[str]] = [
    # Provider API keys
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    # JWTs: header.payload.signature
    re.compile(r"\beyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}"),
    # Private key blocks
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----", re.S),
    # Any identifier containing a secret-ish word, assigned a value.
    # Deliberately matches the word anywhere in the name so underscored
    # variables like AWS_SECRET_ACCESS_KEY are caught (\bsecret\b would not).
    re.compile(
        r"(?i)\b([A-Za-z0-9_.-]*"
        r"(?:secret|passwo?rd|passwd|api[_-]?key|access[_-]?key|auth[_-]?token|"
        r"[_-]token|^token|credential|encryptionkey)"
        r"[A-Za-z0-9_.-]*)(\s*[=:]\s+|\s*[=:]\s*)[\"']?([^\s\"',;]{6,})[\"']?"
    ),
    # Bearer <value>
    re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9\-._~+/]{12,}=*)"),
    # .env style SHOUTY_NAME=longvalue, with or without a leading `export`
    re.compile(r"(?m)^\s*(?:export\s+)?([A-Z][A-Z0-9_]{3,})=(\S{8,})$"),
    # Bare long base64/hex runs
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"),
]

# Fields that are themselves key material and must never be persisted.
_SECRET_KEY_RE = re.compile(r"(?i)(encryptionkey|secretkey|privatekey|apikey|access_token)$")


def _replace_keyed(match: re.Match[str]) -> str:
    """Keep the field name, drop the value, so context survives redaction."""
    groups = match.groups()
    name = groups[0]
    sep = groups[1] if len(groups) > 2 else "="
    if len(groups) == 2:  # .env style
        return f"{name}={PLACEHOLDER}"
    return f"{name}{sep}{PLACEHOLDER}"


def scrub(text: str | None) -> str | None:
    """Return `text` with credential-shaped substrings replaced."""
    if not text:
        return text
    out = text
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            out = pattern.sub(_replace_keyed, out)
        else:
            out = pattern.sub(PLACEHOLDER, out)
    return out


def is_secret_key(name: str) -> bool:
    """True for dict keys whose *values* are key material (e.g. blobEncryptionKey)."""
    return bool(_SECRET_KEY_RE.search(name or ""))


def scrub_obj(obj):
    """Recursively scrub a JSON-ish structure, dropping secret-named keys."""
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items() if not is_secret_key(k)}
    if isinstance(obj, list):
        return [scrub_obj(v) for v in obj]
    if isinstance(obj, str):
        return scrub(obj)
    return obj
