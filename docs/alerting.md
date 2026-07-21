# 운영 알림

PinLog의 모니터링·알림 수집, 라우팅, 메시지 정책, 장애 점검 절차를 정의한다.
메트릭과 로그 사용법은 [모니터링](monitoring.md), Receiver 설치 세부사항은
[PinLog Sentinel Receiver](../ops/sentinel-receiver/README.md)를 함께 본다.

**검증 기준일**: 2026-07-21

---

## 1. 경로 요약

PinLog에는 실패 도메인이 다른 두 알림 경로가 있다.

### 클러스터 내부 상태

```text
Prometheus → Alertmanager → PinLog Sentinel Receiver → Mattermost
```

- Prometheus가 rule을 평가한다.
- Alertmanager가 severity별 grouping·repeat·resolved 전송을 결정한다.
- 호스트의 Sentinel Receiver가 전용 Hermes `pinlog-alerts` 프로필로 내용을 가공한다.
- trusted code가 mention·URL·최종 형식을 다시 검증한 뒤 Mattermost에 보낸다.

### 노드 외부 가용성

```text
GitHub-hosted runner → HTTPS/TLS probe → Mattermost 직접 전송
```

GitHub-hosted runner가 5분마다 공개 Grafana 로그인 경로를 검사한다. 이 경로는
**단일 노드 전체 장애**에도 동작해야 한다. Sentinel Receiver도 같은 노드에 있기
때문에 노드가 꺼졌을 때는 경유할 수 없다. 따라서 external monitor의
**Mattermost 직접 전송**은 가용성 탐지를 위한 **의도적 예외**다.

이 예외는 AI가 임의로 메시지를 만드는 우회가 아니다. probe script가 고정 형식,
전이 상태, mention 정책을 deterministic하게 적용한다.

---

## 2. 현재 구현 상태

| 영역 | 상태 | 근거 위치 |
|---|---|---|
| Prometheus rules | 구현 | kube-prometheus-stack 기본 rules |
| Alertmanager | 구현 | `platform/monitoring/kube-prometheus-stack-values.yaml` |
| Sentinel Receiver | 구현 | `ops/sentinel-receiver/` + systemd |
| Receiver metrics 수집 | 구현 | `pinlog-sentinel-receiver` ServiceMonitor |
| Mattermost 운영 채널 전송 | 구현 | credential 기반 webhook |
| 외부 HTTPS/TLS 감시 | 구현 | `.github/workflows/external-https-monitor.yaml` |
| Git·PR·배포 이벤트의 Sentinel 유입 | 미구현 | Jira `S15P11A705-13` 범위 |

Git·PR·배포 이벤트가 아직 Sentinel에 연결된 것처럼 보고하지 않는다. GitHub
Actions에서 ad-hoc webhook을 추가하지 말고 `S15P11A705-13`에서 source 인증,
dedupe, 포맷, 실패 재시도까지 설계한다.

---

## 3. Alertmanager 라우팅 정책

| 이벤트 | Receiver | Repeat | Mention |
|---|---|---:|---|
| `Watchdog` | null | 없음 | 없음 |
| **Critical FIRING** | Sentinel | **1시간** | trusted code가 `@channel` 정확히 1회 삽입 |
| **Warning FIRING** | Sentinel | **6시간** | 없음 |
| **RESOLVED** | Sentinel | 항상 전송 | 없음 |
| 그 외 severity | root null | 없음 | 없음 |

공통 설정:

- `group_by`: `alertname`, `namespace`, `component`
- `group_wait`: 30초
- `group_interval`: 5분
- `send_resolved: true`
- Receiver request timeout: 4분

Alertmanager와 Receiver 모두 중복을 줄이지만 역할이 다르다. Alertmanager는 alert
route와 repeat를 관리하고, Receiver는 동일 group/status/fingerprint/content를 5분
동안 억제한다.

---

## 4. Sentinel Receiver

Receiver는 Kubernetes pod가 아니라 호스트 systemd 서비스다. 단일 노드 구조에서
Hermes 프로필과 credential을 Kubernetes workload에 중복 배치하지 않기 위한 선택이다.

### 네트워크 경로

```text
Alertmanager pod
  → ExternalName Service
  → host TLS :9765
  → /alerts
```

- DNS 이름: `pinlog-sentinel-receiver.monitoring.svc.cluster.local`
- TLS 최소 버전: 1.2
- 인증: 32자 이상 Bearer token
- 허용 source: loopback, k3s pod CIDR `10.42.0.0/16`
- `/healthz`, `/metrics`도 TLS로 제공

### 보안 경계

- Mattermost URL과 TLS private key는 systemd `LoadCredential=`로 전달
- Receiver token 원문과 webhook URL은 Git·로그·Jira·문서에 기록 금지
- 전용 OS 사용자 `pinlog-sentinel`, kubeconfig/API 권한 없음
- Hermes worker payload는 stdin 전달, argv에 넣지 않음
- worker toolset은 비어 있어 진단 명령이나 외부 action 실행 불가
- 모델 출력의 mention과 URL을 deterministic하게 제거
- critical mention은 trusted code만 추가
- dead-letter에는 payload 원문 대신 hash·dedupe key·stage·오류 유형만 저장

### 실패 처리

Sentinel 또는 Mattermost 전송이 실패하면 Receiver는 HTTP 502를 반환한다.
Alertmanager는 성공으로 오인하지 않고 재시도한다. 실패 기록은 최대 1,000건으로
제한한다.

---

## 5. 메시지 계약

Sentinel 메시지는 다음 순서를 유지한다.

```text
🤖 [자동 알림 · SENTINEL]
[SEVERITY][ENVIRONMENT][SOURCE] 제목
대상 / 영향 / 관측값 / 현재 상태 / 필요 행동
---
한 줄 요약: 발생 내용, 현재 상태, 필요한 행동
```

