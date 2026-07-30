# 모니터링

Prometheus(메트릭) + Alertmanager(라우팅) + Loki(로그) + Grafana(시각화) +
Sentinel Receiver(운영 알림) 스택.

**구축 시점**: 2026-07-20, **최근 검증**: 2026-07-27

---

## 접속

**https://monitoring.pin-log.com**

저용량 상시 프로필의 Phase A에서는 Grafana replicas를 0으로 유지하므로 이 URL이
열리지 않는 것이 정상이다. Phase C가 GitOps로 적용된 뒤에만 접속을 기대한다.

계정은 `admin`, 비밀번호는 SealedSecret으로 관리한다. 조회:

```bash
kubectl -n monitoring get secret grafana-admin \
  -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

> 다른 관리 도구(ArgoCD)와 달리 Grafana는 공개 경로로 열어뒀다.
> **실제로 안 보면 모니터링은 무의미하기 때문**이고, Grafana admin은
> 클러스터를 변경할 권한이 없어 ArgoCD 대비 피해 범위가 작다.
> 대신 자가 가입(`allow_sign_up`)과 익명 접근은 꺼두었다.

---

## PinLog Operations Overview 읽는 법

화면은 위에서 아래로 `① 지금 서비스가 정상인가요?` → `② 사용자가 느끼는 요청 상태` →
`③ 서버 자원이 충분한가요?` → `④ 지금 확인할 알림과 로그` 순서로 읽는다.
상태 panel의 green은 정상, yellow는 주의, red는 즉시 확인이 필요하다는 뜻이다.
트래픽·응답 시간·DB 대기·로그량처럼 중립색인 panel은 건강 판정이 아니라 평소 대비
추세를 보여주므로, 단일 값보다 급증·급감과 다른 이상 신호를 함께 본다.

| Panel | 의미 | 정상 | 첫 확인 대상 |
|---|---|---|---|
| 백엔드 연결 상태 | 운영 백엔드 메트릭 수집 여부 | 1 | Backend 상태, ServiceMonitor, metrics endpoint |
| 확인 필요한 알림 | warning·critical firing 알림 수 | 0 | 아래 알림 상세의 대상과 원인 |
| 실행 가능한 서비스 인스턴스 | 요청을 받을 수 있는 deployment replica | desired replica와 일치 | Pod readiness, rollout |
| 컨테이너 재시작 | pod/container별 누적 restart | 갑작스러운 증가 없음 | Pod event, 이전 container 로그 |
| 초당 백엔드 요청 | 운영 백엔드 요청량 | 트래픽에 따라 다름 | 배포 시점, 오류율 |
| 요청 및 서버 오류 추이 | 전체 요청과 HTTP 5xx 비교 | 5xx가 0에 가까움 | 오류 로그, 배포 시점 |
| 평균 응답 시간 | 운영 백엔드 평균 처리 시간 | 확정 SLO 없음 | DB 대기, 5xx |
| DB 연결 대기 요청 | DB connection 대기 수 | 대부분 0 | Hikari pool, PostgreSQL |
| 서버 CPU 사용률 | 서버 전체 CPU 사용률 | 70% 미만 | workload 사용량, CPU pressure |
| 서버 메모리 사용률 | 서버 메모리 사용률 | 75% 미만 | workload memory, OOM/restart |
| 서버 디스크 사용률 | 루트 filesystem 사용률 | 75% 미만 | 이미지, 로그, PVC 사용량 |
| 현재 발생한 알림 상세 | firing warning·critical label | 행 없음 | severity, namespace, pod/instance |
| 서비스별 로그 발생량 | namespace/app별 로그량 | 서비스 활동에 따라 다름 | 배포, restart, 오류 로그 |
| 최근 오류 로그 | error·exception·fail 포함 최근 로그 | 결과 없음 | namespace, pod, container |

`최근 오류 로그`(기존 기술명 `Recent Error Logs`)가 비어 있는 것은 정상일 수 있으며,
그 사실만으로 Loki 장애를 뜻하지 않는다. 같은 시간대의 `서비스별 로그 발생량`과 Loki
데이터소스 상태를 함께 확인한다.

Known limitation: kube-prometheus-stack 기본 dashboard는 현재 없는 `cluster` label을
요구할 수 있으며, 이 제한은 PinLog Operations Overview 변경과 분리해 다룬다.

---

## 구성

| 구성요소 | 차트 | 역할 |
|---|---|---|
| kube-prometheus-stack | `87.17.0` | Prometheus, Alertmanager, Grafana, kube-state-metrics, node-exporter |
| Loki | `7.1.0` | 로그 저장 (SingleBinary, 파일시스템) |
| Alloy | `1.10.1` | 로그 수집 (DaemonSet) |
| Sentinel Receiver | systemd | Alertmanager payload 가공·검증·Mattermost 전달 |
| external-https-monitor | GitHub Actions | 단일 노드 밖에서 HTTPS/TLS 가용성 확인 |

차트는 업스트림을 그대로 쓰고 값만 `platform/monitoring/`에 둔다.
ArgoCD multi-source Application으로 결합한다.

```
platform/monitoring/
├── kube-prometheus-stack-values.yaml
├── loki-values.yaml
└── alloy-values.yaml

