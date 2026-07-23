#!/usr/bin/env bash
#
# 01-install-k3s.sh — 단일 노드 k3s 서버 설치
#
# 사전 조건:
#   1. 00-preflight.sh 실행 완료
#   2. 서버 밖에서 443 도달 확인 (Connection refused = 정상, timeout = SG 차단)
#
# SSAFY가 관리하는 영역(Gerrit 8988/29418, Apache 8989)은 건드리지 않는다.
# k3s는 80/443만 사용하므로 충돌하지 않는다.
#
set -euo pipefail

clean_environment=true
if [[ ${PINLOG_K3S_BOOTSTRAP_CLEAN_ENV:-} != "$$" ||
      ${PATH:-} != "/usr/sbin:/usr/bin:/sbin:/bin" ||
      ${HOME:-} != "/root" || ${LC_ALL:-} != "C" ]]; then
  clean_environment=false
else
  while IFS='=' read -r name _; do
    case "${name}" in
      HOME|LC_ALL|PATH|PINLOG_K3S_BOOTSTRAP_CLEAN_ENV|PWD|SHLVL|_) ;;
      *) clean_environment=false ;;
    esac
  done < <(/usr/bin/env)
fi

if [[ ${clean_environment} != true ]]; then
  script_path=$(/usr/bin/readlink -f -- "$0")
  exec /usr/bin/env -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LC_ALL=C \
    PINLOG_K3S_BOOTSTRAP_CLEAN_ENV="$$" /bin/bash "${script_path}" "$@"
fi

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset KUBECONFIG KUBERNETES_MASTER DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG \
  SYSTEMD_HOST SYSTEMD_MACHINE SYSTEMD_LOG_TARGET APT_CONFIG CURL_HOME

if [[ $EUID -ne 0 ]]; then
  echo "root로 실행해야 합니다: sudo $0" >&2
  exit 1
fi

if [[ $(dpkg --print-architecture) != "amd64" ]]; then
  echo "실패: 이 bootstrap의 package와 image digest는 amd64에 고정되어 있습니다." >&2
  exit 1
fi

# 버전 고정. stable 채널을 그대로 쓰지 않고 명시적으로 박는다 —
# 채널을 쓰면 재설치 시점에 따라 다른 버전이 깔려 재현이 안 된다.
#
# 2026-07-20 기준 k3s stable 채널 값. 갱신하려면:
#   curl -s https://update.k3s.io/v1-release/channels | jq -r '.data[]|select(.id=="stable").latest'
K3S_VERSION="v1.36.2+k3s1"
K3S_BINARY_SHA256="65a55ec56c24eab44383086166ec620a491952b7e23941a49ddca6e8a4c4b4de"
DOCKER_PACKAGE_VERSION="29.1.3-0ubuntu3~24.04.2"
K3S_INSTALL_COMMIT="78ef2d8a892fd3eb2e9d641a95713f74f51cbded"
K3S_INSTALL_SHA256="d264d4d43f7c5a27b44de0075513fb22dfb02d0b7cd33ba7a3838cb822f4729c"

NODE_NAME="pinlog-master"
PUBLIC_HOST="i15a705.p.ssafy.io"
PUBLIC_IP="15.165.74.216"

echo "=== k3s ${K3S_VERSION} 설치 ==="
echo

# preflight 실행 여부 확인
if ! ufw status | grep -q "80/tcp"; then
  echo "경고: ufw에 80/tcp 규칙이 없습니다. 00-preflight.sh를 먼저 실행하세요." >&2
  read -rp "그래도 계속하시겠습니까? [y/N] " ans
  [[ "$ans" == "y" ]] || exit 1
fi

