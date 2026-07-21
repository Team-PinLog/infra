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


def validate_prometheus_contract(documents: list[dict], rules_output: Path) -> list[str]:
    errors: list[str] = []
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

    operator = next(
        (
            doc
            for doc in documents
            if doc.get("kind") == "Deployment"
            and doc.get("metadata", {}).get("name") == "kube-prometheus-stack-operator"
        ),
        None,
    )
    expected_reloader_args = {
        "--config-reloader-cpu-request=10m",
        "--config-reloader-cpu-limit=100m",
        "--config-reloader-memory-request=32Mi",
        "--config-reloader-memory-limit=128Mi",
    }
    if operator is None:
        errors.append("kube-prometheus-stack operator Deployment is missing")
    else:
        args = set(operator["spec"]["template"]["spec"]["containers"][0].get("args", []))
        missing = expected_reloader_args - args
        if missing:
            errors.append("operator config-reloader args missing: " + ",".join(sorted(missing)))

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
    if all_errors:
        for error in all_errors:
            print(error, file=sys.stderr)
        return 1

    print("rendered resource guardrails validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