- `한 줄 요약`은 실제 마지막 내용 줄이다.
- warning과 resolved에는 `@channel`, `@all`, `@here`, 사용자 mention을 허용하지 않는다.
- critical firing만 승인된 `@channel` 하나를 trusted code가 삽입한다.
- secret, 원본 webhook URL, 전체 환경변수, 민감 로그를 포함하지 않는다.
- 상세 조사는 Grafana·GitHub Actions·서버 로그 링크 또는 운영자를 통해 진행한다.

---

## 6. 외부 HTTPS/TLS 모니터

`.github/workflows/external-https-monitor.yaml`이 GitHub-hosted runner에서 5분마다
다음을 확인한다.

- `https://i15a705.p.ssafy.io/grafana/login` HTTPS 응답
- 기대 HTTP status `200`
- 정상 CA chain과 hostname 검증
- TLS 인증서 만료 잔여일

임계값:

- warning: 만료 14일 미만
- critical: 만료 7일 미만 또는 HTTPS/TLS 실패

상태는 protected main이 아닌 `monitor-state` 브랜치에 저장한다. 상태가
`up ↔ warning/down`으로 변할 때만 알리고, recovery는 항상 보낸다. 이 방식은 5분
주기마다 같은 메시지를 보내는 것을 막는다.

외부 모니터의 Mattermost secret은 GitHub secret으로만 관리한다. 출력·state
파일·PR·Jira에 값을 남기지 않는다.

외부 모니터 메시지는 critical에도 Mattermost mention을 넣지 않는다. Sentinel의
Critical FIRING `@channel` 정책은 Alertmanager → Sentinel 내부 경로에만 적용된다.

---

## 7. 운영 점검

### 클러스터 구성

```bash
kubectl -n monitoring get \
  statefulset/alertmanager-kube-prometheus-stack-alertmanager \
  statefulset/prometheus-kube-prometheus-stack-prometheus
kubectl -n monitoring get servicemonitor pinlog-sentinel-receiver
kubectl -n monitoring get prometheusrules
```

### Receiver

```bash
systemctl is-active pinlog-sentinel-receiver.service
systemctl is-enabled pinlog-sentinel-receiver.service

curl --fail --cacert /etc/pinlog-sentinel/tls.crt \
  --resolve pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765:127.0.0.1 \
  https://pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765/healthz

journalctl -u pinlog-sentinel-receiver.service --since "30 minutes ago"
```

credential 파일을 `cat`하거나 환경변수 전체를 출력하지 않는다.

### GitHub 외부 모니터

```bash
gh workflow view external-https-monitor
gh run list --workflow external-https-monitor --limit 10
```

수동 점검은 먼저 `dry_run=true`로 수행한다. 실제 Mattermost 전송은 팀이 알아볼 수
있게 테스트임을 표시하고, 상태 branch 변경 여부까지 확인한다.

---

## 8. 장애 판단

### Alertmanager는 정상인데 메시지가 없다

1. alert label에 `severity="critical"` 또는 `severity="warning"`이 있는지 확인한다.
2. `Watchdog` 또는 root null route에 매칭되지 않았는지 확인한다.
3. Alertmanager 로그에서 webhook status를 확인한다.
4. Receiver `/healthz`와 systemd 상태를 확인한다.
5. Receiver 로그에서 payload 원문이 아닌 stage·오류 유형을 확인한다.

### Receiver가 502를 반환한다

1. Hermes `pinlog-alerts` profile이 설치되어 있는지 확인한다.
2. model 호출 timeout과 Mattermost 응답 오류를 구분한다.
3. dead-letter 증가와 재시도 여부를 확인한다.
4. credential 값을 출력하지 말고 파일 존재·권한·digest 일치만 검사한다.

### 노드가 완전히 꺼졌다

Prometheus·Alertmanager·Sentinel도 함께 중단되므로 내부 경로에서는 알릴 수 없다.
GitHub external monitor run과 Mattermost external-monitor 메시지를 확인한다. 복구 후
`RESOLVED`가 왔는지 확인한다.

---

## 9. E2E 검증 원칙

알림 경로 변경은 다음을 모두 확인해야 완료다.

1. synthetic Critical FIRING이 Mattermost에 도착
2. `@channel`이 정확히 한 번만 존재
3. synthetic Warning FIRING에 mention 없음
4. 조건 제거 후 RESOLVED가 도착하고 mention 없음
5. 마지막 줄이 `한 줄 요약`
6. Receiver metrics와 logs에 성공/실패 상태가 반영
7. synthetic rule과 test artifact 제거

실제 채널 전송은 사용자에게 보이는 부작용이므로 실행 전에 범위를 알리고 승인된
테스트 시간에 수행한다.

---

## 10. 롤백

Receiver만 중지할 때:

```bash
systemctl disable --now pinlog-sentinel-receiver.service
```

이 상태에서는 Alertmanager가 계속 재시도하므로 장시간 방치하지 않는다. 완전한
롤백은 Helm values에서 Sentinel route를 제거하거나 Alertmanager를 비활성화한 뒤
Argo CD sync까지 확인한다.

외부 monitor는 단일 노드 장애 탐지 경로이므로 내부 Sentinel로 단순 전환하지 않는다.
workflow를 중지하면 노드 전체 장애 알림이 사라진다는 영향을 먼저 공유한다.

---

## 관련 문서

- [모니터링](monitoring.md)
- [운영 런북](runbook.md)
- [Git/CI 거버넌스](git-governance.md)
- [Sentinel Receiver 상세](../ops/sentinel-receiver/README.md)
- [아키텍처](architecture.md)
