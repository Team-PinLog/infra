#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "root로 실행해야 합니다: sudo $0" >&2
  exit 1
fi

BACKUP_ROOT=${PINLOG_FIREWALL_BACKUP_ROOT:-/var/backups/pinlog-host-firewall}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=${BACKUP_ROOT}/management-ports-${STAMP}
install -d -m 0700 "${BACKUP_DIR}"

cp -a /etc/ufw/user.rules /etc/ufw/user6.rules /etc/default/ufw "${BACKUP_DIR}/"
iptables-save > "${BACKUP_DIR}/iptables.before"
ip6tables-save > "${BACKUP_DIR}/ip6tables.before"
ufw status verbose > "${BACKUP_DIR}/ufw-status.before"
ufw status numbered > "${BACKUP_DIR}/ufw-numbered.before"
systemctl show gerrit httpd pinlog-sentinel-receiver k3s tailscaled ufw \
  -p Id -p LoadState -p ActiveState -p SubState -p MainPID \
  > "${BACKUP_DIR}/services.before" 2>&1 || true

printf '#!/usr/bin/env bash\nset -euo pipefail\ninstall -m 0640 %q /etc/ufw/user.rules\ninstall -m 0640 %q /etc/ufw/user6.rules\ninstall -m 0644 %q /etc/default/ufw\nufw reload\nufw status verbose\n' \
  "${BACKUP_DIR}/user.rules" \
  "${BACKUP_DIR}/user6.rules" \
  "${BACKUP_DIR}/ufw" \
  > "${BACKUP_DIR}/rollback.sh"
chmod 0700 "${BACKUP_DIR}/rollback.sh"
bash -n "${BACKUP_DIR}/rollback.sh"

allow_rules=(
  "allow in on tailscale0 to any port 8989 proto tcp comment 'Gerrit web via Tailscale'"
  "allow in on cni0 from 10.42.0.0/16 to any port 9765 proto tcp comment 'Sentinel from k3s pods'"
  "allow in on cni0 from 10.42.0.0/16 to any port 9100 proto tcp comment 'node-exporter from Prometheus'"
  "allow in on cni0 from 10.42.0.0/16 to any port 10250 proto tcp comment 'kubelet from k3s pods'"
  "allow in on cni0 from 10.42.0.0/16 to any port 6443 proto tcp comment 'k3s API from pods'"
)

declare -A deny_comments=(
  [8988]='Block Gerrit backend on VPC/public'
  [8989]='Block Gerrit web on VPC/public'
  [29418]='Block Gerrit SSH on VPC/public'
  [9765]='Block Sentinel on VPC/public'
  [9100]='Block node-exporter on VPC/public'
  [10250]='Block kubelet on VPC/public'
  [6443]='Block k3s API on VPC/public'
)
ports=(8988 8989 29418 9765 9100 10250 6443)

# Parse every rule before mutating the live ruleset.
for rule in "${allow_rules[@]}"; do
  eval "ufw --dry-run ${rule}" >/dev/null
done
for port in "${ports[@]}"; do
  eval "ufw --dry-run deny in on enX0 to any port ${port} proto tcp comment '${deny_comments[$port]}'" >/dev/null
done

rollback_on_error() {
  local rc=$?
  echo "UFW 적용 실패(rc=${rc}); ${BACKUP_DIR}/rollback.sh 실행" >&2
  "${BACKUP_DIR}/rollback.sh" >&2
  exit "${rc}"
}
trap rollback_on_error ERR

# allow-before-deny: Tailscale 관리 경로와 실제 pod consumer를 먼저 보존한다.
for rule in "${allow_rules[@]}"; do
  eval "ufw ${rule}"
done

# 기존 public Gerrit web allow만 제거한다. 이미 없으면 idempotent하게 건너뛴다.
if ufw status numbered | grep -Eq '8989/tcp[[:space:]]+ALLOW IN[[:space:]]+Anywhere([[:space:]]|$)'; then
  ufw --force delete allow 8989/tcp
fi

# AWS/VPC/public 트래픽은 enX0로 들어오므로 관리 포트에 명시적 deny를 둔다.
# lo, tailscale0, cni0 및 CNI/ServiceLB forwarding 규칙은 변경하지 않는다.
for port in "${ports[@]}"; do
  eval "ufw deny in on enX0 to any port ${port} proto tcp comment '${deny_comments[$port]}'"
done
ufw reload
trap - ERR

ufw status numbered
printf 'backup=%s\nrollback=%s/rollback.sh\n' "${BACKUP_DIR}" "${BACKUP_DIR}"
