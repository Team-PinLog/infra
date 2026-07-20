# 모니터링

Prometheus(메트릭) + Loki(로그) + Grafana(시각화) 스택.

**구축 시점**: 2026-07-20

---

## 접속

**https://i15a705.p.ssafy.io/grafana**

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

## 구성

| 구성요소 | 차트 | 역할 |
|---|---|---|
| kube-prometheus-stack | `87.17.0` | Prometheus, Grafana, kube-state-metrics, node-exporter |
| Loki | `7.1.0` | 로그 저장 (SingleBinary, 파일시스템) |
| Alloy | `1.10.1` | 로그 수집 (DaemonSet) |

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
```

### 실측 사용량 (구축 직후, 서비스 0개 상태)

| 파드 | 메모리 |
|---|---|
| Prometheus | 403Mi |
| Grafana | 300Mi |
| Loki | 147Mi |
| Alloy | 63Mi |
| kube-state-metrics | 18Mi |
| operator | 21Mi |
| node-exporter | 9Mi |
| **합계** | **~960Mi** |

노드 전체: CPU 15%, 메모리 5.1Gi / 10Gi 여유.
서비스가 붙으면 Prometheus 메모리가 활성 시리즈 수에 비례해 늘어난다.

### 스토리지

| PVC | 크기 | 보관 |
|---|---|---|
| Prometheus | 12Gi | 7일 (`retention: 7d`, `retentionSize: 8GB`) |
| Loki | 20Gi | 7일 (`retention_period: 168h`) |
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
`SingleBinary` + `filesystem` + 캐시 전면 비활성으로 147Mi에 맞췄다.

> ⚠️ 차트를 업그레이드할 때 이 재정의들이 여전히 유효한지 반드시 확인할 것.
> 키 이름이 바뀌면 조용히 기본값으로 돌아가고, 그 결과는 클러스터 정지다.

### Alertmanager 비활성

5주 프로젝트에서 알림 라우팅(Slack 연동, 억제 규칙, 그룹핑)을 실제로 설정할
일이 없는데 메모리와 설정 부담만 는다. **알림 룰 자체는 Prometheus가
평가하므로** 발화 여부는 Grafana에서 확인할 수 있다.

필요해지면 `alertmanager.enabled: true`로 켜면 된다.

### k3s 미지원 컴포넌트 모니터링 비활성

k3s는 control-plane을 단일 바이너리 안에서 돌리므로
`kubeControllerManager`, `kubeScheduler`, `kubeProxy`, `kubeEtcd`는
붙을 대상이 없다. 켜두면 **Targets 화면이 영구히 빨간 상태**가 되어
진짜 장애를 가린다.

결과: 타겟 11개 전부 UP, 빨간 타겟 0개.

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
| Alertmanager 데이터소스 잔존 | `alertmanager.enabled: false`만으로는 부족. `sidecar.datasources.alertmanager.enabled: false` **와** `deleteDatasources`가 함께 필요. 프로비저닝된 데이터소스는 read-only라 API 삭제도 거부된다 |
| 앱 메트릭 미수집 | `serviceMonitorSelectorNilUsesHelmValues: false` 누락 시 발생 |

---

## 트러블슈팅

### Grafana에 접속이 안 됨

```bash
kubectl -n monitoring get pods -l app.kubernetes.io/name=grafana
kubectl -n monitoring logs deploy/kube-prometheus-stack-grafana -c grafana
curl -I https://i15a705.p.ssafy.io/grafana/api/health
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
        memory: 3Gi   # 기본 2Gi
```

전체 예산(`architecture.md` 4장)을 확인하고 올릴 것. 이 서버는 swap이 없어
메모리 압박이 즉시 파드 종료로 이어진다.

---

## 관련 문서

- [`architecture.md`](architecture.md) — 전체 구조와 설계 결정
- [`runbook.md`](runbook.md) — 일반 장애 대응
- [`../examples/README.md`](../examples/README.md) — 새 서비스 추가
