"""Security boundaries for untrusted alert text."""

from __future__ import annotations

import re

_REDACTIONS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:token|password|passwd|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(https?://[^\s/]+/hooks/)[^\s)>]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(ignore|disregard|override)\s+(?:all\s+)?(?:previous|prior|system)\s+instructions?\b"), "[UNTRUSTED_INSTRUCTION_REDACTED]"),
)
_MENTIONS = re.compile(r"(?i)(?<![\w])@[a-z0-9._-]+")
_URLS = re.compile(r"https?://[^\s)>]+", re.IGNORECASE)
_MARKDOWN_URLS = re.compile(r"\[([^\]]{0,200})\]\(https?://[^)\s]+\)", re.IGNORECASE)


def redact_text(value: object, limit: int = 2048) -> str:
    text = str(value).replace("\x00", " ")[:limit]
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def strip_mentions_and_urls(value: object) -> str:
    text = _MARKDOWN_URLS.sub(r"\1", str(value))
    text = _URLS.sub("[링크 제거됨]", text)
    return " ".join(_MENTIONS.sub("", text).split())
