# metrics-server 운영 계약

## 배경과 원인

이 클러스터는 현재 K3s embedded containerd runtime을 사용한다. 저용량 profile은
Docker·cri-dockerd를 사용하던 당시 kubelet의 인증된 `/metrics/resource` 응답이
container resource stats 수집을 기다리며 약 12~15초 걸렸던 장애에서 도입됐다.
네트워크 자체는 정상이었고 같은 Pod network namespace에서 노드 `10250` 포트의
인증 전 응답은 1ms 이내였지만 실제 resource metrics 요청만 길어졌다.

k3s 기본 metrics-server의 `--kubelet-request-timeout=10s`보다 resource endpoint가
느려 모든 scrape가 timeout 났고, 결과적으로 Pod가 `0/1 Ready`, Metrics APIService가
`MissingEndpoints`가 되었다. 기본 manifest의 `--metric-resolution=15s`도 4 vCPU
단일 노드에서 비싼 stats 수집을 반복해 CPU contention을 키웠다.

## PinLog 설정

현재 **저용량 상시 프로필에서는 Deployment replicas를 0**으로 유지한다. HPA가
없어 scheduling 기능 손실은 없고, `kubectl top`은 의도적으로 제공하지 않는다.
systemd unit의 `DESIRED_REPLICAS=0`과 5분 timer가 k3s packaged manifest의 재생성
뒤에도 scale-zero를 복구한다. profile 해제는 이 값을 1로 바꾸는 별도 PR·설치로만
수행한다.

`bootstrap/tune-metrics-server.sh`는 다른 인자를 보존하면서 replicas와 다음 두
인자를 idempotent하게 맞춘다. GET 이후 동시 변경을 덮어쓰지 않도록
`resourceVersion`, 현재 replicas와 args를 JSON Patch 선행조건으로 검사하고,
충돌하면 최신 Deployment를 다시 읽어 bounded retry한다.

- `--metric-resolution=60s`: stats 수집 빈도를 낮춰 runtime 부하를 줄인다.
- `--kubelet-request-timeout=30s`: 관측된 12~15초 응답을 안전하게 수용한다.

k3s packaged component manifest는 k3s 시작 시 다시 작성된다. 따라서
`pinlog-metrics-server-tuning.service`가 k3s 시작 뒤 tuning을 적용하고,
`pinlog-metrics-server-tuning.timer`가 5분마다 drift를 확인·복구한다. service는
적용 후 30초 안정화 구간 동안 설정이 유지되는지도 검증하며, 실패하면 bounded
retry한다. 저용량 profile 설치·적용은 다음 명령으로 수행한다.

```bash
sudo ./bootstrap/install-metrics-server-tuning.sh
```

이 작업은 metrics-server Deployment의 args를 보정하고 replicas를 0으로 내린다.
노드 재부팅이나 k3s 재시작은 필요하지 않는다.

## 검증

저용량 profile 완료 조건은 service·timer enabled, timer active, service result
success, Deployment desired/status/ready/available `0/0/0/0`, metrics-server Pod `0`이다.
service는 spec만 바꾸고 성공 처리하지 않으며 status counters와 Pod inventory가 모두
0이 될 때까지 최대 60초 bounded wait한다. Metrics API/`kubectl top`이 unavailable인
것이 정상이다. k3s 재시작은 이 검증을 위해 수행하지 않는다. 다음 timer 주기(최대
5분) 뒤에도 replicas와 Pod가 0인지 확인한다.

```bash
systemctl is-enabled pinlog-metrics-server-tuning.service
systemctl is-enabled pinlog-metrics-server-tuning.timer
systemctl is-active pinlog-metrics-server-tuning.timer
systemctl show pinlog-metrics-server-tuning.service -p Result
kubectl -n kube-system get deploy,pod,svc,endpoints metrics-server
kubectl get apiservice v1beta1.metrics.k8s.io
```

재활성화가 필요해도 현재 scale-zero는 **의도적** 설정이므로 무조건 enable하지 않는다.
복구 설계는 `bootstrap/pinlog-metrics-server-tuning.service`의
`DESIRED_REPLICAS=0`을 1로 바꾸는 별도 PR, resource budget 검토, CI/render 검증,
승인된 설치 순서로 구성한다. 이 변경의 acceptance와 live 적용 전 영향(상시 CPU·메모리
증가, kubelet stats 조회 부하, Metrics API 복구)을 승인자가 확인한 뒤에만 실행한다.
그때만 다음을 검증한다.

- 변경 전 노드 allocatable 대비 전체 requests가 80% 미만이고 metrics-server request/limit을 더해도 resource budget 이내
- metrics-server Pod `1/1 Ready`
- APIService `Available=True`
- `kubectl top node`와 `kubectl top pods -A` 성공
- 최소 10분 동안 새 kubelet timeout 없음
- CPU PSI·CPU steal이 활성화 전 baseline보다 유의하게 악화되지 않음
- PostgreSQL, Redis, Cowork 및 Argo CD 상태에 회귀 없음

위 acceptance가 하나라도 실패하면 `DESIRED_REPLICAS=0`으로 되돌리고 service를 재실행한다.

## Rollback

```bash
sudo systemctl disable --now pinlog-metrics-server-tuning.timer
sudo systemctl disable --now pinlog-metrics-server-tuning.service
kubectl apply -f /var/lib/rancher/k3s/server/manifests/metrics-server/metrics-server-deployment.yaml
kubectl -n kube-system rollout status deployment/metrics-server --timeout=120s
```

Rollback은 k3s packaged manifest의 기본 `15s/10s` 설정으로 되돌린다. 이후
metrics-server가 replicas 1로 다시 실행되고 timeout 날 수 있으므로 저용량 profile
자체를 철회하는 영향 큰 변경으로 취급하고 Metrics API와 workload 상태를 함께
확인한다.
