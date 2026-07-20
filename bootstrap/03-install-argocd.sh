#!/usr/bin/env bash
#
# 03-install-argocd.sh — ArgoCD 설치
#
# ArgoCD는 인터넷에 노출하지 않는다.
# 공개된 ArgoCD + 팀 공용 비밀번호는 클러스터 전체를 내주는 것과 같다.
# 접근은 SSH 터널 또는 Tailscale로 한다.
#
set -euo pipefail

KUBECTL="${KUBECTL:-k3s kubectl}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

echo "=== ArgoCD 설치 ==="
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update argo

helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --set dex.enabled=false \
  --set notifications.enabled=false \
  --set configs.params."server\.insecure"=true \
  --set server.service.type=ClusterIP \
  --set controller.resources.requests.memory=256Mi \
  --set controller.resources.limits.memory=768Mi \
  --set repoServer.resources.requests.memory=128Mi \
  --set repoServer.resources.limits.memory=512Mi \
  --set server.resources.requests.memory=128Mi \
  --set server.resources.limits.memory=256Mi \
  --wait --timeout 10m

# server.insecure=true 인 이유:
#   Traefik이 TLS를 종단하므로 ArgoCD 자체 TLS를 켜두면
#   전형적인 무한 리다이렉트 루프에 빠진다.
#
# dex.enabled=false, notifications.enabled=false 인 이유:
#   쓰지 않는 컴포넌트로 15Gi 중 수백 Mi를 낭비할 이유가 없다.

echo
echo "=== 초기 admin 비밀번호 ==="
${KUBECTL} -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || echo "(이미 삭제됨)"
echo
echo

cat <<'EOF'
=== ArgoCD 설치 완료 ===

접속 (SSH 터널):
  # 로컬 노트북에서
  ssh -L 8080:localhost:8080 ubuntu@i15a705.p.ssafy.io
  # 서버에서
  sudo k3s kubectl port-forward svc/argocd-server -n argocd 8080:80
  # 브라우저에서 http://localhost:8080

첫 로그인 후 반드시:
  1. admin 비밀번호 변경
  2. 초기 비밀번호 Secret 삭제:
       k3s kubectl -n argocd delete secret argocd-initial-admin-secret

다음: ./04-bootstrap-root-app.sh
EOF
