# 컨테이너 runtime

PinLog의 호스트 runtime 계약과 운영 절차를 정의한다.

## 계약

```text
Kubernetes / k3s
  → 내장 cri-dockerd
  → Docker Engine
  → OCI 컨테이너
```

- Node의 `containerRuntimeVersion`은 `docker://<version>`이어야 한다.
- Docker Engine은 컨테이너 실행 계층이고, Pod·Deployment·Service·재시작·rollout은
  계속 Kubernetes와 Argo CD가 관리한다.
- Dockerfile로 만든 OCI image는 Docker와 containerd 양쪽에서 실행할 수 있다.
  runtime을 Docker로 선택했다고 image 형식이 바뀌는 것은 아니다.
- 호스트에서 실행한 `docker run` 컨테이너는 k3s가 관리하거나 채택하지 않는다.
  운영 workload는 반드시 Kubernetes `Deployment`·`StatefulSet`·`DaemonSet`으로
  선언한다.

K3s `v1.36.2+k3s1`의 도움말은 `--docker` 경로를 experimental로 표시한다. 이
프로젝트는 원래 설계 계약에 따라 해당 경로를 의도적으로 사용하지만, K3s 또는
Docker를 업그레이드할 때마다 cri-dockerd 호환성과 전체 runtime smoke를 다시
검증해야 한다.

## 최초 설치

`bootstrap/01-install-k3s.sh`가 다음 순서를 보장한다.

이 스크립트는 완전히 깨끗한 호스트의 최초 설치 전용이다. k3s binary/service, Rancher
datastore, kubelet, CNI, runtime directory, uninstall script뿐 아니라 기존 Docker/containerd
package, binary, data-root, socket, systemd override가 하나라도 있으면 아무 변경도 하지 않고
중단한다. 기존 node·standalone container·불완전 설치는 아래 migration·복구 절차를 사용한다.
Snap Docker의 package·data·unit·artifact와 쓰기 대상의 symlink 조상도 clean host로
간주하지 않는다.

bootstrap 전체를 system-only PATH의 최소 환경으로 한 번 재실행하고 export 환경
whitelist도 검증하므로 marker가 외부에서 맞춰져도 임의 환경을 유지할 수 없다. 호출자의
`KUBECONFIG`, `DOCKER_HOST`/context, systemd remote target, APT·curl config 환경은
상속하지 않는다. Docker readiness는 로컬 Unix socket을 지정하고, installer curl은 사용자
`.curlrc`를 비활성화한다. Snap 조회도 timeout을 두며 조회 오류는 clean으로 간주하지 않는다.

1. Ubuntu `docker.io` 패키지를 검증된 exact package version으로 설치하고
   `/usr/bin/docker`의 package ownership을 확인한다.
2. `docker.service`를 enable/start하고 `docker info`를 검증한다.
3. clean-host guard 통과 후 `/etc/rancher/k3s/config.yaml`에 `docker: true`를
   mode `0600`으로 기록한다.
4. k3s installer를 실행하기 전에 systemd drop-in으로 `k3s.service`에
   `Requires/After=docker.service`를 준비한다.
5. K3s 공식 GitHub의 특정 commit에 고정된 installer를 SHA-256 검증한 뒤
   `INSTALL_K3S_SKIP_ENABLE=true`, `INSTALL_K3S_SKIP_START=true`로 실행한다.
   설치된 amd64 k3s binary도 저장소에 고정한 SHA-256과 다시 대조한다.
6. bootstrap이 최소 환경(`env -i`)으로 installer를 실행하고, k3s를 enable한 뒤
   `start --no-block`으로 시작한다. API와 Traefik 대기는 각각 300초로 제한한다.
7. Node runtime이 `docker://`이고 실제 systemd dependency가 로드됐는지
   fail-closed로 검증한다.

Docker start는 비동기로 요청하고 `docker info`를 최대 120초 기다린다. 네트워크 stall도
무한 대기하지 않는다. APT 단계는 각 900초, installer 파일 download는
connect 10초·전체 120초·3회 retry, installer 내부 binary/hash download를 포함한 실행은
300초로 제한한다. API/Traefik polling의 각 `kubectl` 요청도 10~15초로 제한하고,
DNS 카나리아 이미지는 linux/amd64 manifest digest로 고정한다.

mutable `get.k3s.io`를 shell로 pipe하지 않는다. installer는 unit만 준비하고,
bootstrap이 직접 첫 start와 bounded 검증을 수행한다. 최초 설치 후 API·system Pod·runtime
검증이 실패하면 k3s service를 disable/stop하고 artifact를 보존한다. 자동 uninstall이나
datastore·kubelet·CNI 삭제는 수행하지 않는다. 원인과 데이터 소유권을 확인한 후 별도
승인된 복구 절차로만 정리한다.

