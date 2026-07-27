#!/usr/bin/env python3
"""Inspect or atomically update PinLog Backend immutable image fields."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


TAG_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FIELD_RE = re.compile(
    r"^(?P<indent>  )(?P<key>repository|tag|digest):(?P<space>\s*)"
    r"(?P<value>[^#\n]*?)(?P<comment>\s+#.*)?(?P<newline>\n?)$"
)
EXPECTED_REPOSITORY = "ghcr.io/team-pinlog/back"


@dataclass(frozen=True)
class ParsedValues:
    original: str
    lines: list[str]
    fields: dict[str, tuple[int, re.Match[str]]]
    repository: str
    tag: str
    digest: str


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_values(path: Path) -> ParsedValues:
    if path.is_symlink() or not path.is_file():
        raise ValueError("values path must be a regular file, not a symlink")

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    image_indexes = [
        index for index, line in enumerate(lines) if line.rstrip("\r\n") == "image:"
    ]
    if len(image_indexes) != 1:
        raise ValueError("values file must contain exactly one top-level image mapping")

    start = image_indexes[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if stripped and not lines[index].startswith((" ", "\t", "#")):
            end = index
            break

    fields: dict[str, tuple[int, re.Match[str]]] = {}
    for index in range(start, end):
        match = FIELD_RE.fullmatch(lines[index])
        if not match:
            continue
        key = match.group("key")
        if key in fields:
            raise ValueError(f"duplicate image.{key} field")
        fields[key] = (index, match)
    if set(fields) != {"repository", "tag", "digest"}:
        raise ValueError(
            "image mapping must contain exactly one repository, tag, and digest field"
        )

    repository = _unquote(fields["repository"][1].group("value"))
    tag = _unquote(fields["tag"][1].group("value"))
    digest = _unquote(fields["digest"][1].group("value"))
    if repository != EXPECTED_REPOSITORY:
        raise ValueError(f"image.repository must remain {EXPECTED_REPOSITORY}")
    if not TAG_RE.fullmatch(tag):
        raise ValueError("existing tag must be a lowercase 40-character Git commit SHA")
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError(
            "existing digest must be sha256 followed by 64 lowercase hex characters"
        )

    return ParsedValues(
        original=original,
        lines=lines,
        fields=fields,
        repository=repository,
        tag=tag,
        digest=digest,
    )


def update_values(path: Path, tag: str, digest: str) -> bool:
    if not TAG_RE.fullmatch(tag):
        raise ValueError("tag must be a lowercase 40-character Git commit SHA")
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("digest must be sha256 followed by 64 lowercase hex characters")

    parsed = parse_values(path)
    if parsed.tag == tag and parsed.digest == digest:
        return False

    lines = parsed.lines.copy()
    for key, value in (("tag", tag), ("digest", digest)):
        index, match = parsed.fields[key]
        comment = match.group("comment") or ""
        newline = match.group("newline")
        lines[index] = f"  {key}: {value}{comment}{newline}"

    updated = "".join(lines)
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, mode)
        with open(temporary_name, "rb") as durable_temporary:
            os.fsync(durable_temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--tag")
    parser.add_argument("--digest")
    args = parser.parse_args()
    try:
        if args.inspect:
            if args.tag is not None or args.digest is not None:
                raise ValueError("--inspect cannot be combined with --tag or --digest")
            parsed = parse_values(args.values)
            print(parsed.repository)
            print(parsed.tag)
            print(parsed.digest)
            return 0
        if args.tag is None or args.digest is None:
            raise ValueError("--tag and --digest are required unless --inspect is used")
        changed = update_values(args.values, args.tag, args.digest)
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
