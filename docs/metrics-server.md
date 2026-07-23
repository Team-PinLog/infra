# metrics-server 운영 계약

## 배경과 원인

이 클러스터는 단일 k3s 노드에서 Docker·cri-dockerd runtime을 사용한다. 현재
workload 밀도에서 kubelet의 인증된 `/metrics/resource` 응답은 Docker/CRI의
container resource stats 수집을 기다리며 약 12~15초가 걸렸다. 네트워크 자체는
정상이었다. 같은 Pod network namespace에서 노드 `10250` 포트의 인증 전 응답은
1ms 이내였지만, 실제 resource metrics 요청만 길어졌다.

k3s 기본 metrics-server의 `--kubelet-request-timeout=10s`보다 resource endpoint가
느려 모든 scrape가 timeout 났고, 결과적으로 Pod가 `0/1 Ready`, Metrics APIService가
`MissingEndpoints`가 되었다. 기본 manifest의 `--metric-resolution=15s`도 4 vCPU
단일 노드에서 비싼 stats 수집을 반복해 CPU contention을 키웠다.

## PinLog 설정

`bootstrap/tune-metrics-server.sh`는 다른 인자를 보존하면서 다음 두 인자만
idempotent하게 맞춘다.

- `--metric-resolution=60s`: stats 수집 빈도를 낮춰 runtime 부하를 줄인다.
- `--kubelet-request-timeout=30s`: 관측된 12~15초 응답을 안전하게 수용한다.

k3s packaged component manifest는 k3s 시작 시 다시 작성된다. 따라서
`pinlog-metrics-server-tuning.service`가 k3s 시작 뒤 tuning을 적용하고,
`pinlog-metrics-server-tuning.timer`가 5분마다 drift를 확인·복구한다. service는
적용 후 30초 안정화 구간 동안 설정이 유지되는지도 검증하며, 실패하면 bounded
retry한다. 설치와 현재 클러스터 적용은 다음 명령으로 수행한다.

```bash
sudo ./bootstrap/install-metrics-server-tuning.sh
```

이 작업은 metrics-server Deployment만 rolling update한다. 노드 재부팅이나 k3s 재시작은 필요하지 않는다.

## 검증

```bash
systemctl is-enabled pinlog-metrics-server-tuning.service
systemctl is-enabled pinlog-metrics-server-tuning.timer
systemctl is-active pinlog-metrics-server-tuning.timer
systemctl show pinlog-metrics-server-tuning.service -p Result
kubectl -n kube-system get deploy,pod,svc,endpoints metrics-server
kubectl get apiservice v1beta1.metrics.k8s.io
kubectl top node
kubectl top pods -A
kubectl -n kube-system logs deploy/metrics-server --since=10m
```

완료 조건은 다음과 같다.

- metrics-server Pod `1/1 Ready`
- APIService `Available=True`
- `kubectl top node`와 `kubectl top pods -A` 성공
- 최소 10분 동안 새 kubelet timeout 없음
- PostgreSQL, Redis, Cowork 및 Argo CD 상태에 회귀 없음

## Rollback

```bash
sudo systemctl disable --now pinlog-metrics-server-tuning.timer
sudo systemctl disable --now pinlog-metrics-server-tuning.service
kubectl apply -f /var/lib/rancher/k3s/server/manifests/metrics-server/metrics-server-deployment.yaml
kubectl -n kube-system rollout status deployment/metrics-server --timeout=120s
```

Rollback은 k3s packaged manifest의 기본 `15s/10s` 설정으로 되돌린다. 이후
metrics-server가 다시 timeout 날 수 있으므로 Metrics API 상태를 함께 확인한다.
