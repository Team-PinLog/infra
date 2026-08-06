#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT=${PINLOG_FIREWALL_BACKUP_ROOT:-/var/backups/pinlog-host-firewall}
UFW_CONFIG_DIR=${PINLOG_FIREWALL_UFW_CONFIG_DIR:-/etc/ufw}
UFW_DEFAULTS=${PINLOG_FIREWALL_UFW_DEFAULTS:-/etc/default/ufw}
if [[ ${EUID} -ne 0 ]]; then
  # CI는 권한 없는 runner에서 fake ufw로 순서/backup 계약을 실행한다. test mode는
  # /tmp backup과 명시적 test log가 모두 있어야 하며 실제 ufw 권한을 우회하지 않는다.
  if [[ ${PINLOG_FIREWALL_TEST_MODE:-} != 1 ||
        ${BACKUP_ROOT} != /tmp/* ||
        ${UFW_CONFIG_DIR} != /tmp/* ||
        ${UFW_DEFAULTS} != /tmp/* ||
        -z ${UFW_TEST_LOG:-} ]]; then
    echo "root로 실행해야 합니다: sudo $0" >&2
    exit 1
  fi
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=${BACKUP_ROOT}/management-ports-${STAMP}
install -d -m 0700 "${BACKUP_DIR}"

cp -a "${UFW_CONFIG_DIR}/user.rules" "${UFW_CONFIG_DIR}/user6.rules" \
  "${UFW_DEFAULTS}" "${BACKUP_DIR}/"
iptables-save > "${BACKUP_DIR}/iptables.before"
ip6tables-save > "${BACKUP_DIR}/ip6tables.before"
ufw status verbose > "${BACKUP_DIR}/ufw-status.before"
ufw status numbered > "${BACKUP_DIR}/ufw-numbered.before"
systemctl show gerrit httpd pinlog-sentinel-receiver k3s tailscaled ufw \
  -p Id -p LoadState -p ActiveState -p SubState -p MainPID \
  > "${BACKUP_DIR}/services.before" 2>&1 || true

printf '#!/usr/bin/env bash\nset -euo pipefail\ninstall -m 0640 %q %q\ninstall -m 0640 %q %q\ninstall -m 0644 %q %q\nufw reload\nufw status verbose\n' \
  "${BACKUP_DIR}/user.rules" \
  "${UFW_CONFIG_DIR}/user.rules" \
  "${BACKUP_DIR}/user6.rules" \
  "${UFW_CONFIG_DIR}/user6.rules" \
  "${BACKUP_DIR}/ufw" \
  "${UFW_DEFAULTS}" \
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
declare -A cni_gerrit_comments=(
  [8988]='Restrict Gerrit backend to localhost/Tailscale'
  [8989]='Restrict Gerrit web to localhost/Tailscale'
  [29418]='Restrict Gerrit SSH to localhost/Tailscale'
)
cni_gerrit_ports=(8988 8989 29418)

# Parse every rule before mutating the live ruleset.
for rule in "${allow_rules[@]}"; do
  eval "ufw --dry-run ${rule}" >/dev/null
done
for port in "${ports[@]}"; do
  eval "ufw --dry-run deny in on enX0 to any port ${port} proto tcp comment '${deny_comments[$port]}'" >/dev/null
done
for port in "${cni_gerrit_ports[@]}"; do
  eval "ufw --dry-run insert 1 deny in on cni0 to any port ${port} proto tcp comment '${cni_gerrit_comments[$port]}'" >/dev/null
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

# 기존 broad cni0 allow보다 앞에 삽입해 Gerrit를 localhost/Tailscale로 제한한다.
# CNI forwarding과 실제 monitoring/control-plane consumer port는 그대로 보존한다.
for port in "${cni_gerrit_ports[@]}"; do
  eval "ufw insert 1 deny in on cni0 to any port ${port} proto tcp comment '${cni_gerrit_comments[$port]}'"
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