# 이 스크립트는 완전히 깨끗한 호스트의 최초 설치 전용이다. binary/unit뿐 아니라
# datastore·kubelet·CNI residue도 감지해 기존 데이터를 신규 설치로 오인하지 않는다.
residual_paths=(
  /etc/rancher
  /etc/rancher/k3s
  /etc/rancher/node
  /var/lib/rancher
  /var/lib/rancher/k3s
  /var/lib/kubelet
  /etc/cni/net.d
  /var/lib/cni
  /run/k3s
  /run/flannel
  /usr/local/bin/k3s-uninstall.sh
  /usr/local/bin/k3s-killall.sh
  /usr/local/bin/k3s
  /usr/local/bin/kubectl
  /usr/local/bin/crictl
  /usr/local/bin/ctr
  /etc/systemd/system/k3s.service
  /etc/systemd/system/k3s.service.env
  /etc/systemd/system/k3s.service.d
  /etc/systemd/system/k3s-agent.service
  /etc/systemd/system/k3s-agent.service.env
  /etc/systemd/system/k3s-agent.service.d
  /etc/default/k3s
  /etc/sysconfig/k3s
)
if command -v k3s >/dev/null 2>&1 || \
   systemctl list-unit-files k3s.service --no-legend 2>/dev/null | grep -q '^k3s\.service'; then
  echo "실패: k3s binary 또는 service가 이미 있습니다. migration/복구 절차를 사용하세요." >&2
  exit 1
fi
for path in "${residual_paths[@]}"; do
  if [[ -e "${path}" || -L "${path}" ]]; then
    echo "실패: 기존 Kubernetes 상태 ${path}를 발견했습니다. 자동 삭제하지 않습니다." >&2
    exit 1
  fi
done

# 기존 Docker/containerd도 최초 설치 대상으로 오인하지 않는다. package 교체나
# data-root 채택은 기존 standalone workload를 중단하거나 숨길 수 있으므로 fail closed다.
for package in docker.io docker-ce docker-ce-cli docker-ce-rootless-extras \
  moby-engine moby-cli containerd containerd.io podman-docker; do
  if dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q '^install ok installed$'; then
    echo "실패: 기존 container runtime package ${package}를 발견했습니다. migration 절차를 사용하세요." >&2
    exit 1
  fi
done
for binary in docker dockerd containerd nerdctl; do
  if command -v "${binary}" >/dev/null 2>&1; then
    echo "실패: 기존 container runtime binary ${binary}를 발견했습니다. 자동 교체하지 않습니다." >&2
    exit 1
  fi
done
docker_residual_paths=(
  /usr/local/bin/docker
  /usr/local/bin/dockerd
  /usr/local/bin/containerd
  /usr/local/bin/nerdctl
  /usr/local/sbin/docker
  /usr/local/sbin/dockerd
  /usr/local/sbin/containerd
  /var/lib/docker
  /etc/docker
  /etc/default/docker
  /etc/sysconfig/docker
  /run/docker.sock
  /var/run/docker.sock
  /var/lib/containerd
  /etc/containerd
  /run/containerd
  /etc/systemd/system/docker.service
  /etc/systemd/system/docker.service.d
  /etc/systemd/system/docker.socket
  /etc/systemd/system/docker.socket.d
  /etc/systemd/system/containerd.service
  /etc/systemd/system/containerd.service.d
  /var/snap/docker
  /snap/docker
  /var/lib/snapd/sequence/docker.json
  /etc/systemd/system/snap.docker.dockerd.service
)
for path in "${docker_residual_paths[@]}"; do
  if [[ -e "${path}" || -L "${path}" ]]; then
    echo "실패: 기존 container runtime 상태 ${path}를 발견했습니다. 자동 삭제하지 않습니다." >&2
    exit 1
  fi
done
for service in docker.service docker.socket containerd.service snap.docker.dockerd.service; do
  if systemctl list-unit-files "${service}" --no-legend 2>/dev/null | grep -q "^${service}"; then
    echo "실패: 기존 container runtime unit ${service}를 발견했습니다. 자동 교체하지 않습니다." >&2
    exit 1
  fi
done
if [[ -x /usr/bin/snap ]]; then
  snap_result=""
  if snap_result=$(timeout --signal=TERM --kill-after=5s 15s /usr/bin/snap list docker 2>&1); then
    echo "실패: 기존 Snap Docker package를 발견했습니다. 자동 교체하지 않습니다." >&2
    exit 1
  elif [[ ${snap_result} != "error: no matching snaps installed" ]]; then
    echo "실패: Snap Docker 설치 여부를 안전하게 확인할 수 없습니다." >&2
    exit 1
  fi
fi
if compgen -G '/var/lib/snapd/snaps/docker_*.snap' >/dev/null; then
  echo "실패: 기존 Snap Docker artifact를 발견했습니다. 자동 삭제하지 않습니다." >&2
  exit 1
