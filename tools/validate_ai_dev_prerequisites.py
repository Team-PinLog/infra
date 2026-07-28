#!/usr/bin/env python3
"""Offline/fail-closed validators for AI dev activation prerequisites.

The validators never print secret values. Runtime values are read only when an
operator explicitly invokes the profile subcommand against an ephemeral file.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Mapping


REQUIRED_RUNTIME_KEYS = {
    "DATABASE_URL",
    "GMS_API_KEY",
    "GMS_BASE_URL",
    "PINLOG_EMBEDDING_MODEL",
    "PINLOG_EMBEDDING_DIMENSION",
    "PINLOG_EMBEDDING_DISTANCE",
    "PINLOG_EMBEDDING_PROFILE",
    "INTERNAL_SHARED_SECRET",
}
_PROFILE_FIELDS = {
    "model": "PINLOG_EMBEDDING_MODEL",
    "dimension": "PINLOG_EMBEDDING_DIMENSION",
    "distance": "PINLOG_EMBEDDING_DISTANCE",
}
_MIGRATION = re.compile(r"^V([0-9]+)__.+\.sql$")


def validate_secret_keys(keys: set[str]) -> list[str]:
    errors: list[str] = []
    missing = REQUIRED_RUNTIME_KEYS - keys
    unexpected = keys - REQUIRED_RUNTIME_KEYS
    if missing:
        errors.append("required runtime keys are missing: " + ", ".join(sorted(missing)))
    if unexpected:
        # Never echo malformed input: a caller may accidentally provide KEY=value.
        errors.append("unapproved or malformed runtime key entries are present")
    return errors


def validate_profile_contract(
    runtime: Mapping[str, str], approved_profiles: Mapping[str, object]
) -> list[str]:
    errors: list[str] = []
    required = {"PINLOG_EMBEDDING_PROFILE", *_PROFILE_FIELDS.values()}
    missing = sorted(name for name in required if not runtime.get(name))
    if missing:
        return ["profile preflight fields are missing: " + ", ".join(missing)]

    profile = runtime["PINLOG_EMBEDDING_PROFILE"]
    approved = approved_profiles.get(profile)
    if not isinstance(approved, dict):
        return ["PINLOG_EMBEDDING_PROFILE is not in the approved contract"]
    if set(approved) != set(_PROFILE_FIELDS):
        return ["approved profile must define exactly model, dimension and distance"]

    for contract_field, runtime_field in _PROFILE_FIELDS.items():
        expected = approved.get(contract_field)
        if not isinstance(expected, str) or not expected:
            errors.append(f"approved profile field {contract_field} is invalid")
        elif runtime[runtime_field] != expected:
            errors.append(f"{runtime_field} does not match PINLOG_EMBEDDING_PROFILE")
    dimension = runtime["PINLOG_EMBEDDING_DIMENSION"]
    if not dimension.isdecimal() or int(dimension) <= 0:
        errors.append("PINLOG_EMBEDDING_DIMENSION must be a positive integer")
    return errors


def validate_flyway_migrations(directory: Path) -> list[str]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("migration directory must be a real directory")
    by_version: dict[int, list[str]] = {}
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        match = _MIGRATION.fullmatch(path.name)
        if match:
            by_version.setdefault(int(match.group(1)), []).append(path.name)
    for version, names in by_version.items():
        if len(names) != 1:
            raise ValueError(f"duplicate Flyway version V{version}")
    required = (1, 100, 101)
    missing = [f"V{version}" for version in required if version not in by_version]
    if missing:
        raise ValueError("required Flyway migrations are missing: " + ", ".join(missing))
    return [by_version[version][0] for version in required]


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw or raw.lstrip().startswith("#"):
            continue
        if "=" not in raw:
            raise ValueError(f"invalid environment entry at line {number}")
        key, value = raw.split("=", 1)
        if not key or key in result:
            raise ValueError(f"invalid or duplicate environment key at line {number}")
        result[key] = value
    return result


def _emit(errors: list[str]) -> int:
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("contract=valid")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    keys = subparsers.add_parser("secret-keys")
    keys.add_argument("keys_file", type=Path)
    profile = subparsers.add_parser("profile")
    profile.add_argument("runtime_env", type=Path)
    profile.add_argument("approved_profiles", type=Path)
    flyway = subparsers.add_parser("flyway-files")
    flyway.add_argument("migrations_dir", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "secret-keys":
            names = {
                line.strip()
                for line in args.keys_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            return _emit(validate_secret_keys(names))
        if args.command == "profile":
            runtime = _read_env(args.runtime_env)
            approved = json.loads(args.approved_profiles.read_text(encoding="utf-8"))
            if not isinstance(approved, dict):
                raise ValueError("approved profile contract must be a JSON object")
            return _emit(validate_profile_contract(runtime, approved))
        ordered = validate_flyway_migrations(args.migrations_dir)
        print("required-order=" + ",".join(ordered))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
