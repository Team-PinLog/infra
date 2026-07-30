"""Deterministic Mattermost rendering and policy enforcement."""

from __future__ import annotations

from collections.abc import Mapping

from diagnostics import DiagnosticContext
from schema import validate_analysis
from security import redact_text, strip_mentions_and_urls

AUTOMATION_MARKER = "🤖 **[자동 알림 · SENTINEL]**"
SUMMARY_PREFIX = "**한 줄 요약:**"
_ICONS = {("firing", "critical"): ":red_circle:", ("firing", "warning"): ":warning:", ("resolved", "critical"): ":large_green_circle:", ("resolved", "warning"): ":large_green_circle:"}


def _safe(value: object, limit: int = 1000) -> str:
    return strip_mentions_and_urls(redact_text(value, limit))


def _trusted_grafana_link(value: object) -> str:
    link = str(value)
    if not link.startswith("https://monitoring.pin-log.com/explore"):
        return "https://monitoring.pin-log.com/explore"
    return redact_text(link, 1200)


def _context(item: Mapping[str, object]) -> DiagnosticContext:
    value = item.get("diagnostics")
    return value if isinstance(value, DiagnosticContext) else DiagnosticContext(
        area="Infra",
        facts=(_safe(item.get("description", "관측 세부 정보가 없습니다.")),),
        actions=(f"대상 {_safe(item.get('target', 'unknown'), 200)}의 상태와 최근 로그를 확인",),
        grafana_links=("https://monitoring.pin-log.com/explore",),
    )


def render_message(analysis: dict, item: dict[str, str]) -> str:
    analysis = validate_analysis(analysis)
    status, severity = item["status"], item["severity"]
    title = f"{_ICONS[(status, severity)]} **[{severity.upper()}][{_safe(item['environment'], 32)}][{_safe(item['source'], 32)}] {_safe(analysis['title'], 200)}**"
    if status == "resolved":
        lines = [
            AUTOMATION_MARKER,
            title,
            f"**상태:** 정상화 · 지속 시간: {_safe(item['duration'], 32)}",
            f"**사용자 영향:** {_safe(analysis['impact'])}",
            f"**확인 사실:** Alertmanager가 RESOLVED 상태를 전달했습니다.",
            f"{SUMMARY_PREFIX} {_safe(analysis['summary'])}",
        ]
    else:
        context = _context(item)
        facts = "; ".join(_safe(fact, 300) for fact in context.facts) or "근거가 부족합니다."
        actions = " / ".join(f"{number}) {_safe(action, 220)}" for number, action in enumerate(context.actions[:3], 1))
        links = " · ".join(f"[Explore {number}]({_trusted_grafana_link(link)})" for number, link in enumerate(context.grafana_links, 1))
        lines = [
            AUTOMATION_MARKER,
            title,
            f"**상태:** {status.upper()}, 지속 시간: {_safe(item['duration'], 32)}",
            f"**영역:** {_safe(context.area, 32)}",
            f"**사용자 영향:** {_safe(analysis['impact'])}",
            f"**쉬운 원인 설명:** 확인 사실: {facts} / 추정: {_safe(context.estimate)}",
            f"**확인된 사실:** {facts}",
            f"**평소 대비 핵심 수치:** {_safe(context.metrics)}",
            f"**지금 할 일:** {actions}",
            f"**근거/확신도:** {_safe(context.evidence, 200)} / {_safe(context.confidence, 32)}",
            f"**Grafana 링크:** {links or '제공할 수 없습니다.'}",
            "---",
            f"{SUMMARY_PREFIX} {_safe(analysis['summary'])}",
        ]
        if severity == "critical":
            lines.insert(1, "@channel")
    message = "\n".join(lines)
    expected_mentions = 1 if status == "firing" and severity == "critical" else 0
    if message.count("@channel") != expected_mentions:
        raise ValueError("mention policy violation")
    if len(lines) > 15:
        raise ValueError("message exceeds 15-line target")
    return message
