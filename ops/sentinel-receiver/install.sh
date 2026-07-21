#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

SOURCE_DIR=$(cd "$(dirname "$0")" && pwd)
PROFILE_SOURCE=/root/.hermes/profiles/pinlog-alerts
PROFILE_TARGET=/var/lib/pinlog-sentinel/hermes

if [[ ! -d ${PROFILE_SOURCE} ]]; then
  echo "pinlog-alerts profile is missing" >&2
  exit 1
fi

if ! getent passwd pinlog-sentinel >/dev/null; then
  useradd --system --user-group --home-dir /var/lib/pinlog-sentinel --shell /usr/sbin/nologin pinlog-sentinel
fi

install -d -m 0755 /opt/pinlog-sentinel-receiver
install -d -m 0700 /etc/pinlog-sentinel
install -d -o pinlog-sentinel -g pinlog-sentinel -m 0700 /var/lib/pinlog-sentinel
install -m 0755 "${SOURCE_DIR}/receiver.py" /opt/pinlog-sentinel-receiver/receiver.py
install -m 0644 "${SOURCE_DIR}/pinlog-sentinel-receiver.service" /etc/systemd/system/pinlog-sentinel-receiver.service

if [[ ! -f /etc/pinlog-sentinel/receiver.env ]]; then
  umask 077
  /usr/local/lib/hermes-agent/venv/bin/python3 -c 'import secrets; print("PINLOG_SENTINEL_TOKEN=" + secrets.token_urlsafe(48))' > /etc/pinlog-sentinel/receiver.env
fi
chmod 0600 /etc/pinlog-sentinel/receiver.env

umask 077
kubectl -n monitoring get secret mattermost-alert-webhook -o jsonpath='{.data.url}' \
  | base64 --decode > /etc/pinlog-sentinel/mattermost_url
/usr/local/lib/hermes-agent/venv/bin/python3 -c 'from pathlib import Path; u=Path("/etc/pinlog-sentinel/mattermost_url").read_text().strip(); assert u.startswith("https://") and "/hooks/" in u'
chmod 0600 /etc/pinlog-sentinel/mattermost_url

if [[ ! -s /etc/pinlog-sentinel/tls.key || ! -s /etc/pinlog-sentinel/tls.crt ]]; then
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
    -subj '/CN=pinlog-sentinel-receiver.monitoring.svc.cluster.local' \
    -addext 'subjectAltName=DNS:pinlog-sentinel-receiver.monitoring.svc.cluster.local' \
    -keyout /etc/pinlog-sentinel/tls.key \
    -out /etc/pinlog-sentinel/tls.crt >/dev/null 2>&1
fi
chmod 0600 /etc/pinlog-sentinel/tls.key
chmod 0644 /etc/pinlog-sentinel/tls.crt

systemctl stop pinlog-sentinel-receiver.service 2>/dev/null || true
rm -rf "${PROFILE_TARGET}.new"
cp -aL "${PROFILE_SOURCE}" "${PROFILE_TARGET}.new"
rm -rf "${PROFILE_TARGET}.new/logs" "${PROFILE_TARGET}.new/cache"
rm -f "${PROFILE_TARGET}.new/state.db" "${PROFILE_TARGET}.new/auth.lock"
install -d -m 0700 "${PROFILE_TARGET}.new/logs" "${PROFILE_TARGET}.new/cache"
chown -R pinlog-sentinel:pinlog-sentinel "${PROFILE_TARGET}.new"
rm -rf "${PROFILE_TARGET}"
mv "${PROFILE_TARGET}.new" "${PROFILE_TARGET}"
chown -R pinlog-sentinel:pinlog-sentinel /var/lib/pinlog-sentinel

systemctl daemon-reload
systemctl enable pinlog-sentinel-receiver.service
systemctl restart pinlog-sentinel-receiver.service
