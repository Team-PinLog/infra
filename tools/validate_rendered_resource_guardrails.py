#!/usr/bin/env python3
"""Validate resource guardrails in exact pinned monitoring Helm renders."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

import yaml


REQUIRED_RESOURCE_KEYS = {"cpu", "memory"}
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
EXPECTED_ALERTS = {
    "PinLogProdQuotaHigh",
    "PinLogProdContainerOOMKilled",
    "PinLogProdPodUnschedulable",
}
EXPECTED_CONFIG_RELOADER_ARGS = {
    "--config-reloader-cpu-request=5m",
    "--config-reloader-cpu-limit=50m",
    "--config-reloader-memory-request=32Mi",
    "--config-reloader-memory-limit=96Mi",
}
EXPECTED_PROMETHEUS_WORKLOADS = {
    ("DaemonSet", "kube-prometheus-stack-prometheus-node-exporter"): (None, {"node-exporter": ({"cpu": "10m", "memory": "24Mi"}, {"cpu": "75m", "memory": "64Mi"})}),
    ("Deployment", "kube-prometheus-stack-grafana"): (0, {
        "grafana": ({"cpu": "25m", "memory": "128Mi"}, {"cpu": "150m", "memory": "256Mi"}),
        "grafana-sc-dashboard": ({"cpu": "10m", "memory": "64Mi"}, {"cpu": "50m", "memory": "128Mi"}),
        "grafana-sc-datasources": ({"cpu": "10m", "memory": "64Mi"}, {"cpu": "50m", "memory": "128Mi"}),
        "init-chown-data": ({"cpu": "10m", "memory": "32Mi"}, {"cpu": "50m", "memory": "128Mi"}),
    }),
    ("Deployment", "kube-prometheus-stack-kube-state-metrics"): (1, {"kube-state-metrics": ({"cpu": "10m", "memory": "48Mi"}, {"cpu": "75m", "memory": "128Mi"})}),
    ("Deployment", "kube-prometheus-stack-operator"): (1, {"kube-prometheus-stack": ({"cpu": "25m", "memory": "64Mi"}, {"cpu": "100m", "memory": "192Mi"})}),
    ("Job", "kube-prometheus-stack-admission-create"): (None, {"create": ({"cpu": "10m", "memory": "32Mi"}, {"cpu": "100m", "memory": "128Mi"})}),
    ("Job", "kube-prometheus-stack-admission-patch"): (None, {"patch": ({"cpu": "10m", "memory": "32Mi"}, {"cpu": "100m", "memory": "128Mi"})}),
}
CPU_QUANTITY = re.compile(
    r"^(?:[1-9][0-9]*m|[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$"
)
MEMORY_QUANTITY = re.compile(
    r"^[1-9][0-9]*(?:Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$"
)


def _documents(path: Path) -> list[dict]:
    documents = [document for document in yaml.safe_load_all(path.read_text()) if document]
    if not documents:
        raise ValueError(f"{path}: rendered output is empty")
    return documents


def validate_unique_object_identities(documents: list[dict], context: str) -> list[str]:
    identities = [
        (
            document.get("apiVersion"),
            document.get("kind"),
            document.get("metadata", {}).get("namespace"),
            document.get("metadata", {}).get("name"),
        )
        for document in documents
    ]
    duplicates = sorted(
        {identity for identity in identities if identities.count(identity) > 1},
        key=repr,
    )
    return [f"duplicate {context} object identities: {duplicates}"] if duplicates else []


def _pod_spec(document: dict) -> dict:
    kind = document["kind"]
    if kind == "CronJob":
        return document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    if kind == "Job":
        return document["spec"]["template"]["spec"]
    return document["spec"]["template"]["spec"]


def _validate_resources(owner: str, container: dict) -> list[str]:
    errors: list[str] = []
    resources = container.get("resources") or {}
    for category in ("requests", "limits"):
        actual = set((resources.get(category) or {}).keys())
        missing = REQUIRED_RESOURCE_KEYS - actual
        if missing:
            errors.append(
                f"{owner}/{container.get('name', '<unnamed>')}: "
                f"missing {category} {','.join(sorted(missing))}"
            )
        for resource in REQUIRED_RESOURCE_KEYS & actual:
            value = str(resources[category][resource])
            pattern = CPU_QUANTITY if resource == "cpu" else MEMORY_QUANTITY
            if not pattern.fullmatch(value):
                errors.append(
                    f"{owner}/{container.get('name', '<unnamed>')}: "
                    f"invalid or non-positive {category}.{resource}={value!r}"
                )
    return errors


def validate_render(path: Path) -> tuple[list[dict], list[str]]:
    documents = _documents(path)
    errors: list[str] = []
    workload_count = 0
    for document in documents:
        if document.get("kind") not in WORKLOAD_KINDS:
            continue
        workload_count += 1
        owner = f"{document['kind']}/{document['metadata']['name']}"
        pod_spec = _pod_spec(document)
        containers = list(pod_spec.get("initContainers", [])) + list(
            pod_spec.get("containers", [])
        )
        if not containers:
            errors.append(f"{owner}: no containers rendered")
        for container in containers:
            errors.extend(_validate_resources(owner, container))
    if workload_count == 0:
        errors.append(f"{path}: no workload objects rendered")
    return documents, errors


def validate_low_capacity_render_contract(documents: list[dict]) -> list[str]:
    """Fail closed when pinned chart output escapes the low-capacity profile."""
    errors: list[str] = []

    prometheus = next(
        (
            document
            for document in documents
            if document.get("kind") == "Prometheus"
            and document.get("metadata", {}).get("name")
            == "kube-prometheus-stack-prometheus"
        ),
        None,
    )
    expected_prometheus = {
        "scrapeInterval": "60s",
        "scrapeTimeout": "15s",
        "evaluationInterval": "60s",
        "retention": "3d",
        "retentionSize": "4GB",
    }
    if prometheus is None:
        errors.append("low-capacity Prometheus CR is missing")
    else:
        actual = {
            key: prometheus.get("spec", {}).get(key) for key in expected_prometheus
        }
        if actual != expected_prometheus:
            errors.append(
                f"low-capacity Prometheus settings mismatch: expected "
                f"{expected_prometheus}, got {actual}"
            )

    kubelet = next(
        (
            document
            for document in documents
            if document.get("kind") == "ServiceMonitor"
            and document.get("metadata", {}).get("name")
            == "kube-prometheus-stack-kubelet"
        ),
        None,
    )
    expected_endpoints = [
        {"path": "/metrics", "interval": "120s", "scrapeTimeout": "30s"}
    ]
    if kubelet is None:
        errors.append("low-capacity kubelet ServiceMonitor is missing")
    else:
        actual_endpoints = [
            {
                "path": endpoint.get("path", "/metrics"),
                "interval": endpoint.get("interval"),
                "scrapeTimeout": endpoint.get("scrapeTimeout"),
            }
            for endpoint in kubelet.get("spec", {}).get("endpoints", [])
        ]
        if actual_endpoints != expected_endpoints:
            errors.append(
                f"low-capacity kubelet endpoints mismatch: expected "
                f"{expected_endpoints}, got {actual_endpoints}"
            )

    grafana = next(
        (
            document
            for document in documents
            if document.get("kind") == "Deployment"
            and document.get("metadata", {}).get("name")
            == "kube-prometheus-stack-grafana"
        ),
        None,
    )
    if grafana is None:
        errors.append("low-capacity Grafana Deployment is missing")
    elif grafana.get("spec", {}).get("replicas") != 0:
        errors.append(
            "low-capacity Grafana replicas must be 0, got "
            f"{grafana.get('spec', {}).get('replicas')!r}"
        )

    return errors


def _rendered_container_resources(workload: dict) -> dict[str, dict]:
    pod_spec = _pod_spec(workload)
    return {
        container["name"]: container.get("resources") or {}
        for container in list(pod_spec.get("initContainers", []))
        + list(pod_spec.get("containers", []))
    }


def validate_prometheus_workload_contract(documents: list[dict]) -> list[str]:
    identities = [
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in documents
        if document.get("kind") in WORKLOAD_KINDS
    ]
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        return [f"duplicate Prometheus workload identities: {duplicates}"]
    workloads = {
        (document.get("kind"), document.get("metadata", {}).get("name")): document
        for document in documents
        if document.get("kind") in WORKLOAD_KINDS
    }
    if set(workloads) != set(EXPECTED_PROMETHEUS_WORKLOADS):
        return [
            "Prometheus workload topology mismatch: expected "
            f"{sorted(EXPECTED_PROMETHEUS_WORKLOADS)}, got {sorted(workloads)}"
        ]
    errors: list[str] = []
    for key, (expected_replicas, expected_compact) in EXPECTED_PROMETHEUS_WORKLOADS.items():
        workload = workloads[key]
        actual_replicas = workload.get("spec", {}).get("replicas")
        if actual_replicas != expected_replicas:
            errors.append(
                f"{key[0]}/{key[1]} replicas mismatch: expected {expected_replicas}, "
                f"got {actual_replicas}"
            )
        expected_resources = {
            name: {"requests": requests, "limits": limits}
            for name, (requests, limits) in expected_compact.items()
        }
        actual_resources = _rendered_container_resources(workload)
        if actual_resources != expected_resources:
            errors.append(
                f"{key[0]}/{key[1]} resources mismatch: expected "
                f"{expected_resources}, got {actual_resources}"
            )
    return errors


def _strip_hcl_comments(config: str) -> str:
    output: list[str] = []
    index = 0
    delimiter: str | None = None
    escaped = False
    while index < len(config):
        char = config[index]
        next_char = config[index + 1] if index + 1 < len(config) else ""
        if delimiter is not None:
            output.append(char)
            if delimiter == '"' and escaped:
                escaped = False
            elif delimiter == '"' and char == "\\":
                escaped = True
            elif char == delimiter:
                delimiter = None
            index += 1
            continue
        if char in {'"', '`'}:
            delimiter = char
            output.append(char)
            index += 1
            continue
        if char == "#" or (char == "/" and next_char == "/"):
            while index < len(config) and config[index] != "\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            end = config.find("*/", index + 2)
            if end == -1:
                return ""
            index = end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def validate_hcl_lexical_structure(config: str) -> list[str]:
    depth = 0
    delimiter: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(config):
        char = config[index]
        next_char = config[index + 1] if index + 1 < len(config) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if delimiter is not None:
            if delimiter == '"' and escaped:
                escaped = False
            elif delimiter == '"' and char == "\\":
                escaped = True
            elif char == delimiter:
                delimiter = None
            index += 1
            continue
        if char in {'"', '`'}:
            delimiter = char
        elif char == "#" or (char == "/" and next_char == "/"):
            line_comment = True
            index += 2 if char == "/" else 1
            continue
        elif char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        elif char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return ["Alloy config has an unexpected closing brace"]
            depth -= 1
        index += 1
    errors: list[str] = []
    if delimiter is not None:
        errors.append(f"Alloy config has an unterminated {delimiter} string")
    if block_comment:
        errors.append("Alloy config has an unterminated block comment")
    if depth != 0:
        errors.append(f"Alloy config has {depth} unmatched opening brace(s)")
    return errors


def _hcl_brace_depths(text: str) -> list[int]:
    depths: list[int] = []
    depth = 0
    delimiter: str | None = None
    escaped = False
    for char in text:
        depths.append(-1 if delimiter is not None else depth)
        if delimiter is not None:
            if delimiter == '"' and escaped:
                escaped = False
            elif delimiter == '"' and char == "\\":
                escaped = True
            elif char == delimiter:
                delimiter = None
        elif char in {'"', '`'}:
            delimiter = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
    return depths


def _balanced_hcl_body(text: str, opening: int) -> str | None:
    depth = 0
    delimiter: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if delimiter is not None:
            if delimiter == '"' and escaped:
                escaped = False
            elif delimiter == '"' and char == "\\":
                escaped = True
            elif char == delimiter:
                delimiter = None
            continue
        if char in {'"', '`'}:
            delimiter = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
    return None


def _hcl_component_blocks(config: str, component: str, label: str) -> list[str]:
    clean = _strip_hcl_comments(config)
    depths = _hcl_brace_depths(clean)
    pattern = re.compile(
        rf'(?m)^\s*{re.escape(component)}\s+"{re.escape(label)}"\s*\{{'
    )
    blocks: list[str] = []
    for match in pattern.finditer(clean):
        if depths[match.start()] != 0:
            continue
        opening = clean.find("{", match.start())
        body = _balanced_hcl_body(clean, opening)
        if body is not None:
            blocks.append(body)
    return blocks


def _direct_hcl_text(block: str) -> str:
    """Preserve only depth-zero HCL text while retaining original offsets."""
    output: list[str] = []
    depth = 0
    delimiter: str | None = None
    escaped = False
    for char in block:
        if delimiter is not None:
            output.append(char if depth == 0 else ("\n" if char == "\n" else " "))
            if delimiter == '"' and escaped:
                escaped = False
            elif delimiter == '"' and char == "\\":
                escaped = True
            elif char == delimiter:
                delimiter = None
            continue
        if char in {'"', '`'}:
            delimiter = char
            output.append(char if depth == 0 else " ")
        elif char == "{":
            output.append(char if depth == 0 else " ")
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
            output.append(char if depth == 0 else " ")
        else:
            output.append(char if depth == 0 else ("\n" if char == "\n" else " "))
    return "".join(output)


def _direct_named_hcl_blocks(block: str, name: str) -> list[str]:
    direct = _direct_hcl_text(block)
    depths = _hcl_brace_depths(block)
    pattern = re.compile(rf"(?m)^\s*{re.escape(name)}\s*\{{")
    blocks: list[str] = []
    for match in pattern.finditer(direct):
        if depths[match.start()] != 0:
            continue
        opening = block.find("{", match.start())
        body = _balanced_hcl_body(block, opening)
        if body is not None:
            blocks.append(body)
    return blocks


def _direct_hcl_assignments(block: str, name: str) -> list[str]:
    direct = _direct_hcl_text(block)
    depths = _hcl_brace_depths(block)
    pattern = re.compile(rf"(?m)^\s*{re.escape(name)}\s*=\s*([^\n]+?)\s*$")
    return [
        match.group(1)
        for match in pattern.finditer(direct)
        if depths[match.start()] == 0
    ]


def validate_loki_contract(documents: list[dict]) -> list[str]:
    """Reject scalable/cache topology or ignored low-capacity Loki values."""
    errors: list[str] = []
    identity_errors = validate_unique_object_identities(documents, "Loki")
    if identity_errors:
        return identity_errors
    workloads = [
        document for document in documents if document.get("kind") in WORKLOAD_KINDS
    ]
    identities = [
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in workloads
    ]
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        errors.append(f"duplicate Loki workload identities: {duplicates}")
        return errors
    actual_topology = {
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in workloads
    }
    expected_topology = {("StatefulSet", "loki")}
    if actual_topology != expected_topology:
        errors.append(
            f"low-capacity Loki topology mismatch: expected {sorted(expected_topology)}, "
            f"got {sorted(actual_topology)}"
        )
        return errors

    loki = workloads[0]
    if loki.get("spec", {}).get("replicas") != 1:
        errors.append("low-capacity Loki StatefulSet replicas must be 1")
    expected_resources = {
        "loki": {
            "requests": {"cpu": "50m", "memory": "256Mi"},
            "limits": {"cpu": "250m", "memory": "768Mi"},
        },
        "loki-sc-rules": {
            "requests": {"cpu": "5m", "memory": "48Mi"},
            "limits": {"cpu": "50m", "memory": "96Mi"},
        },
    }
    actual_resources = _rendered_container_resources(loki)
    if actual_resources != expected_resources:
        errors.append(
            f"low-capacity Loki container resources mismatch: expected "
            f"{expected_resources}, got {actual_resources}"
        )

    config = next(
        (
            document.get("data", {}).get("config.yaml")
            for document in documents
            if document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name") == "loki"
        ),
        None,
    )
    expected_limits = {
        "retention_period": "72h",
        "ingestion_rate_mb": 2,
        "ingestion_burst_size_mb": 4,
    }
    try:
        parsed_config = yaml.safe_load(config) if isinstance(config, str) else None
    except yaml.YAMLError as error:
        errors.append(f"low-capacity Loki config is invalid YAML: {error}")
        parsed_config = None
    limits = parsed_config.get("limits_config") if isinstance(parsed_config, dict) else None
    actual_limits = (
        {key: limits.get(key) for key in expected_limits}
        if isinstance(limits, dict)
        else None
    )
    if actual_limits != expected_limits:
        errors.append(
            f"low-capacity Loki effective limits mismatch: expected {expected_limits}, "
            f"got {actual_limits}"
        )
    return errors


def validate_alloy_contract(documents: list[dict]) -> list[str]:
    """Reject non-DaemonSet Alloy topology or ignored resource/config values."""
    errors: list[str] = []
    identity_errors = validate_unique_object_identities(documents, "Alloy")
    if identity_errors:
        return identity_errors
    workloads = [
        document for document in documents if document.get("kind") in WORKLOAD_KINDS
    ]
    identities = [
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in workloads
    ]
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        errors.append(f"duplicate Alloy workload identities: {duplicates}")
        return errors
    actual_topology = {
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in workloads
    }
    expected_topology = {("DaemonSet", "alloy")}
    if actual_topology != expected_topology:
        errors.append(
            f"low-capacity Alloy topology mismatch: expected {sorted(expected_topology)}, "
            f"got {sorted(actual_topology)}"
        )
        return errors

    expected_resources = {
        "alloy": {
            "requests": {"cpu": "25m", "memory": "96Mi"},
            "limits": {"cpu": "150m", "memory": "192Mi"},
        },
        "config-reloader": {
            "requests": {"cpu": "5m", "memory": "32Mi"},
            "limits": {"cpu": "50m", "memory": "64Mi"},
        },
    }
    actual_resources = _rendered_container_resources(workloads[0])
    if actual_resources != expected_resources:
        errors.append(
            f"low-capacity Alloy container resources mismatch: expected "
            f"{expected_resources}, got {actual_resources}"
        )

    config = next(
        (
            document.get("data", {}).get("config.alloy")
            for document in documents
            if document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name") == "alloy"
        ),
        None,
    )
    if not isinstance(config, str):
        errors.append("low-capacity Alloy config is missing")
        return errors
    lexical_errors = validate_hcl_lexical_structure(config)
    if lexical_errors:
        return lexical_errors
    source_blocks = _hcl_component_blocks(config, "loki.source.kubernetes", "pods")
    if len(source_blocks) != 1:
        errors.append(
            f"expected exactly one active loki.source.kubernetes pods block, got {len(source_blocks)}"
        )
    else:
        targets = _direct_hcl_assignments(source_blocks[0], "targets")
        forwards = _direct_hcl_assignments(source_blocks[0], "forward_to")
        if targets != ["discovery.relabel.pod_logs.output"]:
            errors.append(f"Alloy pods source targets mismatch: {targets}")
        if forwards != ["[loki.process.drop_noise.receiver]"]:
            errors.append(f"Alloy pods source forward_to mismatch: {forwards}")

    write_blocks = _hcl_component_blocks(config, "loki.write", "default")
    if len(write_blocks) != 1:
        errors.append(f"expected exactly one active loki.write default block, got {len(write_blocks)}")
    else:
        endpoint_blocks = _direct_named_hcl_blocks(write_blocks[0], "endpoint")
        urls = _direct_hcl_assignments(endpoint_blocks[0], "url") if len(endpoint_blocks) == 1 else []
        expected_url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"
        if len(endpoint_blocks) != 1:
            errors.append(
                f"expected exactly one direct Alloy endpoint block, got {len(endpoint_blocks)}"
            )
        if urls != [f'"{expected_url}"']:
            errors.append(f"Alloy Loki write endpoint mismatch: {urls}")
    return errors


def validate_config_reloader_args(args: list[str]) -> list[str]:
    prefixes = tuple(argument.split("=", 1)[0] + "=" for argument in EXPECTED_CONFIG_RELOADER_ARGS)
    relevant = [argument for argument in args if argument.startswith(prefixes)]
    if len(relevant) != len(EXPECTED_CONFIG_RELOADER_ARGS) or set(relevant) != EXPECTED_CONFIG_RELOADER_ARGS:
        return [
            "operator config-reloader args mismatch: expected exactly "
            f"{sorted(EXPECTED_CONFIG_RELOADER_ARGS)}, got {sorted(relevant)}"
        ]
    return []


def validate_prometheus_contract(documents: list[dict], rules_output: Path) -> list[str]:
    errors = validate_unique_object_identities(documents, "Prometheus")
    if errors:
        return errors
    errors = validate_low_capacity_render_contract(documents)
    errors.extend(validate_prometheus_workload_contract(documents))
    expected_cr_resources = {
        "Prometheus": {"requests": {"cpu": "100m", "memory": "768Mi"}, "limits": {"cpu": "400m", "memory": "1536Mi"}},
        "Alertmanager": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"cpu": "100m", "memory": "192Mi"}},
    }
    for kind in ("Prometheus", "Alertmanager"):
        custom_resources = [doc for doc in documents if doc.get("kind") == kind]
        if len(custom_resources) != 1:
            errors.append(f"expected exactly one {kind} CR, found {len(custom_resources)}")
            continue
        errors.extend(
            _validate_resources(
                f"{kind}/{custom_resources[0]['metadata']['name']}",
                {"name": "main", "resources": custom_resources[0]["spec"].get("resources")},
            )
        )
        actual_resources = custom_resources[0]["spec"].get("resources")
        if actual_resources != expected_cr_resources[kind]:
            errors.append(
                f"{kind} resources mismatch: expected {expected_cr_resources[kind]}, "
                f"got {actual_resources}"
            )

    operator = next(
        (
            doc
            for doc in documents
            if doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("name") == "kube-prometheus-stack-operator"
        ),
        None,
    )
    if operator is None:
        errors.append("kube-prometheus-stack operator Deployment is missing")
    else:
        args = operator["spec"]["template"]["spec"]["containers"][0].get("args", [])
        errors.extend(validate_config_reloader_args(args))

    grafana = next(
        (
            doc
            for doc in documents
            if doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("name") == "kube-prometheus-stack-grafana"
        ),
        None,
    )
    if grafana is None:
        errors.append("Grafana Deployment is missing")
    else:
        init_names = {
            item["name"]
            for item in grafana["spec"]["template"]["spec"].get("initContainers", [])
        }
        if "init-chown-data" not in init_names:
            errors.append("Grafana init-chown-data was not rendered")

    resource_rule = next(
        (
            doc
            for doc in documents
            if doc.get("kind") == "PrometheusRule"
            and doc.get("metadata", {}).get("name")
            == "kube-prometheus-stack-pinlog-resource-guardrails"
        ),
        None,
    )
    if resource_rule is None:
        errors.append("pinlog resource PrometheusRule is missing")
    else:
        rules = [
            rule
            for group in resource_rule["spec"]["groups"]
            for rule in group.get("rules", [])
            if "alert" in rule
        ]
        actual_alerts = {rule["alert"] for rule in rules}
        if actual_alerts != EXPECTED_ALERTS:
            errors.append(
                f"resource alerts mismatch: expected {sorted(EXPECTED_ALERTS)}, "
                f"got {sorted(actual_alerts)}"
            )
        for rule in rules:
            if rule.get("labels", {}).get("severity") != "warning":
                errors.append(f"{rule['alert']}: severity must be warning")
        rules_output.write_text(
            yaml.safe_dump(
                {"groups": resource_rule["spec"]["groups"]},
                sort_keys=False,
            )
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus", type=Path, required=True)
    parser.add_argument("--loki", type=Path, required=True)
    parser.add_argument("--alloy", type=Path, required=True)
    parser.add_argument("--rules-output", type=Path, required=True)
    args = parser.parse_args()

    all_errors: list[str] = []
    rendered: dict[str, list[dict]] = {}
    for name in ("prometheus", "loki", "alloy"):
        path = getattr(args, name)
        documents, errors = validate_render(path)
        rendered[name] = documents
        all_errors.extend(f"{name}: {error}" for error in errors)

    all_errors.extend(
        f"prometheus: {error}"
        for error in validate_prometheus_contract(
            rendered["prometheus"], args.rules_output
        )
    )
    all_errors.extend(
        f"loki: {error}" for error in validate_loki_contract(rendered["loki"])
    )
    all_errors.extend(
        f"alloy: {error}" for error in validate_alloy_contract(rendered["alloy"])
    )
    if all_errors:
        for error in all_errors:
            print(error, file=sys.stderr)
        return 1

    print("rendered resource guardrails validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
