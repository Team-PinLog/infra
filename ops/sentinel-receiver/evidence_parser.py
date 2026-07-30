"""Deterministic, bounded evidence reduction before any Sentinel AI call."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_EVENT_BYTES = 2048
MAX_EVENT_LINES = 32
MAX_EVIDENCE_BYTES = 768
MAX_LOG_TOTAL = 6 * 1024
MAX_METRIC_BYTES = 2 * 1024
MAX_INPUT_BYTES = 12 * 1024
SENSITIVE_QUERY = {"access_token", "api_key", "apikey", "auth", "authorization", "cookie", "jwt", "password", "secret", "session", "token"}

_INJECTION = re.compile(r"(?is)(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?)|(?:reveal|print|show)\s+(?:the\s+)?system\s+prompt|follow\s+(?:these|my)\s+instructions?")
_PEM = re.compile(r"(?is)-----BEGIN [^-\r\n]{1,64}-----.*?-----END [^-\r\n]{1,64}-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{4,})?\b")
_AUTH = re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;]+")
_SECRET = re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|cookie|session(?:id)?)\s*[:=]\s*[^\s,;]+")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()-]{7,}\d)(?!\w)")
_HIGH_ENTROPY = re.compile(r"\b(?=[A-Za-z0-9_+/-]{20,}\b)(?=[A-Za-z0-9_+/-]*[A-Z])(?=[A-Za-z0-9_+/-]*[a-z0-9])[A-Za-z0-9_+/-]{20,}={0,2}\b")


def _url_redact(match: re.Match) -> str:
    try:
        value = match.group(0)
        parsed = urlsplit(value)
        host = parsed.hostname or "redacted.invalid"
        netloc = host + ((f":{parsed.port}") if parsed.port else "")
        query = urlencode([(key, "[REDACTED]" if key.lower() in SENSITIVE_QUERY else val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)])
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except (ValueError, UnicodeError):
        return "[REDACTED_URL]"


def redact_line(value: object) -> tuple[str, set[str]]:
    text = str(value)
    flags: set[str] = set()
    if _INJECTION.search(text):
        text = _INJECTION.sub("[UNTRUSTED_INSTRUCTION_REMOVED]", text)
        flags.add("prompt_injection")
    text = _PEM.sub("[REDACTED_PEM]", text)
    text = re.sub(r"https?://[^\s<>\]]+", _url_redact, text, flags=re.I)
    for pattern, replacement in ((_AUTH, "authorization=[REDACTED]"), (_JWT, "[REDACTED_JWT]"), (_SECRET, "secret=[REDACTED]"), (_EMAIL, "[REDACTED_EMAIL]"), (_PHONE, "[REDACTED_PHONE]"), (_HIGH_ENTROPY, "[REDACTED_HIGH_ENTROPY]")):
        text = pattern.sub(replacement, text)
    return text, flags


def _utf8_cut(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", "ignore"), True


def normalize_event(value: object) -> tuple[str, str, tuple[str, ...]]:
    redacted, flags = redact_line(value)
    redacted, flags2 = redact_line(redacted)  # event-level second pass
    flags |= flags2
    redacted = unicodedata.normalize("NFKC", redacted)
    redacted = "".join(ch for ch in redacted if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    redacted, truncated = _utf8_cut(redacted, MAX_EVENT_BYTES)
    if truncated:
        flags.add("truncated")
    normalized = redacted.lower()
    substitutions = (
        (r"\b\d{4}-\d\d-\d\d[t ]\d\d:\d\d:\d\d(?:\.\d+)?(?:z|[+-]\d\d:?\d\d)?\b", "<timestamp>"),
        (r"\b(?:request|trace|span)[_-]?id\s*[:=]\s*[\w.-]+", "<id>"),
        (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", "<uuid>"),
        (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>"),
        (r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", "<hex>"),
        (r"\b\d+(?:\.\d+)?\s*(?:ns|us|µs|ms|s|sec|seconds?|minutes?|hours?)\b", "<duration>"),
        (r"\b\d+(?:\.\d+)?\s*(?:b|kb|kib|mb|mib|gb|gib|tb|tib)\b", "<size>"),
        (r"(?m)^\s*at\s+.*?(?:\([^\n]*:\d+\)|:\d+)\s*$", "<stack_frame>"),
        (r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", "<number>"),
    )
    for pattern, replacement in substitutions:
        normalized = re.sub(pattern, replacement, normalized, flags=re.I)
    normalized = re.sub(r"[ \t]+", " ", normalized).strip()
    return normalized, redacted, tuple(sorted(flags))


def _events(records: list[dict]) -> list[dict]:
    output: list[dict] = []
    for record in records[:100]:
        if not isinstance(record, dict) or not isinstance(record.get("line"), str):
            continue
        line, line_flags = redact_line(record["line"])
        continuation = bool(re.match(r"^\s+(?:at\s|\.\.\.|caused by:)", line, re.I))
        if continuation and output and output[-1]["line_count"] < MAX_EVENT_LINES and len((output[-1]["text"] + "\n" + line).encode()) <= MAX_EVENT_BYTES:
            output[-1]["text"] += "\n" + line
            output[-1]["line_count"] += 1
            output[-1]["flags"].update(line_flags)
            output[-1]["truncated"] = output[-1]["truncated"] or False
        elif continuation and output:
            output[-1]["truncated"] = True
        else:
            output.append({"text": line, "timestamp": str(record.get("timestamp", ""))[:64], "source": str(record.get("source", "loki"))[:64], "line_count": 1, "flags": set(line_flags), "truncated": False})
    return output


def _score(message: str, count: int, flags: tuple[str, ...]) -> int:
    severity = 4 if re.search(r"panic|fatal|exception", message, re.I) else 3 if re.search(r"error|failed", message, re.I) else 2 if re.search(r"timeout|warn", message, re.I) else 1
    return severity * 100 + min(count, 99) - (20 if "prompt_injection" in flags else 0)


def _metric(metric: dict) -> dict:
    def finite(key):
        value = metric.get(key)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None
    current, baseline = finite("current"), finite("baseline")
    if current is None:
        return {}
    result = {"current": current, "baseline": baseline, "delta": current - baseline if baseline is not None else None, "ratio": current / baseline if baseline not in (None, 0) else None, "anomaly": bool(baseline is not None and abs(current - baseline) > max(abs(baseline), .01))}
    return result if len(json.dumps(result).encode()) <= MAX_METRIC_BYTES else {}


def build_ai_evidence(incident: dict, metric: dict, records: list[dict]) -> dict | None:
    aggregates: dict[str, dict] = {}
    global_flags: set[str] = set()
    for event in _events(records if isinstance(records, list) else []):
        normalized, message, flags = normalize_event(event["text"])
        if not normalized or normalized in {"[untrusted_instruction_removed]", "[redacted]"}:
            continue
        signature = hashlib.sha256(b"sig-v1\0" + normalized.encode()).hexdigest()[:16]
        global_flags.update(flags)
        entry = aggregates.get(signature)
        if entry is None:
            message, cut = _utf8_cut(message, 480)
            entry = aggregates[signature] = {"signature_id": signature, "count": 0, "first_seen": event["timestamp"], "last_seen": event["timestamp"], "sources": set(), "message": message, "truncated": event["truncated"] or cut, "flags": flags}
        entry["count"] += 1
        entry["first_seen"] = min(entry["first_seen"], event["timestamp"])
        entry["last_seen"] = max(entry["last_seen"], event["timestamp"])
        entry["sources"].add(event["source"])
        entry["truncated"] = entry["truncated"] or event["truncated"]
    evidence = []
    for entry in aggregates.values():
        item = {key: value for key, value in entry.items() if key != "sources"}
        item["sources_count"] = len(entry["sources"])
        item["score"] = _score(item["message"], item["count"], item["flags"])
        evidence.append(item)
    evidence.sort(key=lambda e: (-e["score"], -e["count"], str(e["last_seen"]), e["signature_id"]))
    # last_seen descending without relying on numeric timestamp shape
    evidence = sorted(evidence, key=lambda e: e["signature_id"])
    evidence = sorted(evidence, key=lambda e: str(e["last_seen"]), reverse=True)
    evidence = sorted(evidence, key=lambda e: e["count"], reverse=True)
    evidence = sorted(evidence, key=lambda e: e["score"], reverse=True)[:5]
    kept, used = [], 0
    for item in evidence:
        item["flags"] = list(item["flags"])
        size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
        if size <= MAX_EVIDENCE_BYTES and used + size <= MAX_LOG_TOTAL:
            kept.append(item); used += size
    metric_evidence = _metric(metric if isinstance(metric, dict) else {})
    if not kept and not metric_evidence:
        return None
    safe_incident = {key: str(incident.get(key, ""))[:128] for key in ("status", "severity", "alertname", "source", "target")}
    document = {"schema_version": "sentinel-evidence-v1", "incident": safe_incident, "metric_evidence": metric_evidence, "log_evidence": kept, "flags": sorted(global_flags)}
    while len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_INPUT_BYTES and document["log_evidence"]:
        document["log_evidence"].pop()
    if len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_INPUT_BYTES:
        return None
    return document
