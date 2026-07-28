import os
from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "apps" / "dev" / "front" / "values.yaml"
TAG_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class FrontendImageCandidateTest(unittest.TestCase):
    def test_frontend_image_matches_required_candidate(self):
        image = yaml.safe_load(VALUES.read_text(encoding="utf-8"))["image"]
        self.assertEqual(image["repository"], "ghcr.io/team-pinlog/front")
        expected_tag = os.environ.get("FRONTEND_IMAGE_TAG")
        expected_digest = os.environ.get("FRONTEND_IMAGE_DIGEST")
        self.assertEqual(
            expected_tag is None,
            expected_digest is None,
            "FRONTEND_IMAGE_TAG and FRONTEND_IMAGE_DIGEST must be set together",
        )
        if expected_tag is None:
            if image["tag"] == "BLOCKED_UNVERIFIED_IMAGE":
                self.assertEqual(image["digest"], "")
                return
            self.assertRegex(image["tag"], TAG_RE)
            self.assertRegex(image["digest"], DIGEST_RE)
            return
        assert expected_digest is not None
        self.assertRegex(expected_tag, TAG_RE)
        self.assertRegex(expected_digest, DIGEST_RE)
        self.assertEqual((image["tag"], image["digest"]), (expected_tag, expected_digest))


if __name__ == "__main__":
    unittest.main()
