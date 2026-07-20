#!/usr/bin/env bash
#
# 04-bootstrap-root-app.sh — GitOps 진입점
#
# 이것이 마지막 수동 kubectl apply다.
# 이후 모든 클러스터 변경은 infra 저장소 커밋으로만 이루어진다.
#
set -euo pipefail

KUBECTL="${KUBECTL:-k3s kubectl}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== AppProject 생성 ==="
${KUBECTL} apply -f "${REPO_ROOT}/argocd/projects/pinlog.yaml"

echo
echo "=== 루트 Application (app-of-apps) 생성 ==="
${KUBECTL} apply -f "${REPO_ROOT}/argocd/root/root-app.yaml"

echo
echo "=== 동기화 대기 ==="
sleep 15
${KUBECTL} -n argocd get applications

echo
cat <<'EOF'
=== GitOps 부트스트랩 완료 ===

이 시점부터 클러스터는 git이 진실의 원천입니다.
수동 kubectl apply를 하지 마세요 — ArgoCD가 되돌립니다.

확인:
  k3s kubectl -n argocd get applications
  # 전부 Synced / Healthy 여야 합니다

서비스 추가:
  apps/prod/<서비스명>/values.yaml 생성 후 커밋
  ApplicationSet이 자동으로 Application을 만듭니다
EOF
