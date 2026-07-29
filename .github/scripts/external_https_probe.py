#!/usr/bin/env python3
"""External HTTPS/TLS probe for PinLog.

Designed for GitHub-hosted runners so the check survives a complete outage of the
single k3s node. State is stored in a non-secret repository file so Mattermost is
notified on failure, warning, and recovery transitions without spamming every
scheduled run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ProbeResult:
    status: str  # up | warning | down
    severity: str  # info | warning | critical
    summary: str
    details: list[str]
    cert_not_after: str | None = None
    cert_days_remaining: int | None = None
    http_status: int | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PinLog external HTTPS/TLS probe")
    parser.add_argument("--url", default=os.getenv("TARGET_URL", "https://i15a705.p.ssafy.io/grafana/login"))
    parser.add_argument("--expect-status", type=int, default=int(os.getenv("EXPECT_STATUS", "200")))
    parser.add_argument("--tls-warning-days", type=int, default=int(os.getenv("TLS_WARNING_DAYS", "14")))
    parser.add_argument("--tls-critical-days", type=int, default=int(os.getenv("TLS_CRITICAL_DAYS", "7")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("PROBE_TIMEOUT", "10")))
    parser.add_argument("--state-file", default=os.getenv("STATE_FILE", ".github/monitoring/external_https_state.json"))
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "false").lower() == "true")
    parser.add_argument("--force-notify", action="store_true", default=os.getenv("FORCE_NOTIFY", "false").lower() == "true")
    return parser.parse_args()


def host_port_from_url(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"only https URLs are supported: {url}")
    if not parsed.hostname:
        raise ValueError(f"URL has no hostname: {url}")
    return parsed.hostname, parsed.port or 443


def probe_tls(url: str, timeout: float) -> dt.datetime:
    host, port = host_port_from_url(url)
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as ssock:
            cert: dict[str, Any] | None = ssock.getpeercert()
    if not cert or not isinstance(cert.get("notAfter"), str):
        raise ssl.SSLError("peer certificate did not include notAfter")
    raw_not_after = cert["notAfter"]
    return dt.datetime.strptime(raw_not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.UTC)


def probe_http(url: str, expected_status: int, timeout: float) -> int:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "PinLog-External-Monitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return int(res.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def run_probe(args: argparse.Namespace) -> ProbeResult:
    details: list[str] = []
    try:
        not_after = probe_tls(args.url, args.timeout)
        now = dt.datetime.now(dt.UTC)
        remaining = not_after - now
        days = int(remaining.total_seconds() // 86400)
        details.append(f"TLS 인증서 만료: {not_after.isoformat()} ({days}일 남음)")
    except Exception as exc:  # noqa: BLE001 - summarized for alerting
        return ProbeResult(
            status="down",
            severity="critical",
            summary="TLS 연결 또는 인증서 검증 실패",
            details=[f"TLS 검사 오류: {type(exc).__name__}"],
        )

    try:
        http_status = probe_http(args.url, args.expect_status, args.timeout)
        details.append(f"HTTP 상태 코드: {http_status}, 기대값: {args.expect_status}")
    except Exception as exc:  # noqa: BLE001 - summarized for alerting
        return ProbeResult(
            status="down",
            severity="critical",
            summary="HTTPS 요청 실패",
            details=details + [f"HTTP 검사 오류: {type(exc).__name__}"],
            cert_not_after=not_after.isoformat(),
            cert_days_remaining=days,
        )

    if http_status != args.expect_status:
        return ProbeResult(
            status="down",
            severity="critical",
            summary=f"외부 HTTPS 상태 코드 비정상: {http_status}",
            details=details,
            cert_not_after=not_after.isoformat(),
            cert_days_remaining=days,
            http_status=http_status,
        )

    if days < args.tls_critical_days:
        return ProbeResult(
            status="down",
            severity="critical",
            summary=f"TLS 인증서 만료 임박: {days}일 남음",
            details=details,
            cert_not_after=not_after.isoformat(),
            cert_days_remaining=days,
            http_status=http_status,
        )

    if days < args.tls_warning_days:
        return ProbeResult(
            status="warning",
            severity="warning",
            summary=f"TLS 인증서 갱신 필요: {days}일 남음",
            details=details,
            cert_not_after=not_after.isoformat(),
            cert_days_remaining=days,
            http_status=http_status,
        )

    return ProbeResult(
        status="up",
        severity="info",
        summary="외부 HTTPS/TLS 검사 정상",
        details=details,
        cert_not_after=not_after.isoformat(),
        cert_days_remaining=days,
        http_status=http_status,
    )


def read_previous_status(state_file: str) -> str:
    override = os.getenv("PREVIOUS_STATUS")
    if override:
        return override
    path = Path(state_file)
    if not path.exists():
        return "unknown"
    try:
        data = json.loads(path.read_text())
        status = data.get("status")
        return status if isinstance(status, str) else "unknown"
    except Exception:  # noqa: BLE001 - tolerate corrupt state by alerting from unknown
        return "unknown"


def write_state_file(state_file: str, result: ProbeResult, url: str, dry_run: bool) -> None:
    state = {
        "status": result.status,
        "severity": result.severity,
        "summary": result.summary,
        "url": url,
        "http_status": result.http_status,
        "cert_not_after": result.cert_not_after,
        "cert_days_remaining": result.cert_days_remaining,
        "checked_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    comparable_keys = ["status", "severity", "summary", "url", "http_status", "cert_not_after"]
    path = Path(state_file)
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if all(existing.get(key) == state.get(key) for key in comparable_keys):
                print("state_unchanged=true")
                return
        except Exception:  # noqa: BLE001 - corrupt state should be replaced
            pass
    if dry_run:
        print(f"dry_run_state_file={state_file}")
        print(json.dumps(state, ensure_ascii=False, sort_keys=True))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def should_notify(previous: str, current: str, force_notify: bool) -> bool:
    if force_notify:
        return True
    if previous == current:
        return False
    if current in {"down", "warning"}:
        return True
    if current == "up" and previous in {"down", "warning"}:
        return True
    return False


def build_message(result: ProbeResult, previous: str, url: str, state_file: str) -> str:
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if result.status == "up" and previous in {"down", "warning"}:
        title = "[RESOLVED][prod][external-monitor] PinLog 외부 HTTPS/TLS 복구"
        action = "추가 조치 불필요. 재발 시 GitHub Actions 실행 로그와 Ingress/Traefik 상태를 확인하세요."
        final = "외부 HTTPS/TLS 검사가 복구되었고 현재 응답과 인증서 검증은 정상이며 즉시 필요한 조치는 없습니다."
    elif result.status == "up":
        title = "[INFO][prod][external-monitor] PinLog 외부 HTTPS/TLS 검사 정상"
        action = "추가 조치 불필요. 이 메시지는 수동 강제 알림 또는 초기 상태 확인용입니다."
        final = "PinLog 외부 HTTPS/TLS 검사는 정상이며 현재 사람이 해야 할 조치는 없습니다."
    elif result.status == "warning":
        title = "[WARNING][prod][external-monitor] PinLog TLS 인증서 만료 임박"
        action = "인증서 자동 갱신 경로와 Traefik TLSStore 상태를 확인하세요."
        final = "PinLog 외부 TLS 인증서 만료가 가까워졌으며 현재 서비스는 응답하지만 갱신 경로 확인이 필요합니다."
    else:
        title = "[CRITICAL][prod][external-monitor] PinLog 외부 HTTPS/TLS 검사 실패"
        action = "공개 DNS, Traefik LoadBalancer, Ingress, 노드 상태, TLS 인증서를 확인하세요."
        final = "PinLog 외부 HTTPS/TLS 검사에 실패했으며 사용자가 서비스에 접근하지 못할 수 있어 즉시 확인이 필요합니다."

    detail_lines = "\n".join(f"- {item}" for item in result.details)
    return (
        "🤖 **[자동 알림 · EXTERNAL MONITOR]**\n"
        f"{title}\n"
        f"- 대상: `{url}`\n"
        f"- 현재 상태: `{result.status}`\n"
        f"- 이전 상태: `{previous}`\n"
        f"- 검사 시각: `{now}`\n"
        f"- 상태 파일: `{state_file}`\n"
        f"- 요약: {result.summary}\n"
        "\n"
        "**관측값**\n"
        f"{detail_lines}\n"
        "\n"
        f"**필요 행동:** {action}\n"
        "---\n"
        f"**한 줄 요약:** {final}"
    )


def post_mattermost(webhook_url: str, message: str, dry_run: bool) -> None:
    if dry_run:
        print("dry_run_message_begin")
        print(message)
        print("dry_run_message_end")
        return
    body = json.dumps({"text": message}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "PinLog-External-Monitor/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        if res.status < 200 or res.status >= 300:
            raise RuntimeError(f"Mattermost webhook returned HTTP {res.status}")


def main() -> int:
    args = parse_args()
    result = run_probe(args)
    previous = read_previous_status(args.state_file)
    notify = should_notify(previous, result.status, args.force_notify)

    print(json.dumps({
        "url": args.url,
        "status": result.status,
        "severity": result.severity,
        "summary": result.summary,
        "previous": previous,
        "notify": notify,
        "http_status": result.http_status,
        "cert_days_remaining": result.cert_days_remaining,
    }, ensure_ascii=False))

    if notify:
        webhook = os.getenv("MATTERMOST_WEBHOOK_URL", "")
        if not webhook and not args.dry_run:
            raise RuntimeError("MATTERMOST_WEBHOOK_URL is required when notification is needed")
        post_mattermost(webhook, build_message(result, previous, args.url, args.state_file), args.dry_run)

    write_state_file(args.state_file, result, args.url, args.dry_run)
    return 1 if result.status == "down" else 0


if __name__ == "__main__":
    raise SystemExit(main())
