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

if [[ $EUID -ne 0 ]]; then
  echo "root로 실행해야 합니다: sudo $0" >&2
  exit 1
fi

# 버전 고정. latest 금지 — 재현 불가능한 환경이 된다.
# 설치 전 https://github.com/k3s-io/k3s/releases 에서 최신 안정 패치를 확인할 것.
K3S_VERSION="${K3S_VERSION:-v1.31.5+k3s1}"

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

curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="${K3S_VERSION}" sh -s - server \
  --write-kubeconfig-mode 600 \
  --node-name "${NODE_NAME}" \
  --tls-san "${PUBLIC_HOST}" \
  --tls-san "${PUBLIC_IP}" \
  --kubelet-arg=system-reserved=cpu=250m,memory=768Mi \
  --kubelet-arg="eviction-hard=memory.available<500Mi,nodefs.available<10%"

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
echo "=== 기동 대기 ==="
for _ in {1..60}; do
  if k3s kubectl get nodes &>/dev/null; then break; fi
  sleep 2
done

echo
echo "=== 노드 상태 ==="
k3s kubectl get nodes -o wide

echo
echo "=== 시스템 파드 ==="
k3s kubectl get pods -A

echo
echo "=== 파드 네트워킹 카나리아 테스트 ==="
echo "(실패하면 ufw route 규칙 문제입니다 — 00-preflight.sh 재확인)"
if k3s kubectl run preflight-dns-test --image=busybox:1.36 --rm -i --restart=Never \
     --timeout=90s -- nslookup kubernetes.default 2>&1 | grep -q "Address"; then
  echo "  OK  파드 내부 DNS 정상"
else
  echo "  실패  파드 내부 DNS 불가 — ufw 포워딩 규칙을 확인하세요" >&2
  exit 1
fi

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
