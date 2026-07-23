# Pod Security Admission 운영 계약

이 문서는 PinLog namespace의 Pod Security Admission(PSA) 적용 단계와 서비스 배포 계약을 정의한다. 목표는 `restricted` 정책을 즉시 차단 모드로 켜는 것이 아니라, 먼저 **audit/warn**으로 위반을 드러내고 안전하게 줄이는 것이다.

## 현재 정책

GitOps가 소유하는 다음 namespace에는 Kubernetes `v1.36` 기준 `restricted` audit/warn label을 적용한다.

- `pinlog-dev`
- `pinlog-prod`
- `monitoring`

`pod-security.kubernetes.io/enforce` label은 이 단계에서 사용하지 않는다. audit는 API audit event에 위반 annotation을 남기며, warn은 신규 Pod 생성·변경 요청의 클라이언트에 경고를 반환한다. 영구 audit event 수집은 API server audit backend가 별도로 구성된 경우에만 가능하므로, 배포 검증에는 warn 출력과 live workload 점검 결과를 함께 기록한다.

`argocd`와 `kube-system`은 위 Namespace GitOps Application의 소유 범위가 아니다. 특히 `kube-system`에는 k3s 네트워크·스토리지 컴포넌트가 있으므로 일반 서비스 namespace와 같은 enforce 정책을 적용하지 않는다.

## 2026-07-23 비차단 audit 기준선

현재 live Pod를 Kubernetes restricted 항목으로 점검한 결과다. 이는 시점성 증거이며 현재 상태는 항상 클러스터에서 다시 확인한다.

| Namespace | 관찰된 상태 | 분류 |
|---|---|---|
| `argocd` | 핵심 restricted 위반 미검출 | 향후 별도 소유권 정리 후 enforce 후보 |
| `pinlog-dev` | Cowork에 `seccompProfile` 누락 | 공용 chart의 `RuntimeDefault` 기본값으로 보완 |
| `pinlog-prod` | PostgreSQL·Redis·백업 Job에 root 실행 가능성, 권한 상승, capability drop 및 seccomp 누락 | enforce 전 workload 보완 필요 |
| `monitoring` | node-exporter는 `hostNetwork`·`hostPID`·`hostPath` 외에도 `allowPrivilegeEscalation: false`·`capabilities.drop: ["ALL"]`·seccomp가 누락됐다. Grafana `init-chown-data`는 root·CHOWN/DAC_OVERRIDE·권한 상승 제한 누락, Alloy와 config-reloader는 non-root·권한 상승 제한·capability drop·seccomp 누락, Loki와 `loki-sc-rules`는 seccomp 누락 | node-exporter의 host 접근은 운영 예외 후보이고 나머지는 보완 대상이다. restricted enforce 금지 |
| `kube-system` | k3s ServiceLB·local-path-provisioner 등 시스템 권한 필요 | 플랫폼 예외 namespace |

`monitoring`의 node-exporter처럼 노드 관측을 위해 `hostPath`와 host namespace가 필요한 workload는 단순 누락이 아니라 명시적 운영 예외다. 예외가 필요한 namespace를 restricted enforce 대상으로 잘못 분류하지 않는다.

## 서비스 배포 계약

일반 PinLog 서비스 Pod는 restricted 전환을 위해 다음 계약을 만족해야 한다.

- `runAsNonRoot: true`와 0이 아닌 UID/GID를 사용한다.
- `allowPrivilegeEscalation: false`를 사용한다.
- `capabilities.drop: ["ALL"]`을 사용하며, 추가 capability는 원칙적으로 금지한다.
- Pod 또는 container에 `seccompProfile.type: RuntimeDefault`를 선언한다.
- `privileged`, `hostNetwork`, `hostPID`, `hostIPC`, `hostPath`를 사용하지 않는다.
- Secret, ConfigMap, PVC, projected volume 등 restricted에서 허용되는 volume만 사용한다.
- 예외가 필요하면 이유, 대상 namespace/workload, 최소 권한, 만료·재검토 조건을 PR과 Jira에 기록한다.

공용 `microservice` Helm chart는 `RuntimeDefault` seccomp를 기본값으로 제공한다. 서비스 values에서 `podSecurityContext`를 재정의할 때 이 기본값을 제거하지 않는다.

## 검증 방법

정책 label과 chart render를 로컬에서 검증한다.

```bash
python3 -m unittest tests.test_pod_security_admission -v
helm template cowork charts/microservice \
  --namespace pinlog-dev \
  -f apps/dev/cowork/values.yaml
kubectl apply --dry-run=server -f platform/namespaces/namespaces.yaml
```

배포 후에는 다음을 확인한다.

```bash
kubectl get ns pinlog-dev pinlog-prod monitoring --show-labels
kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp
kubectl -n argocd get application platform-namespaces
kubectl get pods -A
```

신규 또는 변경된 workload는 server-side dry-run을 실행해 PSA warn을 수집한다. 경고가 있으면 enforce 전환 대상에서 제외하고 위반 항목을 먼저 수정한다.

## enforce 전환 조건

restricted enforce는 audit/warn 배포와 분리된 PR로 진행한다. 다음 조건을 모두 만족해야 한다.

1. 해당 namespace의 현재 Pod와 GitOps render에서 restricted 위반이 0건이다.
2. 재배포·롤링 업데이트·Job/CronJob 실행 경로까지 server-side dry-run이 통과한다.
3. 필요한 예외가 없거나 별도 namespace로 분리되었다.
4. Argo CD가 Synced/Healthy이고 rollback할 이전 Git revision이 확인된다.
5. `pinlog-dev`에서 먼저 검증하고 관찰 기간을 거친다.
6. `pinlog-prod` enforce는 별도 승인 후 수행한다.

문제가 생기면 GitOps에서 enforce label만 제거하여 audit/warn 단계로 되돌린다. 운영 workload를 임의 삭제하거나 live label을 수동으로 덮어써 GitOps와 드리프트시키지 않는다.
