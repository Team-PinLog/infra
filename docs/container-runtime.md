# 컨테이너 runtime

PinLog의 호스트 runtime 계약과 운영 절차를 정의한다.

## 현재 계약

```text
Kubernetes / k3s
  → K3s embedded containerd
  → OCI 컨테이너
```

- Node의 `containerRuntimeVersion`은 `containerd://<version>`이어야 한다.
- 별도 Docker Engine, system `containerd.service`, cri-dockerd를 Kubernetes runtime으로 사용하지 않는다.
- scheduling, desired state, restart, rollout, Service와 network는 Kubernetes와 Argo CD가 관리한다.
- 운영 workload는 `Deployment`·`StatefulSet`·`DaemonSet`으로만 선언한다.
- 독립 `docker run` workload가 0이어야 하며 Docker socket·`/var/lib/docker`를 Pod에 mount하지 않는다.
- Dockerfile로 만든 GHCR OCI image와 Kubernetes `imagePullSecrets`는 embedded containerd에서 그대로 사용한다.

Docker/cri-dockerd 경로는 K3s `v1.36.2+k3s1`에서 experimental이며, 2026-07-28 PinLog
단일 노드에서 Kubelet PLEG의 약 1초 주기 `ListPodSandbox`·`ListContainers`와 주기적
`ListImages`가 dockerd와 Docker의 system containerd를 지속 포화시켰다. native containerd
전환 후 이 bridge를 제거했다.

## clean-host 최초 설치

`bootstrap/01-install-k3s.sh`는 완전히 깨끗한 amd64 호스트 전용이다.

1. 기존 K3s datastore, kubelet, CNI, binary, service가 있으면 아무것도 삭제하지 않고 중단한다.
2. Docker·Moby·system containerd package, binary, socket, data root, systemd unit 또는 Snap
   artifact가 있으면 기존 workload를 채택하거나 교체하지 않고 중단한다.
3. K3s installer URL을 exact Git commit에 고정하고 SHA-256을 검증한다.
4. installer와 K3s version·binary checksum을 고정하고 `INSTALL_K3S_SKIP_ENABLE=true`,
   `INSTALL_K3S_SKIP_START=true`로 unit만 설치한다.
5. 별도 runtime package, `docker: true`, Docker systemd dependency를 만들지 않는다.
6. K3s를 enable/start하고 API, CoreDNS, local-path-provisioner, metrics-server, Traefik을 bounded wait한다.
7. Node runtime이 `containerd://`이고 `Requires/After`에 `docker.service`가 없음을 fail-closed로 확인한다.
8. digest-pinned DNS canary와 localhost 80/443 smoke를 실제 실행한다.

설치 실패 시 bounded stop/disable만 수행하고 datastore·kubelet·CNI를 자동 삭제하지 않는다.
기존 cluster에는 이 스크립트를 재실행하지 않고 아래 migration 또는 rollback 절차를 사용한다.

현재 확인:

```bash
kubectl get node pinlog-master \
  -o jsonpath='{.status.nodeInfo.containerRuntimeVersion}{"\n"}'
systemctl show k3s -p Requires -p After
systemctl is-active k3s
! systemctl is-active --quiet docker
! systemctl is-active --quiet containerd
k3s crictl ps
```

`systemctl is-active`는 inactive unit에서 exit 3을 반환하므로 Docker와 system containerd는
위처럼 부정 assertion으로 각각 확인한다.

## Docker/cri-dockerd → native containerd migration

이 작업은 모든 Pod를 재생성하는 전체 maintenance다. VM reboot는 필요하지 않다.
`docker: true`만 삭제하고 restart하면 두 runtime의 process·mount·container가 겹칠 수 있으므로
아래 순서를 생략하지 않는다.

### 사전 gate

- Frontend·AI activation과 backup verification 같은 예약 mutation을 pause한다.
- Docker running container를 label로 분류해 Kubernetes 관리 container와 독립 container를 구분한다.
- 독립 `docker run` workload가 0이 아니면 중단한다.
- Pod spec에서 Docker socket/hostPath, RuntimeClass, privileged runtime 의존성을 감사한다.
- PostgreSQL fresh backup을 생성하고 non-empty 및 `pg_restore --list` 성공을 확인한다.
- Node, Pod, controller, PVC, Argo CD와 Docker container logical identity baseline을 root-only로 보존한다.
- `/etc/rancher/k3s/config.yaml`, `10-docker-runtime.conf`, K3s service와 checksum을 root-only rollback 디렉터리에 보존한다.
- `docker.service`, `docker.socket`, `containerd.service`의 기존 `is-enabled 상태`를 **어떤 unit mutation보다 먼저** 기록한다.
- config, systemd drop-in과 embedded containerd directory를 atomic rename할 각 destination parent가
  해당 source와 same filesystem인지 source별로 `stat -c %d`(`st_dev`)를 독립 확인한다.
  `/etc/rancher`, `/etc/systemd`, `/var/lib`는 서로 다른 filesystem일 수 있다. 하나라도 다르면 중단하고
  각 source와 같은 filesystem의 artifact 경로를 따로 사용한다.

