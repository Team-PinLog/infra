#!/usr/bin/env bash
# Keep the packaged k3s metrics-server usable with Docker/cri-dockerd when
# kubelet resource-stat collection exceeds the upstream 10-second timeout.
set -euo pipefail

KUBECTL=${KUBECTL:-/usr/local/bin/kubectl}
PYTHON=${PYTHON:-/usr/bin/python3}
NAMESPACE=${NAMESPACE:-kube-system}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-20}
SLEEP_SECONDS=${SLEEP_SECONDS:-2}
VERIFY_ATTEMPTS=${VERIFY_ATTEMPTS:-6}
VERIFY_SLEEP_SECONDS=${VERIFY_SLEEP_SECONDS:-5}

patch_file=$(mktemp)
trap 'rm -f "$patch_file"' EXIT
applied=false

for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
  if ! current=$(
    "$KUBECTL" -n "$NAMESPACE" get deployment metrics-server \
      --request-timeout=5s -o json 2>/dev/null
  ); then
    if ((attempt == MAX_ATTEMPTS)); then
      echo "metrics-server Deployment did not become available" >&2
      exit 1
    fi
    sleep "$SLEEP_SECONDS"
    continue
  fi

  patch=$(
    printf '%s' "$current" | "$PYTHON" -c '
import json
import sys

deployment = json.load(sys.stdin)
resource_version = deployment["metadata"]["resourceVersion"]
containers = deployment["spec"]["template"]["spec"]["containers"]
metrics_index = next(
    (index for index, item in enumerate(containers) if item.get("name") == "metrics-server"),
    None,
)
if metrics_index is None:
    raise SystemExit("metrics-server container is missing")

metrics = containers[metrics_index]
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

args_path = f"/spec/template/spec/containers/{metrics_index}/args"
operations = [
    {
        "op": "test",
        "path": "/metadata/resourceVersion",
        "value": resource_version,
    }
]
if "args" in metrics:
    operations.append({"op": "test", "path": args_path, "value": original})
    operations.append({"op": "replace", "path": args_path, "value": desired})
else:
    container_path = f"/spec/template/spec/containers/{metrics_index}"
    operations.append({"op": "test", "path": container_path, "value": metrics})
    operations.append({"op": "add", "path": args_path, "value": desired})
print(json.dumps(operations))
'
  )

  if [[ -z "$patch" ]]; then
    break
  fi

  printf '%s' "$patch" >"$patch_file"
  if "$KUBECTL" -n "$NAMESPACE" patch deployment metrics-server \
    --type=json --patch-file "$patch_file"; then
    applied=true
    break
  fi

  if ((attempt == MAX_ATTEMPTS)); then
    echo "metrics-server tuning patch did not converge" >&2
    exit 1
  fi
  echo "metrics-server changed concurrently; retrying" >&2
  sleep "$SLEEP_SECONDS"
done

if [[ "$applied" == true ]]; then
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
