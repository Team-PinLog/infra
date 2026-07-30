"""Safe stdlib-only Prometheus/Loki runtime adapter for reviewed diagnostics."""

from __future__ import annotations

import ipaddress
import json
import math
import statistics
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from evidence_parser import redact_line

MAX_WINDOW_SECONDS = 20 * 60
MAX_LIMIT = 100
MAX_TIMEOUT_SECONDS = 3
MAX_RESPONSE_BYTES = 64 * 1024
MIN_BASELINE_SAMPLES = 3

PROMQL = {
    "backend_http_5xx_ratio": 'sum(rate(http_server_requests_seconds_count{namespace="pinlog-prod",status=~"5.."}[5m])) / clamp_min(sum(rate(http_server_requests_seconds_count{namespace="pinlog-prod"}[5m])), 1)',
    "frontend_availability": 'min(up{namespace="pinlog-prod",job=~".*front.*"})',
    "ai_request_failure_ratio": 'sum(rate(http_server_requests_seconds_count{namespace="pinlog-prod",uri=~".*ai.*",status=~"5.."}[5m])) / clamp_min(sum(rate(http_server_requests_seconds_count{namespace="pinlog-prod",uri=~".*ai.*"}[5m])), 1)',
    "infra_ready_ratio": 'sum(kube_pod_status_ready{namespace="pinlog-prod",condition="true"}) / clamp_min(count(kube_pod_status_ready{namespace="pinlog-prod",condition="true"}), 1)',
}
LOGQL = {
    "backend_error_logs": '{namespace="pinlog-prod",container=~".*back.*"} |~ "(?i)error|exception|timeout"',
    "frontend_error_logs": '{namespace="pinlog-prod",container=~".*front.*"} |~ "(?i)error|exception|timeout"',
    "ai_error_logs": '{namespace="pinlog-prod"} |~ "(?i)ai|model|inference" |~ "(?i)error|exception|timeout"',
    "infra_error_logs": '{namespace="pinlog-prod"} |~ "(?i)error|failed|unhealthy"',
}


class ConfigurationError(ValueError):
    pass


class ResponseTooLarge(ValueError):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "redirect rejected", headers, fp)


def _endpoint(value: object, expected_port: int) -> str:
    parsed = urllib.parse.urlparse(str(value))
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ConfigurationError("diagnostic endpoint must be a bare HTTP URL")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("diagnostic endpoint host must be an IP address") from exc
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or port != expected_port:
        raise ConfigurationError("diagnostic endpoint is outside the allowed host/port range")
    return f"http://{address}:{port}"


class DiagnosticQueryAdapter:
    def __init__(self, config: dict, opener=None):
        if not isinstance(config, dict):
            raise ConfigurationError("diagnostic config must be an object")
        self.endpoints = {
            "prometheus": _endpoint(config.get("prometheus_url"), 9090),
            "loki": _endpoint(config.get("loki_url"), 3100),
        }
        if opener is None:
            safe_opener = urllib.request.build_opener(NoRedirectHandler())
            self.opener = lambda request, timeout: safe_opener.open(request, timeout=timeout)
        else:
            self.opener = opener

    @classmethod
    def from_file(cls, path: Path):
        raw = Path(path).read_bytes()
        if len(raw) > 4096:
            raise ConfigurationError("diagnostic config is too large")
        return cls(json.loads(raw))

    def __call__(self, source, template, start, end, limit, timeout):
        templates = PROMQL if source == "prometheus" else LOGQL if source == "loki" else None
        if templates is None or template not in templates:
            raise ValueError("diagnostic query template is not allowlisted")
        start = int(start)
        end = min(int(end), start + MAX_WINDOW_SECONDS)
        limit = max(1, min(int(limit), MAX_LIMIT))
        timeout = max(0.1, min(float(timeout), MAX_TIMEOUT_SECONDS))
        params = {"query": templates[template], "start": start, "end": end, "step": 60}
        if source == "loki":
            params.update({"limit": limit, "direction": "backward"})
        request = urllib.request.Request(
            self.endpoints[source] + "/api/v1/query_range?" + urllib.parse.urlencode(params),
            method="GET",
            headers={"Accept": "application/json"},
        )
        with self.opener(request, timeout) as response:
            if getattr(response, "status", 200) != 200:
                raise urllib.error.HTTPError(request.full_url, response.status, "query failed", {}, None)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge("diagnostic response exceeds cap")
        document = json.loads(raw)
        if document.get("status") != "success" or not isinstance(document.get("data", {}).get("result"), list):
            raise ValueError("invalid diagnostic response")
        result = document["data"]["result"]
        if source == "prometheus":
            values = result[0].get("values", []) if result and isinstance(result[0], dict) else []
            finite_values = []
            for pair in values:
                try:
                    value = float(pair[1])
                except (IndexError, KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    finite_values.append(value)
            if not finite_values:
                return {}
            output = {"value": finite_values[-1]}
            # Keep the current point out of the baseline. Three prior finite points
            # are the minimum useful sample for a deterministic robust median.
            prior = finite_values[:-1]
            if len(prior) >= MIN_BASELINE_SAMPLES:
                output["baseline"] = statistics.median(prior)
            return output
        lines = []
        for stream in result:
            source = "loki"
            if isinstance(stream, dict) and isinstance(stream.get("stream"), dict):
                labels = stream["stream"]
                source = str(labels.get("pod") or labels.get("container") or "loki")[:64]
            for pair in stream.get("values", []) if isinstance(stream, dict) else []:
                if isinstance(pair, list) and len(pair) == 2:
                    safe_line, _ = redact_line(pair[1])
                    lines.append({"timestamp": str(pair[0])[:64], "line": safe_line[:2048], "source": source})
                    if len(lines) >= limit:
                        return lines
        return lines