fi

for path in /etc/rancher /var/lib/rancher /usr/local/bin /etc/systemd/system; do
  if [[ -L "${path}" ]]; then
    echo "실패: 쓰기 대상의 symlink 조상 ${path}를 발견했습니다. 자동 추적하지 않습니다." >&2
    exit 1
  fi
done

echo "=== Docker Engine 설치 및 검증 ==="
if ! dpkg-query -W -f='${Status}' docker.io 2>/dev/null | grep -q '^install ok installed$'; then
  timeout --signal=TERM --kill-after=30s 900s apt-get update
  timeout --signal=TERM --kill-after=30s 900s \
    apt-get install -y "docker.io=${DOCKER_PACKAGE_VERSION}"
fi
installed_docker_version=$(dpkg-query -W -f='${Version}' docker.io)
if [[ "${installed_docker_version}" != "${DOCKER_PACKAGE_VERSION}" ]]; then
  echo "실패: 예상 docker.io=${DOCKER_PACKAGE_VERSION}, 실제=${installed_docker_version}" >&2
  exit 1
fi
if [[ ! -x /usr/bin/docker ]] || ! dpkg-query -S /usr/bin/docker | grep -q '^docker\.io:'; then
  echo "실패: /usr/bin/docker가 검증된 docker.io 패키지 소유가 아닙니다." >&2
  exit 1
fi
systemctl enable docker.service
systemctl start --no-block docker.service
docker_deadline=$((SECONDS + 120))
until timeout --signal=TERM --kill-after=5s 10s \
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  DOCKER_HOST=unix:///var/run/docker.sock /usr/bin/docker info >/dev/null 2>&1; do
  if (( SECONDS >= docker_deadline )); then
    echo "실패: 120초 안에 Docker Engine이 준비되지 않았습니다." >&2
    exit 1
  fi
  sleep 2
done

installer_tmp=""
runtime_config=""
runtime_dropin=""
install_started=false
bootstrap_complete=false
cleanup_install() {
  rc=$?
  [[ -z "${installer_tmp}" ]] || rm -f "${installer_tmp}"
  if [[ ${rc} -ne 0 && "${install_started}" != true ]]; then
    # clean-host guard 뒤 이번 실행이 만든 exact 파일만 되돌린다.
    [[ -z "${runtime_config}" ]] || rm -f "${runtime_config}"
    [[ -z "${runtime_dropin}" ]] || rm -f "${runtime_dropin}"
    rm -f /etc/rancher/k3s/config.yaml \
      /etc/systemd/system/k3s.service.d/10-docker-runtime.conf
    rmdir /etc/rancher/k3s /etc/rancher /etc/systemd/system/k3s.service.d 2>/dev/null || true
    systemctl daemon-reload >/dev/null 2>&1 || true
  elif [[ ${rc} -ne 0 && "${bootstrap_complete}" != true ]]; then
    echo "실패: 불완전한 k3s를 disable/stop하고 artifact를 보존합니다." >&2
    timeout --signal=TERM --kill-after=5s 30s \
      systemctl stop k3s.service >/dev/null 2>&1 || true
    systemctl disable k3s.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet k3s.service; then
      echo "CRITICAL: k3s.service가 아직 active입니다. 자동 강제 종료하지 않습니다." >&2
    fi
    if systemctl is-enabled --quiet k3s.service; then
      echo "CRITICAL: k3s.service가 아직 enabled입니다. 다음 부팅 전 수동 확인이 필요합니다." >&2
    fi
    echo "재실행하거나 데이터를 삭제하지 말고 docs/container-runtime.md의 복구 절차를 따르세요." >&2
  fi
  trap - EXIT
  exit "${rc}"
}
trap cleanup_install EXIT

# PinLog의 runtime 계약은 Docker Engine + k3s 내장 cri-dockerd다.
# 이 파일을 installer보다 먼저 만들어 최초 기동부터 containerd drift를 막는다.
install -d -m 700 /etc/rancher/k3s
runtime_config=$(mktemp /etc/rancher/k3s/.config.yaml.XXXXXX)
printf '%s\n' 'docker: true' >"${runtime_config}"
chmod 600 "${runtime_config}"
chown root:root "${runtime_config}"
mv "${runtime_config}" /etc/rancher/k3s/config.yaml

