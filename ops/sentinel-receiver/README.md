# PinLog Sentinel Receiver

## AI evidence boundary

FIRING 처리 순서는 `allowlisted diagnostics → deterministic evidence parser → AI → Mattermost`이다. AI에는 원시 Alertmanager payload/annotations/label set/로그/시계열을 전달하지 않고 최대 12KiB의 `sentinel-evidence-v1` closed JSON만 전달한다. metric은 current/baseline/delta/ratio/anomaly(2KiB), 로그는 2단계 redaction과 NFKC 정규화 후 `sig-v1` signature dedupe 및 deterministic Top-5(개별 768B, 합계 6KiB)로 제한한다. 유효 evidence 없음 또는 parser/schema 실패는 AI 0회와 deterministic fallback으로 종료한다. RESOLVED는 diagnostics/AI 모두 0회이다. parser 입력과 원시는 저장하지 않으며 기존 cache에는 bounded model analysis만 저장한다.

`diagnostics.py`는 FIRING 알림에만 allowlist Prometheus/Loki 진단을 추가한다. query
adapter에는 임의 query가 아니라 검토된 template identifier, 최대 20분 window,
100개 결과, 3초 timeout만 전달한다. RESOLVED는 adapter와 AI를 모두 건너뛴다. 조회
실패 시에도 전달 경로는 deterministic 한국어 fallback을 사용하며 자동 조치를 하지 않는다.

Prometheus matrix에서는 유한한 값만 사용한다. 마지막 유효값을 현재값으로 삼고, 그
현재값을 제외한 앞쪽 유효값이 3개 이상일 때만 median을 평소값으로 표시한다. 빈 값,
NaN/Inf, 단일·짧은 시계열에는 평소값을 만들지 않는다. 근거와 신뢰도는 실제로 얻은
Prometheus 값과 redaction된 Loki 표본만 반영하며 Frontend/AI 빈 지표는
`비교 가능한 지표 없음`으로 명시한다.

Alertmanager webhook을 direct AI API 또는 기존 Hermes로 분석하고, 모든 분석 실패를
deterministic fallback으로 대체한 뒤 Mattermost Incoming Webhook으로 전송하는 호스트
서비스입니다. Phase 0 기본값은 `shadow`이며 기존 Hermes 결과가 권위 있는 메시지다.

## Phase 0 모드

`PINLOG_SENTINEL_MODE`의 기존 런타임 enum 값은 구현 계약대로 유지한다. 공개 문서에서는
provider 이름 대신 모델 미호출, shadow, direct AI API, Hermes rollback 동작으로
구분한다. 미설정·잘못된 값은 fail-safe shadow가 된다.

- 모델 미호출 모드: deterministic fallback만 전송한다.
- shadow 모드: Hermes가 권위 경로이며 AI API는 전달 완료 뒤 비동기 평가만 한다.
  AI API 실패는 Mattermost 전달을 막지 않는다.
- direct AI API 모드: OpenAI-compatible `/v1/chat/completions`를 재시도 없이 한 번 호출한다.
- Hermes rollback 모드: AI API를 우회한다.

RESOLVED는 모드와 무관하게 AI API를 0회 호출하고 항상 deterministic하게 전송한다.
AI API timeout, 429/5xx, DNS/TLS, malformed/oversize/strict JSON 위반은 모두 fallback으로
끝나므로 Mattermost 성공 시 HTTP 200이다. HTTP 502는 Mattermost 최종 실패에만 쓴다.

## 보안 경계

- Alertmanager 요청은 32자 이상의 Bearer token으로 인증합니다.
- Receiver는 HTTPS 전용으로 동작하며 Alertmanager는 CA Secret으로 인증서를 검증합니다.
- Receiver 접근 source IP는 loopback과 k3s pod CIDR(`10.42.0.0/16`)로 제한합니다.
- 원본 payload와 Mattermost URL은 로그·SQLite에 저장하지 않습니다.
- payload는 프로세스 argv에 넣지 않고 Hermes worker subprocess stdin으로 전달합니다.
- Hermes worker는 tool schema를 하나도 받지 않습니다(`toolsets=[]`).
- Mattermost URL, TLS private key, Cowork AI API credential은 systemd `LoadCredential=`로 전달합니다.
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
- `pinlog-dev/cowork-api-credentials`의 AI API credential을 stdout 없이 root:root 0600
  credential 파일로 원자적으로 설치합니다.
- 호스트에서 `*.monitoring.svc.cluster.local`을 해석할 수 없으므로 설치 시 Prometheus/Loki
  Service의 현재 ClusterIP를 검증해 root:root 0600 `diagnostics.json`으로 원자적으로
  기록하고 systemd credential로만 전달합니다. 런타임에는 kubeconfig/API 권한이 없습니다.
  Service 재생성으로 ClusterIP가 바뀌면 진단 조회가 실패하고 fallback 알림은 계속
  전송됩니다. 이 경우 설치 스크립트를 다시 실행해 endpoint snapshot을 갱신해야 합니다.
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
- AI API 예산은 전체 `6/hour`, `30/day`; warning 부분집합은 `2/hour`, `10/day`입니다.
- 입력 allowlist·redaction·injection data boundary는 24 KiB, AI API 출력은 700 tokens와
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