argocd/apps/
├── monitoring-prometheus.yaml   (wave 2)
├── monitoring-loki.yaml         (wave 2)
├── monitoring-alloy.yaml        (wave 3 — Loki 이후)
└── secrets-monitoring.yaml      (wave -1 — Grafana 비밀번호)

ops/sentinel-receiver/            (호스트 systemd 서비스)
.github/workflows/external-https-monitor.yaml
```

### 실측 사용량 (2026-07-21)

| 구성요소 | 컨테이너 합계 메모리 |
|---|---|
| Prometheus + config-reloader | 638Mi |
| Grafana + sidecars | 304Mi |
| Loki + rules sidecar | 163Mi |
| Alloy + config-reloader | 68Mi |
| Alertmanager + config-reloader | 30Mi |
| kube-state-metrics | 19Mi |
| operator | 24Mi |
| node-exporter | 11Mi |
| **합계** | **1,257Mi (~1.23Gi)** |

이 값은 순간 실측이며 용량 보장이 아니다. 서비스와 rule이 늘면 Prometheus
메모리가 활성 시리즈 수에 비례해 증가한다.

### 리소스 가드레일

`pinlog-prod`는 단일 노드의 시스템·GitOps·모니터링 생존 자원을 보장하기 위해
다음 namespace 예산을 사용한다.

| 항목 | 상한 |
|---|---:|
| CPU requests | 2 cores |
| 메모리 requests | 6Gi |
| CPU limits | 4 cores |
| 메모리 limits | 8Gi |
| Pod | 30 |
| PVC | 10 |
| PVC 요청 스토리지 합계 | 50Gi |

resources가 없는 신규 prod 컨테이너에는 LimitRange가 request `100m/128Mi`,
limit `500m/768Mi`를 기본 적용한다. 컨테이너 하나의 최대값은 `2 CPU/2Gi`다.
서비스별 명시값이 우선하며, microservice chart 기본값은 `100m/384Mi` request와
`500m/768Mi` limit이다. PostgreSQL·Redis·실행 중인 backup Job의 CPU limit 합계는
`2 cores`라 steady-state 기준으로는 기본 서비스 4개가 추가로 들어간다. 다만
microservice rollout은 `maxSurge: 1`이라 500m가 하나 더 필요하다. backup과 겹쳐도
rollout 여유를 보장하려면 기본값 서비스는 3개까지로 보고, 네 번째 서비스 추가나
서비스별 limit 증설 전에 quota·노드 여유와 backup 실행 시간을 함께 재산정한다.

GitOps가 관리하는 Prometheus·Alertmanager·Grafana·Loki·Alloy 및 sidecar에도
CPU·메모리 requests/limits를 명시한다. Argo CD와 k3s core workload는 이 저장소의
GitOps 관리 범위가 아니므로 이 정책이 임의로 live patch하지 않는다. CI는 Argo와
동일한 pinned chart를 렌더링해 모든 container·initContainer와 operator-generated
resource args를 검사하고, rendered alert rules를 promtool로 검증한다.

운영 alert:

- `PinLogProdQuotaHigh`: quota 사용률 80% 초과가 10분 지속
- `PinLogProdContainerOOMKilled`: 최근 10분 내 OOMKilled
- `PinLogProdPodUnschedulable`: prod Pod 스케줄 실패가 5분 지속

세 alert는 모두 `warning`으로 Sentinel에 전달된다. 값 조정은 live `kubectl edit`이
아니라 기능 브랜치·PR·필수 CI를 거쳐 `platform/namespaces/namespaces.yaml` 또는
해당 workload values를 변경한다.

### 저용량 상시 프로필

과거 `t2.xlarge`와 Docker/cri-dockerd 조합에서 전체 monitoring을 동시에 실행하면
Docker stats 계산과 scrape burst가 stateful workload와 CPU를 경쟁했다. 이 프로필은
수집 완전성보다 PostgreSQL·Redis·Backend 생존을 우선하며 세 단계로만 재개한다.

**의도한 기능 손실:**

- kubelet `cAdvisor`, probe, resource endpoint를 수집하지 않아 컨테이너별
  CPU·메모리 시계열이 없다.
- HPA가 없으므로 `metrics-server`는 중지 상태를 유지한다. 따라서 `kubectl top`은
  제공하지 않는다. host systemd service·5분 timer가 k3s의 packaged manifest
  재생성 뒤에도 replicas 0을 복구하며 설치·검증 절차는 `metrics-server.md`를 따른다.
- 메트릭은 **최대 3일**이며 `4GB`에 먼저 도달하면 더 짧아진다. 로그도 최대
  3일이며 20Gi PVC가 먼저 가득 차지 않아야 한다.
- Phase A에서는 Grafana replicas를 0으로 유지한다. Prometheus API와
  Alertmanager/Sentinel 경로로 collection과 alerting을 먼저 검증한다.

**재개 순서:**

초기 저용량 PR은 세 monitoring Application 모두에
`argocd.argoproj.io/skip-reconcile: "true"`를 선언한다. 단, annotation과 values를
같은 PR에서 변경하므로 merge 자체만으로 안전하다고 가정하지 않는다. **pre-merge gate**로
root Application을 먼저 pause하고 세 child를 pause한 뒤 live annotation이 모두
`true`인지 확인해야 한다. 이 gate를 통과하지 않으면 merge하지 않는다.

```bash
set -euo pipefail
kubectl -n argocd annotate application root \
  argocd.argoproj.io/skip-reconcile=true --overwrite --request-timeout=5s
