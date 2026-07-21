#!/usr/bin/env python3
"""External HTTPS/TLS probe for PinLog.

Designed for GitHub-hosted runners so the check survives a complete outage of the
single k3s node. State is stored in a GitHub Actions repository variable so
Mattermost is notified on failure, warning, and recovery transitions without
spamming every scheduled run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
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
    parser.add_argument("--state-key", default=os.getenv("STATE_KEY", "PINLOG_EXTERNAL_MONITOR_STATUS"))
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


def probe_tls(url: str, timeout: float) -> tuple[dt.datetime, str]:
    host, port = host_port_from_url(url)
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as ssock:
            cert: dict[str, Any] | None = ssock.getpeercert()
    if not cert or not isinstance(cert.get("notAfter"), str):
        raise ssl.SSLError("peer certificate did not include notAfter")
    raw_not_after = cert["notAfter"]
    not_after = dt.datetime.strptime(raw_not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.UTC)
    return not_after, raw_not_after


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
        not_after, raw_not_after = probe_tls(args.url, args.timeout)
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


def gh_variable_get(name: str) -> str:
    override = os.getenv("PREVIOUS_STATUS")
    if override:
        return override
    try:
        result = subprocess.run(
            ["gh", "variable", "get", name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return "unknown"


def gh_variable_set(name: str, value: str, dry_run: bool) -> None:
    if dry_run:
        print(f"dry_run_state_set {name}={value}")
        return
    subprocess.run(["gh", "variable", "set", name, "--body", value], check=True)


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


def build_message(result: ProbeResult, previous: str, url: str, state_key: str) -> str:
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if result.status == "up" and previous in {"down", "warning"}:
        title = "[RESOLVED][prod][external-monitor] PinLog 외부 HTTPS/TLS 복구"
        action = "추가 조치 불필요. 재발 시 GitHub Actions 실행 로그와 Ingress/Traefik 상태를 확인하세요."
        final = "외부 HTTPS/TLS 검사가 복구되었고 현재 응답과 인증서 검증은 정상이며 즉시 필요한 조치는 없습니다."
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
        f"- 상태 저장 키: `{state_key}`\n"
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
    previous = gh_variable_get(args.state_key)
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
        post_mattermost(webhook, build_message(result, previous, args.url, args.state_key), args.dry_run)

    gh_variable_set(args.state_key, result.status, args.dry_run)
    return 1 if result.status == "down" else 0


if __name__ == "__main__":
    raise SystemExit(main())
