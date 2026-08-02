#!/usr/bin/env bash
#
# 03-install-argocd.sh — ArgoCD 설치
#
# ArgoCD는 인터넷에 노출하지 않는다.
# 공개된 ArgoCD + 팀 공용 비밀번호는 클러스터 전체를 내주는 것과 같다.
# 접근은 SSH 터널 또는 Tailscale로 한다.
#
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

echo "=== ArgoCD 설치 ==="
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update argo

helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --set configs.cm."admin\.enabled"=true \
  --set dex.enabled=false \
  --set notifications.enabled=false \
  --set configs.params."server\.insecure"=true \
  --set configs.params."controller\.log\.level"=warn \
  --set configs.params."reposerver\.log\.level"=warn \
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

cat <<'EOF'
=== ArgoCD 설치 완료 ===

이 bootstrap은 credential 등 민감정보를 출력하지 않습니다.
접근·인증·복구 절차는 승인된 운영자만 docs/argocd-access-runbook.md를 따르십시오.
현재 인증 모드(admin 활성, SSO 미구성)는 유지되며 승인 전 변경하지 마십시오.

다음: ./04-bootstrap-root-app.sh
EOF