for app in monitoring-prometheus monitoring-loki monitoring-alloy; do
  kubectl -n argocd annotate application "$app" \
    argocd.argoproj.io/skip-reconcile=true --overwrite --request-timeout=5s
done
for app in root monitoring-prometheus monitoring-loki monitoring-alloy; do
  test "$(kubectl -n argocd get application "$app" --request-timeout=5s \
    -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/skip-reconcile}')" = true
done
```

merge 후 root를 unpause해 Git의 child pause annotation을 반영하고, 세 child가 live에서
계속 `true`인지 다시 확인한다. activation은 그 뒤 annotation을 제거하는 아래 후속
PR들로만 진행한다.

```bash
set -euo pipefail
: "${EXPECTED_REVISION:?set the merged main commit SHA}"
kubectl -n argocd annotate application root \
  argocd.argoproj.io/skip-reconcile- --request-timeout=5s
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  revision=$(kubectl -n argocd get application root --request-timeout=5s \
    -o jsonpath='{.status.sync.revision}')
  sync=$(kubectl -n argocd get application root --request-timeout=5s \
    -o jsonpath='{.status.sync.status}')
  [[ "$revision" == "$EXPECTED_REVISION" && "$sync" == Synced ]] && break
  sleep 2
done
test "$revision" = "$EXPECTED_REVISION"
test "$sync" = Synced
for app in monitoring-prometheus monitoring-loki monitoring-alloy; do
  test "$(kubectl -n argocd get application "$app" --request-timeout=5s \
    -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/skip-reconcile}')" = true
