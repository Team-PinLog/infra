# PinLog Sentinel Receiver

`diagnostics.py`는 FIRING 알림에만 allowlist Prometheus/Loki 진단을 추가한다. query
adapter에는 임의 query가 아니라 검토된 template identifier, 최대 20분 window,
100개 결과, 3초 timeout만 전달한다. RESOLVED는 adapter와 AI를 모두 건너뛴다. 조회
실패 시에도 전달 경로는 deterministic 한국어 fallback을 사용하며 자동 조치를 하지 않는다.

Alertmanager webhook을 direct GMS API 또는 기존 Hermes로 분석하고, 모든 분석 실패를
deterministic fallback으로 대체한 뒤 Mattermost Incoming Webhook으로 전송하는 호스트
서비스입니다. Phase 0 기본값은 `shadow`이며 기존 Hermes 결과가 권위 있는 메시지다.

## Phase 0 모드

`PINLOG_SENTINEL_MODE`는 `off|shadow|gms|hermes` 중 하나다. 미설정·잘못된 값은
fail-safe `shadow`가 된다.

- `off`: 모델을 호출하지 않고 deterministic fallback만 전송한다.
- `shadow`: Hermes가 권위 경로이며 GMS는 전달 완료 뒤 비동기 평가만 한다. GMS 실패는
  Mattermost 전달을 막지 않는다.
- `gms`: OpenAI-compatible `/v1/chat/completions`를 재시도 없이 한 번 호출한다.
- `hermes`: GMS를 우회하는 Hermes rollback 모드다.

RESOLVED는 모드와 무관하게 GMS를 0회 호출하고 항상 deterministic하게 전송한다.
GMS timeout, 429/5xx, DNS/TLS, malformed/oversize/strict JSON 위반은 모두 fallback으로
끝나므로 Mattermost 성공 시 HTTP 200이다. HTTP 502는 Mattermost 최종 실패에만 쓴다.

## 보안 경계

- Alertmanager 요청은 32자 이상의 Bearer token으로 인증합니다.
- Receiver는 HTTPS 전용으로 동작하며 Alertmanager는 CA Secret으로 인증서를 검증합니다.
- Receiver 접근 source IP는 loopback과 k3s pod CIDR(`10.42.0.0/16`)로 제한합니다.
- 원본 payload와 Mattermost URL은 로그·SQLite에 저장하지 않습니다.
- payload는 프로세스 argv에 넣지 않고 Hermes worker subprocess stdin으로 전달합니다.
- Hermes worker는 tool schema를 하나도 받지 않습니다(`toolsets=[]`).
- Mattermost URL, TLS private key, Cowork `GMS_KEY`는 systemd `LoadCredential=`로 전달합니다.
- Receiver 런타임은 Kubernetes kubeconfig/API 권한 없이 `pinlog-sentinel` 전용 사용자로 실행합니다.
- 실패 저장소에는 payload hash, dedupe key, stage, 오류 유형만 저장하며 최대 1,000건으로 제한합니다.

## 설치

```bash
cd /root/infra/ops/sentinel-receiver
./install.sh
```

설치 스크립트는 다음을 수행합니다.

- `/etc/pinlog-sentinel/receiver.env`가 없으면 Receiver Bearer token을 생성합니다.
- `monitoring/mattermost-alert-webhook` Secret을 설치 시점에 한 번 읽어 `/etc/pinlog-sentinel/mattermost_url` credential 파일로 저장합니다.
- `pinlog-dev/cowork-api-credentials`의 `GMS_KEY`를 stdout 없이 root:root 0600
  `/etc/pinlog-sentinel/gms_key`로 원자적으로 설치합니다.
- 자체 서명 TLS 인증서/키가 없으면 생성합니다.
- `pinlog-alerts` Hermes 프로필을 `/var/lib/pinlog-sentinel/hermes`로 복사하고 전용 사용자 소유로 맞춥니다.
- systemd unit을 재시작합니다.

`receiver.env`의 token 원문과 Mattermost URL은 Git, 터미널 출력, Jira, 로그에 남기지 않습니다. Alertmanager용 Secret/SealedSecret에는 token만 전달하고 TLS 검증용 공개 인증서(`ca.crt`)는 별도 Secret으로 전달합니다.

## 확인

```bash
systemctl status pinlog-sentinel-receiver
curl --fail --cacert /etc/pinlog-sentinel/tls.crt \
  --resolve pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765:127.0.0.1 \
  https://pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765/healthz
curl --fail --cacert /etc/pinlog-sentinel/tls.crt \
  --resolve pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765:127.0.0.1 \
  https://pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765/metrics
journalctl -u pinlog-sentinel-receiver --since '30 minutes ago'
```

## 전달 정책

- Critical FIRING: trusted code가 정확히 한 번 `@channel` 삽입, Alertmanager repeat 1시간
- Warning FIRING: 멘션 없음, repeat 6시간
- RESOLVED: 항상 전송, 멘션 없음
- 모델 출력의 Mattermost mention과 URL은 deterministic하게 제거합니다.
- deterministic incident key 기준 critical cooldown은 60분, warning은 6시간이고
  RESOLVED는 항상 전송합니다.
- 동시 분석 1개, 대기열 8개로 제한합니다.
- GMS 예산은 전체 `6/hour`, `30/day`; warning 부분집합은 `2/hour`, `10/day`입니다.
- 입력 allowlist·redaction·injection data boundary는 24 KiB, GMS 출력은 700 tokens와
  strict closed JSON 8 KiB로 제한합니다.
- sanitized analysis cache는 24시간, 전달·예산·실패 metadata는 7일 보존합니다. raw
  request/response는 저장하지 않습니다.
- 모든 분석 오류는 deterministic fallback을 사용하며 Mattermost 실패만 HTTP 502입니다.

## 롤백

```bash
systemctl disable --now pinlog-sentinel-receiver
```

Alertmanager Helm values에서 receiver route를 제거하거나 Alertmanager를 다시 비활성화한 뒤 동기화합니다.

## 관련 문서

- [`../../docs/alerting.md`](../../docs/alerting.md) — route, severity 정책, 외부 모니터, 장애 점검
- [`../../docs/monitoring.md`](../../docs/monitoring.md) — 메트릭, 로그, 용량, 애플리케이션 관측
- [`../../docs/runbook.md`](../../docs/runbook.md) — 일반 인프라 장애 대응
