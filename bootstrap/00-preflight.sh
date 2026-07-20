#!/usr/bin/env bash
#
# 00-preflight.sh — k3s 설치 "전에" 반드시 실행
#
# 이 서버의 ufw는 routed 정책이 deny 상태다. 그대로 k3s를 설치하면
# 파드는 Running인데 DNS/통신이 안 되는, 원인 파악이 어려운 상태가 된다.
# 이 스크립트가 CNI 포워딩을 열어준다.
#
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "root로 실행해야 합니다: sudo $0" >&2
  exit 1
fi

echo "=== 현재 ufw 상태 ==="
ufw status verbose || true
echo

echo "=== [1/4] ufw 규칙 추가 ==="

# HTTPS 리다이렉트 및 향후 ACME HTTP-01 갱신용.
# 주의: 보안그룹 레벨에서도 80이 열려야 실제로 외부 도달이 된다 (SSAFY 요청 필요).
ufw allow 80/tcp

# k3s(flannel) 파드 네트워크 포워딩. 이게 없으면 파드 간 통신과 DNS가 깨진다.
ufw allow in on cni0
ufw route allow in on cni0
ufw route allow out on cni0

# flannel vxlan — 워커 노드가 조인할 때 필요. 지금 열어둬도 무해하다.
ufw allow 8472/udp

# 6443(k8s API)은 의도적으로 열지 않는다. kubectl은 SSH 터널 또는 Tailscale로 접근.

ufw reload
echo
echo "=== 적용 후 ufw 상태 ==="
ufw status verbose
echo

echo "=== [2/4] 커널 모듈 확인 ==="
# k3s가 실행 시 로드하지만, 커널에 존재하는지 미리 확인한다.
for m in overlay br_netfilter vxlan iptable_nat; do
  if modinfo "$m" &>/dev/null; then
    echo "  OK   $m"
  else
    echo "  경고  $m 없음 — k3s 네트워킹에 문제가 생길 수 있습니다"
  fi
done
echo

echo "=== [3/4] 자원 기준선 기록 ==="
echo "--- 메모리 ---"
free -h
echo "--- 디스크 ---"
df -h /
echo "--- CPU ---"
nproc
echo

echo "=== [4/4] 포트 충돌 확인 ==="
if ss -tln | grep -qE ':(80|443|6443)\s'; then
  echo "  경고: 80/443/6443 중 이미 점유된 포트가 있습니다:"
  ss -tlnp | grep -E ':(80|443|6443)\s'
  echo "  Traefik이 바인딩하지 못합니다. 해결 후 진행하세요."
  exit 1
else
  echo "  OK  80, 443, 6443 모두 비어 있음"
fi
echo

cat <<'EOF'
=== preflight 완료 ===

다음 단계로 넘어가기 전에 반드시 확인할 것:

  1. 개인 노트북(서버 밖)에서 443 도달 여부를 실제로 테스트하세요:
       curl -vk https://i15a705.p.ssafy.io/

     서버 안에서 테스트하면 AWS 보안그룹 문제가 가려집니다.
     실패하면 SSAFY에 보안그룹 확인을 요청해야 하고,
     그 전까지 ingress 설계를 진행하면 안 됩니다.

  2. 80도 마찬가지로 확인하세요. ufw는 방금 열었지만
     보안그룹은 팀이 제어할 수 없습니다.

확인되면: sudo ./01-install-k3s.sh
EOF