done
```

1. **Phase A:** `argocd/apps/monitoring-prometheus.yaml`에서 skip annotation만
   제거하는 PR을 merge한다. Prometheus, Alertmanager, operator,
   kube-state-metrics, node-exporter가 실행되고 Grafana는 0으로 남아야 한다.
2. native containerd 전환 뒤 Phase A가 2시간 이상 안정적이면 **Phase B-1:**
   `monitoring-loki.yaml`의 annotation만 제거하는 PR을 merge해 Loki Ready/PVC와
   API를 확인한다. **5분 capacity gate**를 통과한 뒤 **Phase B-2:**
   `monitoring-alloy.yaml`의 annotation만 제거하는 PR을 merge하고 실제 Backend
   로그 유입을 확인한다. Phase B-2도 5분 gate를 적용한다.
3. Phase A와 Phase B가 모두 안정적일 때만 별도 PR로 **Phase C:**
   `grafana.replicas: 1`을 적용한다.

개발·배포 속도를 위해 **24시간 watchdog은 비차단** 장기 관찰로 유지한다. hourly
watchdog 실패는 즉시 조사하되, 각 단계의 위 5분 gate가 통과하면 다음 phase를
진행할 수 있다.

각 단계 전에 Backend·PostgreSQL·Redis restart count, readiness, PVC와 현재 pause
상태를 기록한다. phase별 고유 경로를 정해 exact PVC inventory도 남긴다.

```bash
export PVC_INVENTORY_FILE=/var/tmp/pinlog-monitoring-pvc-before-phase-a.json
umask 077
kubectl -n monitoring get pvc --request-timeout=5s -o json \
  | jq '[.items[] | {name: .metadata.name, phase: .status.phase}]' \
  >"$PVC_INVENTORY_FILE"
jq -e 'all(.[]; .phase == "Bound")' "$PVC_INVENTORY_FILE"
```

다음 조건을 collection PASS로 본다.

- Prometheus와 Alertmanager Ready
- Backend target `up == 1`
- node-exporter와 kube-state-metrics target UP
- Sentinel alert route와 Receiver health 정상

다음 조건을 capacity PASS로 본다.

- Backend·PostgreSQL·Redis restart delta 0
- readiness loss 0
- CPU steal이 5분 동안 25%를 초과하지 않음
- CPU PSI avg60이 25 미만
- idle CPU가 5분 동안 20% 미만으로 유지되지 않음

새 stateful restart, Backend readiness loss, 반복 probe timeout, 위 CPU steal/PSI
기준 초과 또는 controller/runtime 불안정이 하나라도 발생하면 해당 단계를 중단하고
다음 bounded 절차로 **즉시 pause 복귀**한다. PVC 삭제, StatefulSet 삭제,
monitoring Application의 prune 활성화는 rollback 수단이 아니다.

```bash
set -euo pipefail

# 1) child Application을 관리하는 root부터 pause한다. root는 자신의 source path에서
# 제외되므로 이 live annotation은 root self-heal에 의해 제거되지 않는다.
kubectl -n argocd annotate application root \
  argocd.argoproj.io/skip-reconcile=true --overwrite --request-timeout=5s
test "$(kubectl -n argocd get application root --request-timeout=5s \
  -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/skip-reconcile}')" = true

# 2) root pause 확인 뒤 child를 pause한다. 이후 workload 명령보다 반드시 앞선다.
for app in monitoring-prometheus monitoring-loki monitoring-alloy; do
  kubectl -n argocd annotate application "$app" \
    argocd.argoproj.io/skip-reconcile=true --overwrite --request-timeout=5s
  test "$(kubectl -n argocd get application "$app" --request-timeout=5s \
    -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/skip-reconcile}')" = true
done

# 3) Prometheus Operator부터 정지하고 완전히 사라질 때까지 최대 120초 기다린다.
# 단계에 따라 아직 생성되지 않은 resource는 정상적으로 건너뛴다.
if kubectl -n monitoring get deployment/kube-prometheus-stack-operator \
  --request-timeout=5s >/dev/null 2>&1; then
  kubectl -n monitoring scale deployment/kube-prometheus-stack-operator \
    --replicas=0 --request-timeout=5s
  if [[ -n "$(kubectl -n monitoring get pod --request-timeout=5s \
    -l app=kube-prometheus-stack-operator -o name)" ]]; then
    kubectl -n monitoring wait --for=delete pod \
      -l app=kube-prometheus-stack-operator --timeout=120s --request-timeout=125s
  fi
fi

# 4) operator가 관리하던 StatefulSet과 나머지 Deployment를 0으로 고정한다.
scale_if_exists() {
  local resource="$1"
  if kubectl -n monitoring get "$resource" --request-timeout=5s >/dev/null 2>&1; then
    kubectl -n monitoring scale "$resource" --replicas=0 --request-timeout=5s
  fi
}
for resource in \
  statefulset/prometheus-kube-prometheus-stack-prometheus \
  statefulset/alertmanager-kube-prometheus-stack-alertmanager \
  statefulset/loki \
  deployment/kube-prometheus-stack-kube-state-metrics \
  deployment/kube-prometheus-stack-grafana; do
  scale_if_exists "$resource"
