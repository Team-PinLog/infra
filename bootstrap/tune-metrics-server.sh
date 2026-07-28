#!/usr/bin/env bash
# Reconcile the packaged k3s metrics-server for the PinLog low-capacity profile.
# Keep safe latency flags for a future reactivation while enforcing scale zero.
set -euo pipefail

KUBECTL=${KUBECTL:-/usr/local/bin/kubectl}
PYTHON=${PYTHON:-/usr/bin/python3}
NAMESPACE=${NAMESPACE:-kube-system}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-20}
SLEEP_SECONDS=${SLEEP_SECONDS:-2}
VERIFY_ATTEMPTS=${VERIFY_ATTEMPTS:-6}
VERIFY_SLEEP_SECONDS=${VERIFY_SLEEP_SECONDS:-5}
ZERO_VERIFY_ATTEMPTS=${ZERO_VERIFY_ATTEMPTS:-30}
ZERO_VERIFY_SLEEP_SECONDS=${ZERO_VERIFY_SLEEP_SECONDS:-2}
DESIRED_REPLICAS=${DESIRED_REPLICAS:-0}

if [[ "$DESIRED_REPLICAS" != 0 && "$DESIRED_REPLICAS" != 1 ]]; then
  echo "DESIRED_REPLICAS must be 0 or 1" >&2
  exit 2
fi

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
desired_replicas = int(sys.argv[1])
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
original_replicas = deployment["spec"].get("replicas", 1)
if original == desired and original_replicas == desired_replicas:
    raise SystemExit(0)

args_path = f"/spec/template/spec/containers/{metrics_index}/args"
operations = [
    {
        "op": "test",
        "path": "/metadata/resourceVersion",
        "value": resource_version,
    }
]
if original != desired:
    if "args" in metrics:
        operations.append({"op": "test", "path": args_path, "value": original})
        operations.append({"op": "replace", "path": args_path, "value": desired})
    else:
        container_path = f"/spec/template/spec/containers/{metrics_index}"
        operations.append({"op": "test", "path": container_path, "value": metrics})
        operations.append({"op": "add", "path": args_path, "value": desired})
if original_replicas != desired_replicas:
    if "replicas" in deployment["spec"]:
        operations.append({"op": "test", "path": "/spec/replicas", "value": original_replicas})
        operations.append({"op": "replace", "path": "/spec/replicas", "value": desired_replicas})
    else:
        operations.append({"op": "add", "path": "/spec/replicas", "value": desired_replicas})
print(json.dumps(operations))
' "$DESIRED_REPLICAS"
  )

  if [[ -z "$patch" ]]; then
    break
  fi

  printf '%s' "$patch" >"$patch_file"
  if "$KUBECTL" -n "$NAMESPACE" patch deployment metrics-server \
    --type=json --patch-file "$patch_file" --request-timeout=5s; then
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
  if [[ "$DESIRED_REPLICAS" == 1 ]]; then
    "$KUBECTL" -n "$NAMESPACE" rollout status deployment/metrics-server \
      --timeout=120s
  fi
  echo "metrics-server tuning applied"
else
  echo "metrics-server tuning already applied"
fi

if [[ "$DESIRED_REPLICAS" == 0 ]]; then
  zero_ready=false
  for ((attempt = 1; attempt <= ZERO_VERIFY_ATTEMPTS; attempt++)); do
    deployment_json=$(
      "$KUBECTL" -n "$NAMESPACE" get deployment metrics-server \
        --request-timeout=5s -o json
    )
    pods_json=$(
      "$KUBECTL" -n "$NAMESPACE" get pods -l k8s-app=metrics-server \
        --request-timeout=5s -o json
    )
    if printf '%s\n%s' "$deployment_json" "$pods_json" | "$PYTHON" -c '
import json
import sys

decoder = json.JSONDecoder()
raw = sys.stdin.read()
deployment, offset = decoder.raw_decode(raw)
pods, _ = decoder.raw_decode(raw[offset:].lstrip())
status = deployment.get("status", {})
counters = ("replicas", "readyReplicas", "availableReplicas", "updatedReplicas")
if any(status.get(key, 0) != 0 for key in counters):
    raise SystemExit(1)
if pods.get("items"):
    raise SystemExit(1)
'; then
      zero_ready=true
      break
    fi
    sleep "$ZERO_VERIFY_SLEEP_SECONDS"
  done
  if [[ "$zero_ready" != true ]]; then
    echo "metrics-server scale-zero runtime verification timed out" >&2
    exit 1
  fi
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
desired_replicas = int(sys.argv[1])
containers = deployment["spec"]["template"]["spec"]["containers"]
metrics = next((item for item in containers if item.get("name") == "metrics-server"), None)
if metrics is None:
    raise SystemExit(1)
args = metrics.get("args", [])
resolution = [arg for arg in args if arg.startswith("--metric-resolution=")]
timeout = [arg for arg in args if arg.startswith("--kubelet-request-timeout=")]
if resolution != ["--metric-resolution=60s"] or timeout != ["--kubelet-request-timeout=30s"]:
    raise SystemExit(1)
if deployment["spec"].get("replicas", 1) != desired_replicas:
    raise SystemExit(1)
' "$DESIRED_REPLICAS"; then
    echo "metrics-server tuning drifted during stabilization" >&2
    exit 1
  fi
done

echo "metrics-server tuning stable"
