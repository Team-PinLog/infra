#!/usr/bin/env bash
# Keep the packaged k3s metrics-server usable with Docker/cri-dockerd when
# kubelet resource-stat collection exceeds the upstream 10-second timeout.
set -euo pipefail

KUBECTL=${KUBECTL:-/usr/local/bin/kubectl}
PYTHON=${PYTHON:-/usr/bin/python3}
NAMESPACE=${NAMESPACE:-kube-system}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-30}
SLEEP_SECONDS=${SLEEP_SECONDS:-2}
VERIFY_ATTEMPTS=${VERIFY_ATTEMPTS:-6}
VERIFY_SLEEP_SECONDS=${VERIFY_SLEEP_SECONDS:-5}

current=""
for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
  if current=$(
    "$KUBECTL" -n "$NAMESPACE" get deployment metrics-server \
      --request-timeout=5s -o json 2>/dev/null
  ); then
    break
  fi
  if ((attempt == MAX_ATTEMPTS)); then
    echo "metrics-server Deployment did not become available" >&2
    exit 1
  fi
  sleep "$SLEEP_SECONDS"
done

patch=$(
  printf '%s' "$current" | "$PYTHON" -c '
import json
import sys

deployment = json.load(sys.stdin)
containers = deployment["spec"]["template"]["spec"]["containers"]
metrics = next((item for item in containers if item.get("name") == "metrics-server"), None)
if metrics is None:
    raise SystemExit("metrics-server container is missing")

original = metrics.get("args", [])
kept = [
    arg
    for arg in original
    if not arg.startswith("--metric-resolution=")
    and not arg.startswith("--kubelet-request-timeout=")
]
desired = kept + ["--metric-resolution=60s", "--kubelet-request-timeout=30s"]
if original == desired:
    raise SystemExit(0)

print(json.dumps({
    "spec": {
        "template": {
            "spec": {
                "containers": [{"name": "metrics-server", "args": desired}]
            }
        }
    }
}))
'
)

if [[ -n "$patch" ]]; then
  patch_file=$(mktemp)
  trap 'rm -f "$patch_file"' EXIT
  printf '%s' "$patch" >"$patch_file"
  "$KUBECTL" -n "$NAMESPACE" patch deployment metrics-server \
    --type=strategic --patch-file "$patch_file"
  "$KUBECTL" -n "$NAMESPACE" rollout status deployment/metrics-server \
    --timeout=120s
  echo "metrics-server tuning applied"
else
  echo "metrics-server tuning already applied"
fi

for ((attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt++)); do
  sleep "$VERIFY_SLEEP_SECONDS"
  if ! verify_current=$(
    "$KUBECTL" -n "$NAMESPACE" get deployment metrics-server \
      --request-timeout=5s -o json 2>/dev/null
  ); then
    echo "metrics-server tuning verification could not read Deployment" >&2
    exit 1
  fi
  if ! printf '%s' "$verify_current" | "$PYTHON" -c '
import json
import sys

deployment = json.load(sys.stdin)
containers = deployment["spec"]["template"]["spec"]["containers"]
metrics = next((item for item in containers if item.get("name") == "metrics-server"), None)
if metrics is None:
    raise SystemExit(1)
args = metrics.get("args", [])
resolution = [arg for arg in args if arg.startswith("--metric-resolution=")]
timeout = [arg for arg in args if arg.startswith("--kubelet-request-timeout=")]
if resolution != ["--metric-resolution=60s"] or timeout != ["--kubelet-request-timeout=30s"]:
    raise SystemExit(1)
'; then
    echo "metrics-server tuning drifted during stabilization" >&2
    exit 1
  fi
done

echo "metrics-server tuning stable"