done

# 5) 생성된 DaemonSet은 존재하지 않는 node label로 scheduling을 막는다.
for ds in kube-prometheus-stack-prometheus-node-exporter alloy; do
  if kubectl -n monitoring get daemonset "$ds" \
    --request-timeout=5s >/dev/null 2>&1; then
    kubectl -n monitoring patch daemonset "$ds" --type=merge \
      --request-timeout=5s \
      -p '{"spec":{"template":{"spec":{"nodeSelector":{"pinlog.io/monitoring-paused":"true"}}}}}'
  fi
done

# 6) 최대 120초 안에 monitoring Pod가 모두 종료되는지 bounded 확인한다.
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  count=$(kubectl -n monitoring get pods --request-timeout=5s -o json \
    | jq '.items | length')
  (( count == 0 )) && break
  sleep 2
done
test "$(kubectl -n monitoring get pods --request-timeout=5s -o json \
  | jq '.items | length')" -eq 0

# 7) 재생성·데이터 손실·남은 endpoint가 없는지 확인한다.
kubectl -n monitoring get deployment,statefulset,daemonset --request-timeout=5s
kubectl -n monitoring get pvc --request-timeout=5s
: "${PVC_INVENTORY_FILE:?set the pre-phase inventory path}"
test -r "$PVC_INVENTORY_FILE"
current_pvc_inventory=$(mktemp)
trap 'rm -f "$current_pvc_inventory"' EXIT
kubectl -n monitoring get pvc --request-timeout=5s -o json \
  >"$current_pvc_inventory"
python3 /root/infra/tools/verify_monitoring_pvc_inventory.py \
  --before "$PVC_INVENTORY_FILE" --current "$current_pvc_inventory"
kubectl -n monitoring get endpointslices --request-timeout=5s -o json \
  | jq -e '[.items[].endpoints[]? | select(.conditions.ready != false)
            | .addresses[]?] | length == 0'
```

rollback 후에는 방금 live로 넣은 annotation에만 의존하지 않는다. 활성화 PR의 역변경,
즉 세 child Application에 `skip-reconcile: "true"`를 되돌리는 후속 PR을 즉시 만들고
필수 CI·merge를 완료한다. root가 pause된 동안 child의 live annotation은 유지된다.
merge가 확인된 뒤에만 root annotation을 제거하고 root sync 후 세 child annotation이
Git과 live에서 모두 `true`인지 검증한다. 그 전에는 root를 unpause하지 않는다.

```bash
set -euo pipefail
: "${EXPECTED_ROLLBACK_REVISION:?set the merged rollback commit SHA}"
kubectl -n argocd annotate application root \
  argocd.argoproj.io/skip-reconcile- --request-timeout=5s
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  revision=$(kubectl -n argocd get application root --request-timeout=5s \
    -o jsonpath='{.status.sync.revision}')
  sync=$(kubectl -n argocd get application root --request-timeout=5s \
    -o jsonpath='{.status.sync.status}')
  [[ "$revision" == "$EXPECTED_ROLLBACK_REVISION" && "$sync" == Synced ]] && break
  sleep 2
done
test "$revision" = "$EXPECTED_ROLLBACK_REVISION"
test "$sync" = Synced
for app in monitoring-prometheus monitoring-loki monitoring-alloy; do
  test "$(kubectl -n argocd get application "$app" --request-timeout=5s \
    -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/skip-reconcile}')" = true
