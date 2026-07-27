import os
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "apps" / "prod" / "back" / "values.yaml"
TAG_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BackendImageCandidateTest(unittest.TestCase):
    def test_backend_image_is_immutable_and_matches_optional_candidate(self):
        image = yaml.safe_load(VALUES.read_text())["image"]
        self.assertEqual(image["repository"], "ghcr.io/team-pinlog/back")
        self.assertRegex(image["tag"], TAG_RE)
        self.assertRegex(image["digest"], DIGEST_RE)

        expected_tag = os.environ.get("BACKEND_IMAGE_TAG")
        expected_digest = os.environ.get("BACKEND_IMAGE_DIGEST")
        self.assertEqual(
            expected_tag is None,
            expected_digest is None,
            "BACKEND_IMAGE_TAG and BACKEND_IMAGE_DIGEST must be set together",
        )
        if expected_tag is not None:
            self.assertIsNotNone(expected_digest)
            assert expected_digest is not None
            self.assertRegex(expected_tag, TAG_RE)
            self.assertRegex(expected_digest, DIGEST_RE)
            self.assertEqual(
                (image["tag"], image["digest"]),
                (expected_tag, expected_digest),
            )


if __name__ == "__main__":
    unittest.main()