실패 cleanup은 stop을 30초로 제한하고 stop·disable 결과를 각각 확인한다. 실패한 service가
여전히 active 또는 enabled이면 `CRITICAL`을 출력하되, stateful workload를 임의 SIGKILL하거나
datastore를 삭제하지 않는다. 다음 부팅 전에 운영자가 상태를 확인해야 한다.

cleanup trap은 `config.yaml`과 systemd drop-in을 만들기 전에 등록한다. installer를 실행하기
전 파일 생성·download·checksum 단계에서 실패하면 clean-host guard 뒤 이번 실행이 만든
final·temporary 파일만 정확한 경로로 제거하고 빈 directory만 `rmdir`한다. installer가
시작된 뒤에는 원인 분석을 위해 모든 artifact를 보존한다.

Docker 또는 K3s upgrade는 bootstrap 상수만 임의 수정하지 않는다. 동일 PR에서 distro
repository에 exact Docker package가 존재하는지, pinned installer의 SHA-256, cri-dockerd
호환성, 전체 runtime smoke를 검증하고 architecture 버전 표를 함께 갱신한다.

현재 설정 확인:

```bash
kubectl get node pinlog-master \
  -o jsonpath='{.status.nodeInfo.containerRuntimeVersion}{"\n"}'
systemctl show k3s -p Requires -p After
systemctl is-active docker k3s
```

## 기존 containerd 노드 migration

runtime migration은 모든 Pod를 다시 만드는 유지보수 작업이다. 다음을 먼저 확보한다.

- k3s datastore의 일관된 snapshot과 server token의 root-only 백업
- PostgreSQL 최신 backup 및 `pg_restore --list` 검증
- 현재 Node·Pod·PVC·Argo CD baseline
- 명시적인 rollback 기준과 유지보수 시간

`docker: true`를 추가하고 `systemctl restart k3s`만 수행하면 이전 embedded
containerd의 shim과 container process가 stale 상태로 남을 수 있다. 이 경우 새 Docker
컨테이너와 이전 컨테이너가 동시에 실행되어 host port와 Prometheus/Grafana/Loki lock,
심하면 데이터 PVC를 중복 점유한다.

따라서 기존 노드 전환은 다음 순서로 수행한다.

1. stateful workload backup을 검증한다.
2. k3s를 중지하고 이전 runtime의 workload가 완전히 종료됐는지 확인한다.
3. Docker Engine과 systemd dependency를 구성한다.
4. `docker: true`를 적용하고 k3s를 시작한다.
5. 이전 `cri-containerd-*` cgroup process와 K3s embedded containerd shim이 0인지 확인한다.
6. active Pod 전체 Ready, PVC Bound, Argo CD Synced/Healthy를 확인한다.
7. PostgreSQL query, Redis ping, DNS, ClusterIP, Ingress, monitoring readiness를 실제 호출한다.
8. migration 후 backup을 생성하고 archive parse를 다시 확인한다.

stale process가 발견되면 PID를 추측해 종료하지 않는다. 실행 경로, runtime namespace,
start time, cgroup을 모두 확인해 Docker `moby` process와 구분한다. 데이터 프로세스가
중복 실행 중이면 먼저 이전 runtime process를 정리하고 해당 controller의 Pod를
재생성해 lock과 backoff를 초기화한다.

## rollback

다음 중 하나면 Docker runtime 전환을 완료로 판정하지 않는다.

- Node가 `docker://`로 등록되지 않음
- old containerd process가 남음
- active Pod 또는 controller가 Ready로 복구되지 않음
- PostgreSQL·Redis·DNS·ClusterIP·Ingress 검증 실패
- PVC 또는 Argo CD 상태 이상

rollback은 유지보수 상태에서 수행한다. Docker workload를 먼저 정상 종료하고,
`/etc/rancher/k3s/config.yaml`의 `docker: true`와 Docker dependency를 제거한 뒤 k3s를
containerd로 재기동한다. 단순 config 삭제 후 restart만 하면 반대 방향에서도 두
runtime의 process가 중복될 수 있으므로 process·mount·CNI 정리와 전체 검증을 같은
절차로 수행한다.

## cloudflared와 애플리케이션

Cloudflare가 제공하는 `docker run cloudflare/cloudflared ...`는 image와 인자를
설명하는 단일 Docker 호스트용 예시다. PinLog에서는 동일 OCI image를 digest로 고정해
Kubernetes Deployment로 배포하고 token은 SealedSecret으로 전달한다. Docker runtime을
사용하더라도 독립 `docker run`은 GitOps 관리 범위 밖이므로 사용하지 않는다.