done
```

### 스토리지

| PVC | 크기 | 보관 |
|---|---|---|
| Prometheus | 12Gi | 최대 3일, 4GB 상한 (`retention: 3d`, `retentionSize: 4GB`) |
| Alertmanager | 1Gi | 120시간 (`retention: 120h`) |
| Loki | 20Gi | 최대 3일 (`retention_period: 72h`) |
| Grafana | 2Gi | 대시보드·설정 |

---

## 왜 이렇게 구성했는가

### Promtail이 아니라 Alloy

`grafana/promtail` 차트는 **공식적으로 deprecated** 상태다
(`helm show chart grafana/promtail` → `deprecated: true`).
Grafana가 후속으로 Alloy를 지정했다.

로그는 호스트 `/var/log` 마운트가 아니라 **Kubernetes API로 읽는다**
(`loki.source.kubernetes`). 권한 요구가 적고 k3s에서 안정적이다.

### Loki 차트 기본값을 대부분 무력화했다

**이 서버에서 가장 위험했던 부분이다.** 차트 기본값은 프로덕션 클러스터를 가정한다:

| 기본값 | 결과 |
|---|---|
| `deploymentMode: SimpleScalable` | 파드 9개 (read 3 / write 3 / backend 3) |
| `loki.storage.type: s3` | S3 버킷 필요 (우리에겐 없음) |
| `chunksCache.allocatedMemory: 8192` | **8Gi 요구** |
| `resultsCache.allocatedMemory: 1024` | 1Gi 요구 |

15Gi 단일 노드에 그대로 설치하면 **캐시만으로 9Gi를 요구해 클러스터가 죽는다.**
`SingleBinary` + `filesystem` + 캐시 전면 비활성으로 제한한다. 현재 rendered
guardrail은 두 컨테이너 합계 request `208Mi`, limit `800Mi`이며 실제 사용량은
로그량과 쿼리에 따라 달라진다.

> ⚠️ 차트를 업그레이드할 때 이 재정의들이 여전히 유효한지 반드시 확인할 것.
> 키 이름이 바뀌면 조용히 기본값으로 돌아가고, 그 결과는 클러스터 정지다.

### Alertmanager + Sentinel 활성

Alertmanager는 severity별 grouping·repeat·resolved 정책을 적용하고, TLS/Bearer
인증으로 호스트의 Sentinel Receiver에 전달한다.

```text
Prometheus → Alertmanager → PinLog Sentinel Receiver → Mattermost
```

- critical 반복: 1시간
- warning 반복: 6시간
- resolved: 항상 전송
- Watchdog: null receiver
- Receiver metrics: static HTTPS ScrapeConfig로 60초마다 수집

단일 노드 전체 장애는 이 경로 자체가 중단되므로 GitHub-hosted external monitor가
별도로 공개 HTTPS/TLS를 확인한다. 상세 정책과 직접 전송 예외는
[`alerting.md`](alerting.md)를 기준으로 한다.

### k3s 미지원 컴포넌트 모니터링 비활성

k3s는 control-plane을 단일 바이너리 안에서 돌리므로
`kubeControllerManager`, `kubeScheduler`, `kubeProxy`, `kubeEtcd`는
붙을 대상이 없다. 켜두면 **Targets 화면이 영구히 빨간 상태**가 되어
진짜 장애를 가린다.

Phase A에서는 활성화한 target만 UP이어야 한다. 비활성 control-plane target,
kubelet cAdvisor/probe/resource endpoint, Grafana는 성공 target 수에 포함하지 않는다.

### `serviceMonitorSelectorNilUsesHelmValues: false`

기본값(`true`)이면 **이 Helm 릴리스 라벨이 붙은 ServiceMonitor만** 수집한다.
우리 마이크로서비스 차트가 만드는 ServiceMonitor에는 그 라벨이 없으므로,
false로 두지 않으면 **애플리케이션 메트릭이 전혀 수집되지 않는다.**

---

## 서비스 메트릭 수집하기

마이크로서비스 차트에 ServiceMonitor 지원이 들어 있다. **기본은 꺼져 있다** —
앱이 메트릭을 노출하지 않는데 켜면 Targets가 빨갛게 남기 때문이다.

### 1. 앱에 메트릭 노출 추가 (Spring Boot)

```groovy
// build.gradle
implementation 'io.micrometer:micrometer-registry-prometheus'
```

```yaml
# application.yml
management:
  endpoints:
    web:
      exposure:
        include: health,prometheus
```

### 2. infra에서 켜기

```yaml
# apps/prod/<서비스>/values.yaml
metrics:
  enabled: true
  # context-path를 포함해야 한다 (probes.path와 같은 이유)
  path: /api/<서비스>/actuator/prometheus
```

커밋하면 ArgoCD가 ServiceMonitor를 만들고 Prometheus가 자동으로 수집한다.

### 3. 확인

Grafana → Explore → Prometheus 데이터소스에서:
```promql
up{job="<서비스>"}
```

---

## 로그 보기

Grafana → Explore → **Loki** 데이터소스.

```logql
# 특정 네임스페이스 전체
{namespace="pinlog-prod"}

