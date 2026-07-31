import hashlib
import re
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "microservice"
LEGACY_SHA_TAG = "a" * 40
RUN_BOUND_TAG = (
    "775629c1ebf015426b5589a24e2f17dcdae1203e-"
    "cfg-b41c064fb34a495a273b-run-30597520006-a1"
)
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def render(tag: str) -> list[dict]:
    output = subprocess.run(
        [
            "helm",
            "template",
            "front",
            str(CHART),
            "--namespace",
            "pinlog-dev",
            "--set-string",
            "image.repository=ghcr.io/team-pinlog/front",
            "--set-string",
            f"image.tag={tag}",
            "--set-string",
            "image.digest=sha256:" + "b" * 64,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [document for document in yaml.safe_load_all(output) if document]


def deployment(documents: list[dict]) -> dict:
    return next(document for document in documents if document["kind"] == "Deployment")


class MicroserviceLabelValueTest(unittest.TestCase):
    def test_legacy_sha_tag_remains_readable_and_image_reference_is_unchanged(self):
        documents = render(LEGACY_SHA_TAG)

        for document in documents:
            labels = document.get("metadata", {}).get("labels", {})
            if "app.kubernetes.io/version" in labels:
                self.assertEqual(labels["app.kubernetes.io/version"], LEGACY_SHA_TAG)

        image = deployment(documents)["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(
            image,
            f"ghcr.io/team-pinlog/front:{LEGACY_SHA_TAG}@sha256:" + "b" * 64,
        )

    def test_run_bound_tag_uses_stable_dns_label_without_changing_image_or_selectors(self):
        self.assertEqual(len(RUN_BOUND_TAG), 84)
        legacy_documents = render(LEGACY_SHA_TAG)
        run_bound_documents = render(RUN_BOUND_TAG)
        repeated_documents = render(RUN_BOUND_TAG)

        version_labels = {
            document["metadata"]["labels"]["app.kubernetes.io/version"]
            for document in run_bound_documents
            if "app.kubernetes.io/version" in document.get("metadata", {}).get("labels", {})
        }
        self.assertEqual(len(version_labels), 1)
        version_label = version_labels.pop()
        self.assertLessEqual(len(version_label), 63)
        self.assertRegex(version_label, DNS_LABEL_RE)
        self.assertNotEqual(version_label, RUN_BOUND_TAG)
        expected_version_label = (
            RUN_BOUND_TAG[:50].rstrip("-")
            + "-"
            + hashlib.sha256(RUN_BOUND_TAG.encode()).hexdigest()[:12]
        )
        self.assertEqual(version_label, expected_version_label)

        repeated_version_labels = {
            document["metadata"]["labels"]["app.kubernetes.io/version"]
            for document in repeated_documents
            if "app.kubernetes.io/version" in document.get("metadata", {}).get("labels", {})
        }
        self.assertEqual(repeated_version_labels, {version_label})

        legacy_deployment = deployment(legacy_documents)
        run_bound_deployment = deployment(run_bound_documents)
        self.assertEqual(
            run_bound_deployment["spec"]["template"]["spec"]["containers"][0]["image"],
            f"ghcr.io/team-pinlog/front:{RUN_BOUND_TAG}@sha256:" + "b" * 64,
        )
        self.assertEqual(
            run_bound_deployment["spec"]["selector"]["matchLabels"],
            legacy_deployment["spec"]["selector"]["matchLabels"],
        )
        self.assertEqual(
            run_bound_deployment["spec"]["selector"]["matchLabels"],
            {
                key: run_bound_deployment["spec"]["template"]["metadata"]["labels"][key]
                for key in run_bound_deployment["spec"]["selector"]["matchLabels"]
            },
        )


if __name__ == "__main__":
    unittest.main()