# 첫 k3s 시작부터 Docker가 선행되도록 installer 실행 전에 drop-in을 준비한다.
install -d -m 755 /etc/systemd/system/k3s.service.d
runtime_dropin=$(mktemp /etc/systemd/system/k3s.service.d/.10-docker-runtime.conf.XXXXXX)
cat >"${runtime_dropin}" <<'EOF'
[Unit]
Requires=docker.service
After=docker.service
EOF
chmod 644 "${runtime_dropin}"
chown root:root "${runtime_dropin}"
mv "${runtime_dropin}" /etc/systemd/system/k3s.service.d/10-docker-runtime.conf
systemctl daemon-reload

installer_tmp=$(mktemp)

installer_url="https://raw.githubusercontent.com/k3s-io/k3s/${K3S_INSTALL_COMMIT}/install.sh"
curl --disable -fsSL --proto '=https' --tlsv1.2 \
  --connect-timeout 10 --max-time 120 --retry 3 --retry-delay 2 --retry-connrefused \
  "${installer_url}" -o "${installer_tmp}"
printf '%s  %s\n' "${K3S_INSTALL_SHA256}" "${installer_tmp}" | sha256sum -c -

install_started=true
timeout --signal=TERM --kill-after=30s 300s \
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
  INSTALL_K3S_VERSION="${K3S_VERSION}" \
  INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SKIP_START=true \
  sh "${installer_tmp}" server \
  --write-kubeconfig-mode 600 \
  --node-name "${NODE_NAME}" \
  --tls-san "${PUBLIC_HOST}" \
  --tls-san "${PUBLIC_IP}" \
  --kubelet-arg=system-reserved=cpu=250m,memory=768Mi \
  --kubelet-arg="eviction-hard=memory.available<500Mi,nodefs.available<10%"
printf '%s  %s\n' "${K3S_BINARY_SHA256}" /usr/local/bin/k3s | sha256sum -c -
systemctl daemon-reload
systemctl enable k3s.service
systemctl start --no-block k3s.service

# 설계 근거 (플래그를 바꾸기 전에 읽을 것):
#
#   Traefik 유지        - k3s가 관리하고 경로 라우팅/미들웨어를 지원하며 ~80Mi.
#                         ingress-nginx로 바꿔서 얻는 게 없다.
#   servicelb 유지      - --disable=servicelb 하면 안 된다. Traefik의 LoadBalancer
#                         Service를 호스트 80/443에 바인딩하는 게 이 컴포넌트다.
#                         끄면 Service가 영원히 Pending 상태가 된다.
#   metrics-server 유지 - 15Gi 박스에서 OOM 디버깅할 때 kubectl top이 필요하다.
#   SQLite 사용         - --cluster-init(etcd)는 다중 control-plane HA에만 필요하다.
#                         워커 노드는 SQLite 백엔드에도 문제없이 조인한다. ~200Mi 절약.
#   system-reserved /   - 이 서버는 swap이 없다. 여유를 남기지 않으면 커널 OOM 킬러가
#   eviction-hard         k3s 자체를 죽일 수 있다.

echo
echo "=== API 서버 기동 대기 ==="
deadline=$((SECONDS + 300))
until timeout --signal=TERM --kill-after=5s 15s \
  k3s kubectl --request-timeout=10s get nodes &>/dev/null; do
  if (( SECONDS >= deadline )); then
    echo "실패: 300초 안에 k3s API가 준비되지 않았습니다." >&2
    exit 1
  fi
  sleep 2
done

# 주의: 여기서 바로 검증하면 안 된다.
# kubectl이 응답하는 시점은 CoreDNS/Traefik이 스케줄되기 "전"이라
# 성급하게 확인하면 멀쩡한 클러스터를 실패로 오판한다.
echo
echo "=== 시스템 파드 Ready 대기 ==="
timeout --signal=TERM --kill-after=5s 310s \
  k3s kubectl wait --for=condition=Ready pod -l k8s-app=kube-dns \
    -n kube-system --timeout=300s