# 특정 서비스
{namespace="pinlog-prod", app="auth-service"}

# 에러만
{namespace="pinlog-prod"} |= "ERROR"

# 특정 파드
{namespace="pinlog-prod", pod="auth-service-abc123-xyz"}
```

### 수집되는 라벨

`namespace`, `pod`, `container`, `app`

라벨을 더 늘리지 않는 이유: Loki는 **라벨 조합 하나가 스트림 하나**가 되고,
카디널리티가 높으면 쿼리가 급격히 느려진다. 로그 내용 검색은 `|=` 필터로 한다.

### 제외되는 로그

헬스체크 접근 로그(`actuator/health`, `/healthz`, `kube-probe`)는
Alloy 단계에서 버린다. 전체 로그의 대부분을 차지해 정작 필요한 로그를
찾기 어렵게 만들고 보관 기간도 줄이기 때문이다.

설정 위치: `platform/monitoring/alloy-values.yaml`의 `loki.process "drop_noise"`

---

## 구축 중 겪은 함정

| 문제 | 원인·해결 |
|---|---|
| Loki 기본값이 8Gi 캐시 요구 | `chunksCache`/`resultsCache` 명시적 비활성 필수 |
| Prometheus CRD 적용 실패 | CRD가 커서 client-side apply 어노테이션 한도(262144 bytes) 초과 → `ServerSideApply=true` |
| Alertmanager → Receiver 연결 실패 | pod에서 호스트 loopback을 쓸 수 없다. TLS server name을 유지하는 ExternalName Service와 CA Secret을 함께 사용한다 |
| 앱 메트릭 미수집 | `serviceMonitorSelectorNilUsesHelmValues: false` 누락 시 발생 |

---

## 트러블슈팅

### Grafana에 접속이 안 됨

```bash
kubectl -n monitoring get pods -l app.kubernetes.io/name=grafana
kubectl -n monitoring logs deploy/kube-prometheus-stack-grafana -c grafana
curl -I https://monitoring.pin-log.com/api/health
```

서브경로 문제라면 `grafana.ini`의 `root_url`과 `serve_from_sub_path`를 확인한다.

### Prometheus 타겟이 DOWN

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
# → http://localhost:9090/targets
```

애플리케이션 타겟이면 `metrics.path`가 `context-path`를 포함하는지 확인.

### 로그가 안 보임

```bash
# Alloy가 돌고 있는지
kubectl -n monitoring get ds alloy
kubectl -n monitoring logs ds/alloy -c alloy --tail=50

# Loki가 받고 있는지
kubectl -n monitoring port-forward svc/loki 3100:3100
curl -s 'http://localhost:3100/loki/api/v1/label/namespace/values'
```

헬스체크 로그만 나오는 서비스라면 `drop_noise` 필터에 걸린 것이다.

### Prometheus 메모리 부족

메모리는 **활성 시리즈 수에 비례**한다. 서비스가 늘면 올려야 한다.

```yaml
# platform/monitoring/kube-prometheus-stack-values.yaml
prometheus:
  prometheusSpec:
    resources:
      limits:
        memory: 2Gi   # 저용량 프로필 기본 1536Mi보다 올릴 때
```

전체 예산(`architecture.md` 4장)을 확인하고 올릴 것. 이 서버는 swap이 없어
메모리 압박이 즉시 파드 종료로 이어진다.

### Alertmanager 알림이 Mattermost에 오지 않음

```bash
kubectl -n monitoring get pod \
  alertmanager-kube-prometheus-stack-alertmanager-0
kubectl -n monitoring logs \
  alertmanager-kube-prometheus-stack-alertmanager-0 -c alertmanager --tail=100
systemctl is-active pinlog-sentinel-receiver.service
```

alert에 `severity="critical"` 또는 `severity="warning"` label이 있는지 먼저 확인한다.
Receiver TLS health, route, 재시도 순서는 [`alerting.md`](alerting.md)의 장애 판단을
따른다. token, webhook URL, credential 파일 내용은 출력하지 않는다.

---

## 관련 문서

- [`architecture.md`](architecture.md) — 전체 구조와 설계 결정
- [`alerting.md`](alerting.md) — Alertmanager, Sentinel, 외부 HTTPS 알림
- [`runbook.md`](runbook.md) — 일반 장애 대응
- [`../examples/README.md`](../examples/README.md) — 새 서비스 추가
