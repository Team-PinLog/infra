#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "root로 실행해야 합니다: sudo $0" >&2
  exit 1
fi

script_dir=$(cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)
install -m 0755 "$script_dir/tune-metrics-server.sh" \
  /usr/local/sbin/pinlog-tune-metrics-server.sh
install -m 0644 "$script_dir/pinlog-metrics-server-tuning.service" \
  /etc/systemd/system/pinlog-metrics-server-tuning.service
install -m 0644 "$script_dir/pinlog-metrics-server-tuning.timer" \
  /etc/systemd/system/pinlog-metrics-server-tuning.timer

systemctl daemon-reload
systemctl enable pinlog-metrics-server-tuning.service
systemctl enable --now pinlog-metrics-server-tuning.timer
systemctl restart pinlog-metrics-server-tuning.service
systemctl is-active --quiet pinlog-metrics-server-tuning.timer
[[ $(systemctl show pinlog-metrics-server-tuning.service -p Result --value) == success ]]

echo "metrics-server tuning service installed"
