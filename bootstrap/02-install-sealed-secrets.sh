#!/usr/bin/env bash
#
# 02-install-sealed-secrets.sh — Sealed Secrets 컨트롤러 설치
#
# ArgoCD보다 "먼저" 설치해야 한다.
# ArgoCD가 SealedSecret을 참조하는 앱을 동기화하려면 컨트롤러가 이미 있어야 한다.
#
set -euo pipefail

KUBECTL="${KUBECTL:-k3s kubectl}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

if ! command -v helm &>/dev/null; then
  echo "=== helm 설치 ==="
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

echo "=== Sealed Secrets 컨트롤러 설치 ==="
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm repo update sealed-secrets

helm upgrade --install sealed-secrets sealed-secrets/sealed-secrets \
  --namespace kube-system \
  --set fullnameOverride=sealed-secrets-controller \
  --set resources.requests.memory=32Mi \
  --set resources.limits.memory=128Mi \
  --wait

echo
echo "=== kubeseal CLI 설치 ==="
KUBESEAL_VERSION="${KUBESEAL_VERSION:-0.27.1}"
if ! command -v kubeseal &>/dev/null; then
  TMP=$(mktemp -d)
  curl -fsSL -o "${TMP}/kubeseal.tar.gz" \
    "https://github.com/bitnami-labs/sealed-secrets/releases/download/v${KUBESEAL_VERSION}/kubeseal-${KUBESEAL_VERSION}-linux-amd64.tar.gz"
  tar -xzf "${TMP}/kubeseal.tar.gz" -C "${TMP}" kubeseal
  install -m 755 "${TMP}/kubeseal" /usr/local/bin/kubeseal
  rm -rf "${TMP}"
fi
kubeseal --version

echo
cat <<'EOF'

  ***  지금 바로 해야 할 일: 컨트롤러 개인키 백업  ***

  이 키를 잃어버리고 클러스터를 재구축하면, 저장소에 있는
  모든 SealedSecret이 영구히 복호화 불가능해집니다.
  하필 발표 압박 속에서 전부 다시 만들게 됩니다.

  실행:
    k3s kubectl -n kube-system get secret \
      -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml \
      > sealed-secrets-master.key

  이 파일을 팀 비밀번호 관리자에 보관하고 로컬에서 삭제하세요.
  절대 git에 커밋하지 마세요.

EOF

echo "다음: sudo ./03-install-argocd.sh"
