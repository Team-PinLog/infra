#!/usr/bin/env python3
"""Atomically update only the activated Frontend workload's immutable image fields."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from pathlib import Path

import yaml


TAG_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
EXPECTED_REPOSITORY = "ghcr.io/team-pinlog/front"
INITIAL_TAG = "BLOCKED_UNVERIFIED_IMAGE"
FIELD_RE = re.compile(
    r"^(?P<indent>  )(?P<key>repository|tag|digest):(?P<space>\s*)"
    r"(?P<value>[^#\n]*?)(?P<comment>\s+#.*)?(?P<newline>\n?)$"
)


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(path: Path) -> tuple[str, list[str], dict[str, tuple[int, re.Match[str]]]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("values path must be a regular file, not a symlink")
    original = path.read_text(encoding="utf-8")
    document = yaml.safe_load(original)
    if not isinstance(document, dict):
        raise ValueError("values must be a YAML mapping")
    if document.get("application") != {"enabled": True}:
        raise ValueError("Frontend application must remain enabled during image updates")
    if document.get("deployment") != {"enabled": True}:
        raise ValueError("Frontend deployment must remain enabled during image updates")
    if document.get("bootstrap", {}).get("enabled") is not False:
        raise ValueError("Frontend bootstrap must remain disabled during image updates")

    lines = original.splitlines(keepends=True)
    image_indexes = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == "image:"]
    if len(image_indexes) != 1:
        raise ValueError("values must contain exactly one top-level image mapping")
    start = image_indexes[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].strip() and not lines[index].startswith((" ", "\t", "#")):
            end = index
            break
    fields: dict[str, tuple[int, re.Match[str]]] = {}
    for index in range(start, end):
        match = FIELD_RE.fullmatch(lines[index])
        if not match:
            continue
        key = match.group("key")
        if key in fields:
            raise ValueError(f"duplicate image.{key}")
        fields[key] = (index, match)
    if set(fields) != {"repository", "tag", "digest"}:
        raise ValueError("image requires exactly repository, tag, and digest")
    repository = unquote(fields["repository"][1].group("value"))
    current_tag = unquote(fields["tag"][1].group("value"))
    current_digest = unquote(fields["digest"][1].group("value"))
    if repository != EXPECTED_REPOSITORY:
        raise ValueError(f"image.repository must remain {EXPECTED_REPOSITORY}")
    initial = current_tag == INITIAL_TAG and current_digest == ""
    immutable = TAG_RE.fullmatch(current_tag) and DIGEST_RE.fullmatch(current_digest)
    if not (initial or immutable):
        raise ValueError("existing image must be the initial placeholder or immutable SHA/digest")
    return original, lines, fields


def update(path: Path, tag: str, digest: str) -> bool:
    if not TAG_RE.fullmatch(tag):
        raise ValueError("tag must be a lowercase 40-character Git commit SHA")
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("digest must be sha256 followed by 64 lowercase hex characters")
    original, lines, fields = parse(path)
    current = {
        key: unquote(match.group("value")) for key, (_, match) in fields.items()
    }
    if current["tag"] == tag and current["digest"] == digest:
        return False
    for key, value in (("tag", tag), ("digest", digest)):
        index, match = fields[key]
        lines[index] = f"  {key}: {value}{match.group('comment') or ''}{match.group('newline')}"
    updated = "".join(lines)
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    if path.read_text(encoding="utf-8") == original:
        raise RuntimeError("atomic update reported a change but content is unchanged")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--values", required=True, type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--tag")
    parser.add_argument("--digest")
    args = parser.parse_args()
    try:
        if args.inspect:
            if args.tag is not None or args.digest is not None:
                raise ValueError("--inspect cannot be combined with --tag or --digest")
            _, _, fields = parse(args.values)
            print(unquote(fields["repository"][1].group("value")))
            print(unquote(fields["tag"][1].group("value")))
            print(unquote(fields["digest"][1].group("value")))
            return 0
        if args.tag is None or args.digest is None:
            raise ValueError("--tag and --digest are required unless --inspect is used")
        changed = update(args.values, args.tag, args.digest)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
