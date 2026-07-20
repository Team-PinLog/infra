#!/usr/bin/env bash
#
# sync-tls-secret.sh — 호스트의 Let's Encrypt 인증서를 k8s Secret으로 동기화
#
# 멱등이므로 systemd 타이머로 매일 돌려도 안전하다.
# SSAFY가 호스트에서 인증서를 갱신하면 24시간 내 클러스터가 자동으로 집어간다.
#
# 중요: cert.pem이 아니라 fullchain.pem을 쓴다.
#       cert.pem은 리프 인증서만 있어서 중간 인증서가 빠지고,
#       일부 클라이언트가 체인 검증에 실패한다.
#
set -euo pipefail

CERT_DIR="/etc/letsencrypt/live/p.ssafy.io"
SECRET_NAME="pinlog-wildcard-tls"
SECRET_NS="kube-system"

KUBECTL="${KUBECTL:-k3s kubectl}"

if [[ ! -r "${CERT_DIR}/fullchain.pem" ]]; then
  echo "인증서를 읽을 수 없습니다: ${CERT_DIR}/fullchain.pem" >&2
  echo "(/etc/letsencrypt/live 는 0700이라 root로 실행해야 합니다)" >&2
  exit 1
fi

# 만료일 확인 후 경고
EXPIRY=$(openssl x509 -in "${CERT_DIR}/fullchain.pem" -noout -enddate | cut -d= -f2)
EXPIRY_EPOCH=$(date -d "${EXPIRY}" +%s)
NOW_EPOCH=$(date +%s)
DAYS_LEFT=$(( (EXPIRY_EPOCH - NOW_EPOCH) / 86400 ))

echo "인증서 만료: ${EXPIRY} (${DAYS_LEFT}일 남음)"

if (( DAYS_LEFT < 30 )); then
  echo "" >&2
  echo "  ***  경고: 인증서 만료가 ${DAYS_LEFT}일 남았습니다  ***" >&2
  echo "  이 인증서는 수동 DNS-01로 발급되어 팀이 갱신할 수 없습니다." >&2
  echo "  SSAFY 담당자에게 갱신 일정을 확인하세요." >&2
  echo "" >&2
fi

${KUBECTL} -n "${SECRET_NS}" create secret tls "${SECRET_NAME}" \
  --cert="${CERT_DIR}/fullchain.pem" \
  --key="${CERT_DIR}/privkey.pem" \
  --dry-run=client -o yaml | ${KUBECTL} apply -f -

echo "Secret ${SECRET_NS}/${SECRET_NAME} 동기화 완료"
