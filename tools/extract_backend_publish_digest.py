#!/usr/bin/env python3
"""Extract one run-bound Backend publish digest from GitHub Actions logs."""

from __future__ import annotations

import re
import sys


PUBLISHED_DIGEST_RE = re.compile(
    r"(?m)^backend-image / publish\t[^\t\n]+\t\S+ "
    r"Published digest: (sha256:[0-9a-f]{64})[ \r]*$"
)


def extract_published_digest(log_text: str) -> str:
    matches = PUBLISHED_DIGEST_RE.findall(log_text)
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one concrete Published digest line, found {len(matches)}"
        )
    return matches[0]


def main() -> int:
    try:
        print(extract_published_digest(sys.stdin.read()))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
