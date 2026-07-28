"""Deterministic alert classification and fallback analysis."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from schema import PayloadError


def incident_key(payload: dict) -> str:
    alerts = payload.get("alerts", [])
    fingerprints = sorted(str(a.get("fingerprint", "")) for a in alerts if isinstance(a, dict))
    labels = payload.get("commonLabels", {}) if isinstance(payload.get("commonLabels"), dict) else {}
    canonical = {
        "groupKey": str(payload.get("groupKey", "")),
        "alertname": str(labels.get("alertname", "")),
        "namespace": str(labels.get("namespace", "")),
        "component": str(labels.get("component", "")),
        "fingerprints": fingerprints,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _duration(starts_at: object, now: float | None) -> str:
    if now is None or not isinstance(starts_at, str) or not starts_at:
        return "산정 불가"
    try:
        started = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        seconds = max(0, int(now - started.timestamp()))
    except (ValueError, OverflowError, OSError):
        return "산정 불가"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    return f"{minutes}분"


def triage(payload: dict, now: float | None = None) -> dict[str, str]:
    status = str(payload.get("status", "firing")).lower()
    if status not in {"firing", "resolved"}:
        raise PayloadError("unsupported alert status")
    severities: set[str] = set()
    active = []
    for alert in payload.get("alerts", []):
        alert_status = str(alert.get("status", status)).lower()
        if status == "firing" and alert_status != "firing":
            continue
        active.append(alert)
        severity = str(alert.get("labels", {}).get("severity", "")).lower()
        if severity in {"critical", "warning"}:
            severities.add(severity)
    severity = "critical" if "critical" in severities else "warning" if "warning" in severities else ""
    if not severity:
        common = str(payload.get("commonLabels", {}).get("severity", "")).lower()
        severity = common if common in {"critical", "warning"} else ""
    if not severity:
        raise PayloadError("unsupported alert severity")
    first = active[0] if active else payload["alerts"][0]
    labels = {**payload.get("commonLabels", {}), **first.get("labels", {})}
    annotations = {**payload.get("commonAnnotations", {}), **first.get("annotations", {})}
    return {
        "incident_key": incident_key(payload),
        "status": status,
        "severity": severity,
        "environment": str(labels.get("environment", "prod")).upper(),
        "source": str(labels.get("source", "monitoring")).upper(),
        "alertname": str(labels.get("alertname", "Alert")),
        "target": str(labels.get("pod") or labels.get("instance") or labels.get("namespace") or labels.get("component") or "unknown"),
        "description": str(annotations.get("description") or annotations.get("message") or annotations.get("summary") or "관측 세부 정보가 없습니다."),
        "duration": _duration(first.get("startsAt"), now),
    }


def build_fallback(item: dict[str, str]) -> dict[str, str]:
    resolved = item["status"] == "resolved"
    name = item["alertname"]
    return {
        "title": f"{name} {'복구' if resolved else '발생'}",
        "impact": "서비스가 정상 상태로 복구되었습니다." if resolved else "관련 서비스의 가용성 또는 성능에 영향이 있을 수 있습니다.",
        "observed": item["description"],
        "action": "추가 조치 없이 재발 여부를 관찰하세요." if resolved else f"대상 {item['target']}의 상태와 최근 로그를 확인하세요.",
        "summary": f"{name}이(가) 복구되었습니다." if resolved else f"{name}이(가) 진행 중이므로 담당자의 확인이 필요합니다.",
    }