timeout --signal=TERM --kill-after=5s 310s \
  k3s kubectl wait --for=condition=Available deploy/local-path-provisioner \
    deploy/metrics-server -n kube-system --timeout=300s
deadline=$((SECONDS + 300))
until timeout --signal=TERM --kill-after=5s 15s \
  k3s kubectl --request-timeout=10s get deploy traefik -n kube-system &>/dev/null; do
  if (( SECONDS >= deadline )); then
    echo "실패: 300초 안에 Traefik Deployment가 생성되지 않았습니다." >&2
    exit 1
  fi
  sleep 3
done
timeout --signal=TERM --kill-after=5s 310s \
  k3s kubectl wait --for=condition=Available deploy/traefik \
    -n kube-system --timeout=300s

echo
echo "=== 노드 상태 ==="
timeout --signal=TERM --kill-after=5s 20s \
  k3s kubectl --request-timeout=15s get nodes -o wide

runtime=$(timeout --signal=TERM --kill-after=5s 20s \
  k3s kubectl --request-timeout=15s get node "${NODE_NAME}" \
    -o jsonpath='{.status.nodeInfo.containerRuntimeVersion}')
if [[ "${runtime}" != docker://* ]]; then
  echo "실패: 예상 runtime=docker://*, 실제 runtime=${runtime}" >&2
  exit 1
fi
echo "  OK  container runtime ${runtime}"

requires=$(systemctl show k3s -p Requires --value)
after=$(systemctl show k3s -p After --value)
if [[ " ${requires} " != *" docker.service "* || \
      " ${after} " != *" docker.service "* ]]; then
  echo "실패: k3s.service의 Docker dependency가 적용되지 않았습니다." >&2
  exit 1
fi
echo "  OK  k3s.service Requires/After=docker.service"

echo
echo "=== 시스템 파드 ==="
timeout --signal=TERM --kill-after=5s 20s \
  k3s kubectl --request-timeout=15s get pods -A

echo
echo "=== 파드 네트워킹 카나리아 (ufw 포워딩 검증) ==="
echo "(실패하면 ufw route 규칙 문제입니다 — 00-preflight.sh 재확인)"
# 반드시 FQDN을 쓴다. busybox의 nslookup은 짧은 이름에 search domain을
# 제대로 적용하지 못해 멀쩡한 클러스터에서도 NXDOMAIN을 낸다.
if timeout --signal=TERM --kill-after=5s 150s \
     k3s kubectl run k3s-dns-canary \
     --image=busybox@sha256:b7f3d86d6e84fc17718c48bcde1450807faa2d56704205c697b4bd5df7b9e29f \
     --rm -i --restart=Never --timeout=120s \
     -- nslookup kubernetes.default.svc.cluster.local 2>&1 \
     | grep -q "10.43.0.1"; then
  echo "  OK  파드 내부 DNS 정상"
else
  echo "  실패  파드 내부 DNS 불가 — ufw 포워딩 규칙을 확인하세요" >&2
  exit 1
fi

echo
echo "=== Traefik 80/443 응답 확인 ==="
# ss로 확인하면 안 된다. klipper-lb는 hostPort를 iptables DNAT으로 구현해서
# 리스닝 소켓이 생기지 않는다. 실제 연결로 확인해야 한다.
# Ingress가 아직 없으므로 404가 정상 응답이다.
for endpoint in "http 80" "https 443"; do
  read -r proto port <<<"${endpoint}"
  if ! code=$(curl -sk -o /dev/null -w "%{http_code}" -m 10 \
      "${proto}://localhost:${port}/"); then
    code="000"
  fi
  if [ "$code" = "000" ]; then
    echo "  실패  ${proto}://localhost:${port} 무응답" >&2
    exit 1
  fi
  echo "  OK  ${proto}://localhost:${port} -> HTTP $code (Ingress 없으므로 404가 정상)"
done

bootstrap_complete=true
rm -f "${installer_tmp}"
trap - EXIT

echo
cat <<'EOF'
=== k3s 설치 완료 ===

kubectl을 일반 사용자로 쓰려면:
  mkdir -p ~/.kube
  sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
  sudo chown $(id -u):$(id -g) ~/.kube/config

다음: sudo ./sync-tls-secret.sh
      sudo ./02-install-sealed-secrets.sh
EOF
