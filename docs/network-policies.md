# NetworkPolicy 운영 계약

## 적용 범위와 현재 단계

이 문서는 `pinlog-prod`, `pinlog-dev`, `monitoring` namespace의 Kubernetes NetworkPolicy 계약이다. k3s의 kube-router가 `KUBE-NWPLCY-*` 체인을 생성하는 것을 2026-07-21 live host에서 확인했다.

이번 단계는 **Ingress default-deny**만 적용한다. 필요한 ingress allow를 더 이른 Argo CD sync wave로 선언하고 마지막 wave에서 default-deny를 적용한다.

**Egress default-deny**는 적용하지 않는다. monitoring workload에는 Kubernetes API `6443`, kubelet `10250`, node-exporter `9100`, Sentinel ExternalName `9765`, DNS 및 namespace 내부 Loki/Prometheus 통신이 있다. 애플리케이션의 외부 API egress 계약도 아직 존재하지 않는다. 목적지 IP를 추정해 egress를 닫으면 관측·알림 또는 향후 서비스가 조용히 중단될 수 있다.

## 현재 관측된 흐름

| Source | Destination | Port | 상태/정책 |
|---|---|---:|---|
| PostgreSQL backup Job | PostgreSQL | TCP 5432 | 같은 `pinlog-prod` namespace이므로 허용 |
| Backend → PostgreSQL | PostgreSQL | TCP 5432 | 동일 namespace 서비스 계약; backend 미배포, synthetic test 대상 |
| Backend → Redis | Redis | TCP 6379 | 동일 namespace 서비스 계약; backend 미배포, synthetic test 대상 |
| Frontend → Backend | Backend HTTP | named port `http` | 동일 namespace 계약; 양쪽 workload 미배포, synthetic test 대상 |
| Traefik → ingress-enabled pod | App HTTP | named port `http` | `networking.pinlog.io/ingress=true` pod만 허용 |
| Traefik → Grafana | Grafana | named port `grafana` | kube-system Traefik에서만 허용 |
| Prometheus → metrics-enabled pod | App metrics | named port `http` | `networking.pinlog.io/metrics=true` pod만 허용 |
| Prometheus | kubelet/API/node-exporter/monitoring targets | 6443/10250/9100 등 | egress 제한 없음; 기존 scrape 유지 |
| Grafana | Prometheus/Loki | 9090/3100 | 같은 `monitoring` namespace이므로 허용 |
| Alloy | Loki/Kubernetes API | 3100/443 | ingress는 같은 namespace 허용, egress 제한 없음 |
| Alertmanager/Prometheus | Sentinel ExternalName | TCP 9765 | egress 제한 없음 |

## 정책 구조와 적용 순서

각 namespace에는 다음 정책이 존재한다.

1. wave `-3`: Traefik/Prometheus 등 외부 namespace의 좁은 allow. 해당 정책에는 같은 namespace source도 함께 넣어 다음 wave 전 일시 차단을 방지한다.
2. wave `-2`: `allow-same-namespace-ingress`.
3. wave `-1`: `default-deny-ingress`.

Kubernetes NetworkPolicy는 여러 정책의 허용 규칙을 합집합으로 계산한다. `default-deny-ingress`는 위 allow를 취소하지 않는다. `network-policies` child Application은 root wave `4`에서 생성되어 wave `3` 이하의 platform/service/monitoring child 정의보다 뒤에 생성된다. 이 순서는 child Application 객체의 생성 순서이며 workload가 Ready가 될 때까지 기다리는 barrier는 아니다. 정책 자체가 workload 부재와 무관하게 안전하게 생성되고, 더 이른 내부 wave에 필요한 allow를 포함한다.

microservice chart는 다음 label을 pod에 선언한다.

- 항상: `app.kubernetes.io/part-of=pinlog`
- Ingress 활성화 시: `networking.pinlog.io/ingress=true`
- metrics 활성화 시: `networking.pinlog.io/metrics=true`

label이 없는 pod는 Traefik 또는 cross-namespace Prometheus 접근을 받지 않는다.

## 배포 전 검증

```bash
python3 -m unittest tests.test_network_policies -v
helm lint charts/microservice
helm template contract-test charts/microservice >/tmp/network-policy-chart.yaml
kubectl apply --dry-run=server -f platform/network-policies/
kubectl apply --dry-run=server -f argocd/apps/network-policies.yaml
```

`kubectl apply --dry-run=server`는 live object를 변경하지 않는다.

## 배포 후 연결성 검증

1. Argo CD `network-policies`가 `Synced/Healthy/Succeeded`인지 확인한다.
2. 기존 경로를 확인한다.
   - Redis `PONG`
   - PostgreSQL `pg_isready`
   - Grafana HTTPS
   - Prometheus/Alertmanager/Loki readiness
   - Prometheus active target health
3. 임시 synthetic pod를 생성해 다음을 확인하고 즉시 삭제한다.
   - 같은 namespace의 Frontend → Backend 성공
   - Backend → PostgreSQL 성공
   - Backend → Redis 성공
   - monitoring Prometheus → metrics-enabled pod 성공
   - 허용되지 않은 `monitoring` pod → PostgreSQL/Redis 실패
   - 허용되지 않은 `pinlog-prod` pod → Grafana 실패
4. Running pod readiness/restart와 최근 NetworkPolicy drop/Warning event를 확인한다.

실제 front/back 서비스가 배포되면 synthetic 결과를 실제 readiness/metrics/API 요청으로 교체한다.

## 실패 징후

- Traefik은 Ready지만 앱 또는 Grafana가 502/504를 반환한다.
- Prometheus target이 `down`으로 전환된다.
- PostgreSQL backup Job이 connection timeout으로 실패한다.
- kube-router NFLOG에 `DROP by policy`가 반복된다.
- 기존 pod는 Ready지만 rollout replacement pod의 dependency 연결이 실패한다.

## rollback

정책은 GitOps가 관리하므로 live `kubectl delete networkpolicy`를 정상 rollback으로 사용하지 않는다.

1. 실패 경로와 source/destination label/port를 기록한다.
2. 아직 merge 전이면 manifest를 수정하고 dry-run 및 synthetic test를 반복한다.
3. 이 기능을 처음 도입한 merge 후 장애면 PR로 해당 introducing commit 전체를 `git revert`하고 필수 CI를 통과시킨다.
4. root Application이 `network-policies` child Application을 prune하면 `resources-finalizer.argocd.argoproj.io`가 추적 중인 NetworkPolicy를 cascade 삭제한다. Application이 `Terminating`인 동안 finalizer를 강제 제거하지 않는다.
5. `kubectl get networkpolicy -A`로 11개 정책이 사라진 뒤 endpoint, target, backup, pod readiness를 다시 검증한다. 이후 정책 변경만 되돌릴 때는 child Application과 source path를 유지한 상태에서 해당 변경 commit을 revert한다.

Egress 단계는 DNS, Kubernetes API/host endpoints, Sentinel, 외부 API allowlist와 서비스별 계약이 준비된 별도 변경에서 수행한다.
