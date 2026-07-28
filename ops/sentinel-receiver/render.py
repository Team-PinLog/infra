"""Deterministic Mattermost rendering and policy enforcement."""

from __future__ import annotations

from schema import validate_analysis
from security import redact_text, strip_mentions_and_urls

AUTOMATION_MARKER = "🤖 **[자동 알림 · SENTINEL]**"
SUMMARY_PREFIX = "**한 줄 요약:**"
_ICONS = {("firing", "critical"): ":red_circle:", ("firing", "warning"): ":warning:", ("resolved", "critical"): ":large_green_circle:", ("resolved", "warning"): ":large_green_circle:"}


def _safe(value: object, limit: int = 1000) -> str:
    return strip_mentions_and_urls(redact_text(value, limit))


def render_message(analysis: dict, item: dict[str, str]) -> str:
    analysis = validate_analysis(analysis)
    status, severity = item["status"], item["severity"]
    title = f"{_ICONS[(status, severity)]} **[{severity.upper()}][{_safe(item['environment'], 32)}][{_safe(item['source'], 32)}] {_safe(analysis['title'], 200)}**"
    lines = [
        AUTOMATION_MARKER,
        title,
        f"**상태:** {status.upper()}, 지속 시간: {_safe(item['duration'], 32)}",
        f"**대상:** `{_safe(item['target'], 200)}`",
        f"**영향:** {_safe(analysis['impact'])}",
        f"**확인된 사실:** {_safe(analysis['observed'])}",
        f"**첫 조치:** {_safe(analysis['action'])}",
        "---",
        f"{SUMMARY_PREFIX} {_safe(analysis['summary'])}",
    ]
    if status == "firing" and severity == "critical":
        lines.insert(1, "@channel")
    message = "\n".join(lines)
    if message.count("@channel") != (1 if status == "firing" and severity == "critical" else 0):
        raise ValueError("mention policy violation")
    if len(lines) > 15:
        raise ValueError("message exceeds 15-line target")
    return message
