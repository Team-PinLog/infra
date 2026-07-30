"""Bounded, allowlisted Prometheus/Loki diagnostic enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import json
import math
from urllib.parse import urlencode

from security import redact_text
from runtime_diagnostics import LOGQL, PROMQL

WINDOW_SECONDS = 20 * 60
QUERY_LIMIT = 100
QUERY_TIMEOUT_SECONDS = 3
GRAFANA_BASE = "https://monitoring.pin-log.com/explore"

# Identifiers, not caller-supplied query text. Adapters map these to reviewed PromQL/LogQL.
QUERY_TEMPLATES = {
    "Backend": ("backend_http_5xx_ratio", "backend_error_logs"),
    "Frontend": ("frontend_availability", "frontend_error_logs"),
    "AI": ("ai_request_failure_ratio", "ai_error_logs"),
    "Infra": ("infra_ready_ratio", "infra_error_logs"),
}


@dataclass(frozen=True)
class DiagnosticContext:
    area: str = "Infra"
    facts: tuple[str, ...] = ()
    estimate: str = "근거가 부족해 원인을 특정할 수 없습니다."
    metrics: str = "비교 가능한 기준값이 없습니다."
    actions: tuple[str, ...] = ("Grafana에서 같은 시간대의 상태를 확인",)
    evidence: str = "Alertmanager payload"
    confidence: str = "낮음"
    grafana_links: tuple[str, ...] = ()


def classify_area(item: dict[str, str]) -> str:
    text = " ".join(str(item.get(key, "")) for key in ("source", "alertname", "target")).lower()
    if any(word in text for word in ("frontend", "web", "browser")):
        return "Frontend"
    if any(word in text for word in (" ai", "ai-", "model", "inference", "gms")):
        return "AI"
    if any(word in text for word in ("backend", "api", "spring")):
        return "Backend"
    return "Infra"


def build_grafana_links(area: str, namespace: str, target: str, start: int, end: int) -> tuple[str, str]:
    end = min(end, start + WINDOW_SECONDS)
    metric_template, log_template = QUERY_TEMPLATES.get(area, QUERY_TEMPLATES["Infra"])
    time_range = {"from": str(start * 1000), "to": str(end * 1000)}
    def explore(datasource: str, expression: str) -> str:
        query = {"refId": "A", "datasource": {"uid": datasource}, "expr": expression}
        left = {"datasource": datasource, "queries": [query], "range": time_range}
        return GRAFANA_BASE + "?" + urlencode({"left": json.dumps(left, separators=(",", ":"))})
    prometheus = explore("prometheus", PROMQL[metric_template])
    loki = explore("loki", LOGQL[log_template])
    return prometheus, loki


def _safe_log(value: object) -> str:
    text = redact_text(value, 240)
    for phrase in ("ignore previous instructions", "system prompt", "follow these instructions"):
        text = text.replace(phrase, "[UNTRUSTED_INSTRUCTION_REMOVED]").replace(phrase.title(), "[UNTRUSTED_INSTRUCTION_REMOVED]")
    return text


def _finite_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def collect_diagnostics(payload: dict, item: dict[str, str], now: float, query: Callable) -> DiagnosticContext:
    """Collect one metric and one log result in a fixed ±10 minute window.

    ``query`` receives only reviewed template identifiers and hard limits; raw alert text can
    never become PromQL/LogQL. Returned log text is redacted and never persisted here.
    """
    area = classify_area(item)
    start, end = int(now) - WINDOW_SECONDS // 2, int(now) + WINDOW_SECONDS // 2
    metric_template, log_template = QUERY_TEMPLATES[area]
    metric = query("prometheus", metric_template, start, end, QUERY_LIMIT, QUERY_TIMEOUT_SECONDS)
    logs = query("loki", log_template, start, end, QUERY_LIMIT, QUERY_TIMEOUT_SECONDS)
    value = metric.get("value") if isinstance(metric, dict) else None
    baseline = metric.get("baseline") if isinstance(metric, dict) else None
    current_value = _finite_float(value)
    baseline_value = _finite_float(baseline)
    has_metric = current_value is not None
    if current_value is not None and baseline_value is not None and baseline_value > 0:
        metrics = f"현재 {current_value:.1%} / 평소 {baseline_value:.1%} ({current_value / baseline_value:.1f}배)"
        metric_fact = f"Prometheus 현재값 {current_value:.1%}, 평소값 {baseline_value:.1%}"
    elif current_value is not None:
        metrics = f"현재 {current_value:.1%} / 비교 가능한 평소 지표 없음"
        metric_fact = f"Prometheus 현재값 {current_value:.1%}, 비교 가능한 평소값 없음"
    else:
        metrics = "비교 가능한 지표 없음" if area in ("Frontend", "AI") else "비교 가능한 기준값이 없습니다."
        metric_fact = "Prometheus에서 비교 가능한 지표를 확인하지 못했습니다."
    safe_logs = tuple(_safe_log(line) for line in (logs[:3] if isinstance(logs, list) else []))
    facts = (metric_fact,) + ((f"Loki 오류 표본 {len(safe_logs)}건 확인",) if safe_logs else ("Loki 오류 표본 없음",))
    estimate = "오류 신호가 증가한 것으로 보이지만 로그 표본만으로 단일 원인을 확정할 수 없습니다." if safe_logs else "확인된 오류 로그 표본이 없어 원인을 특정할 수 없습니다."
    actions = (f"{area} 담당자가 Grafana의 같은 시간대를 확인", "최근 배포와 오류 시작 시점을 대조", "공통 오류가 확인되면 담당자에게 공유")
    namespace = payload.get("commonLabels", {}).get("namespace", "pinlog-prod")
    evidence_parts = ["Alertmanager"]
    if has_metric:
        evidence_parts.append("Prometheus")
    if safe_logs:
        evidence_parts.append("Loki")
    evidence = " + ".join(evidence_parts) + (" payload" if len(evidence_parts) == 1 else "")
    confidence = "중간" if has_metric else "낮음"
    return DiagnosticContext(area, facts, estimate, metrics, actions, evidence, confidence, build_grafana_links(area, namespace, item["target"], start, end))
