#!/usr/bin/env python3
"""Fail closed when rollback lost or unbound a pre-phase monitoring PVC."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_inventory(before: object, current: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(before, list):
        return ["pre-phase inventory must be a JSON array"]
    if not isinstance(current, dict) or not isinstance(current.get("items"), list):
        return ["current inventory must be a Kubernetes List with items"]

    before_names: set[str] = set()
    for item in before:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            errors.append("pre-phase inventory contains an invalid claim entry")
            continue
        name = item["name"]
        if name in before_names:
            errors.append(f"pre-phase inventory contains duplicate claim: {name}")
        before_names.add(name)
        if item.get("phase") != "Bound":
            errors.append(f"pre-phase claim was not Bound: {name}")

    current_claims: dict[str, str | None] = {}
    for item in current["items"]:
        if not isinstance(item, dict):
            errors.append("current inventory contains an invalid claim entry")
            continue
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        name = metadata.get("name")
        if not isinstance(name, str):
            errors.append("current inventory contains a claim without a name")
            continue
        if name in current_claims:
            errors.append(f"current inventory contains duplicate claim: {name}")
        current_claims[name] = status.get("phase")
        if status.get("phase") != "Bound":
            errors.append(f"current claim is not Bound: {name}")

    for name in sorted(before_names - current_claims.keys()):
        errors.append(f"pre-phase claim is missing: {name}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    args = parser.parse_args()

    before = json.loads(args.before.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    errors = validate_inventory(before, current)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("monitoring PVC inventory preserved and Bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