### cutover

1. `systemctl stop k3s.service`를 bounded 실행하고 `inactive`를 확인한다.
2. K3s 중지 후에도 남은 Kubernetes 관리 Docker container만 graceful stop한다. 독립 container가
   새로 나타나거나 baseline count가 다르면 중단한다. `docker ps` 기준 running Docker container가 0인지
   확인하되 rollback 증거인 exited container metadata는 삭제하지 않는다.
3. `/usr/local/bin/k3s-killall.sh`로 stale shim, kubelet mount와 CNI state를 정리한다.
   이 script가 Tailscale advertised routes를 비울 수 있으므로 기존 값을 먼저 보존하고 필요 시 복원한다.
4. K3s가 중지된 상태에서 SQLite `state.db`와 server token을 mode 0600 root-only artifact로 복사하고 checksum을 기록한다.
5. 실제 `containerd-shim*` executable이 0이고 runtime mount가 없음을 확인한다.
6. `/var/lib/rancher/k3s/agent/containerd`를 same filesystem의 rollback 디렉터리
   `containerd-pre-migration`으로 atomic rename한다. 기존 metadata를 fresh runtime에 재사용하지 않는다.
7. `docker: true` config와 `10-docker-runtime.conf`를 삭제하지 말고 사전 확인한 same filesystem의
   rollback 디렉터리로 atomic rename한다.
8. `systemctl daemon-reload` 후 K3s `Requires/After`에 `docker.service`가 없음을 확인한다.
9. running Docker container가 0인 상태에서 bounded `systemctl disable --now docker.service docker.socket
   containerd.service`를 실행한다. 세 unit이 inactive이고 기존 runtime process가 0인지 확인한다.
10. `systemctl start --no-block k3s.service` 후 API와 빈 version이 아닌 Node runtime
    `containerd://<version>`을 bounded wait한다.

### 복구 검증

다음을 모두 통과해야 migration 완료로 판정한다.

- dockerd 0, system containerd 0, K3s embedded containerd 1
- `k3s crictl ps`가 정상 응답하고 running container가 존재
- 모든 active Deployment·StatefulSet·DaemonSet의 desired=ready
- 모든 PVC Bound, Node Ready, Memory/Disk/PID Pressure 없음
- Argo CD Application 전체 Synced/Healthy
- PostgreSQL `pg_isready`, Redis `PING`, Backend liveness/readiness, DNS, Service Endpoint 실제 호출 성공
- migration 후 PostgreSQL backup Job과 `pg_restore --list` 성공
- 복구 burst 뒤 10초 간격 31표본의 **5분 capacity gate**에서 CPU PSI avg60이 매번 25 미만이고 full PSI가 0

통과 후에도 Docker package, data root, exited container metadata와 `containerd-pre-migration`은
안정화·rollback 기간에는 삭제하지 않는다.

## rollback

다음 중 하나면 native containerd 전환을 완료로 판정하지 않는다.

- Node runtime이 `containerd://`가 아님
- stale Docker 또는 embedded containerd process가 남음
- controller, PVC, Argo CD 또는 service functional check 실패
- post-migration backup 실패
- 5분 capacity gate 실패

rollback도 전체 maintenance로 수행한다.

1. K3s를 중지하고 native containerd workload를 정상 종료한다.
2. `k3s-killall.sh` 후 shim·mount·CNI state가 정리됐는지 확인한다.
3. migration 이후 생성된 embedded containerd directory를 별도 실패 artifact로 격리한다.
4. root-only artifact의 `docker: true` config와 `10-docker-runtime.conf`를 원래 위치로 atomic 복원하고 checksum을 확인한다.
5. cutover 전에 기록한 enable 상태대로 Docker socket/service와 system containerd를 enable/start한다.
6. `systemctl daemon-reload` 후 K3s를 시작하고 Node runtime `docker://`를 확인한다.
7. 동일한 controller·PVC·Argo CD·PostgreSQL·Redis·Backend·DNS 검증과 backup을 다시 실행한다.

PID를 추측해 kill하거나 state directory를 삭제하지 않는다. 실행파일, PPID, cgroup과 runtime namespace로
소유권을 확인한다. 반복 restart, VM reboot, PVC 삭제로 우회하지 않는다.

## OCI image와 애플리케이션

Cloudflare나 공급자가 보여주는 `docker run`은 image와 인자를 설명하는 단일-host 예시일 뿐이다.
PinLog에서는 동일 OCI image를 digest로 고정한 Kubernetes Deployment로 배포하고 credential은
SealedSecret/imagePullSecret으로 전달한다. Docker CLI나 daemon 없이도 application GitOps 계약은 변하지 않는다.
