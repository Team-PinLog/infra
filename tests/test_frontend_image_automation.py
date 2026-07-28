from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "apps" / "dev" / "front" / "values.yaml"
UPDATER = ROOT / "tools" / "update_frontend_image.py"
UPDATE_WORKFLOW = ROOT / ".github" / "workflows" / "frontend-image-update.yaml"
AUTO_MERGE_WORKFLOW = ROOT / ".github" / "workflows" / "frontend-image-auto-merge.yaml"
NEW_TAG = "a" * 40
NEW_DIGEST = "sha256:" + "b" * 64



class FrontendScaffoldContractTest(unittest.TestCase):
    def test_frontend_dev_scaffold_is_private_internal_and_activation_blocked(self):
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        self.assertEqual(values["application"], {"enabled": False})
        self.assertEqual(values["deployment"], {"enabled": False})
        self.assertEqual(values["image"]["repository"], "ghcr.io/team-pinlog/front")
        self.assertEqual(values["imagePullSecrets"], [{"name": "ghcr-front-pull"}])
        self.assertEqual(values["service"], {"type": "ClusterIP", "port": 80, "targetPort": 80})
        self.assertEqual(values["ingress"], {"enabled": False})
        self.assertEqual(values["env"], [])
        self.assertFalse(values["probes"]["enabled"])

    def test_blocked_scaffold_renders_no_kubernetes_resources(self):
        command = [
            "helm",
            "template",
            "front",
            str(ROOT / "charts" / "microservice"),
            "--namespace",
            "pinlog-dev",
            "--values",
            str(VALUES),
        ]
        blocked = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(blocked.stdout.strip(), "")

        control = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        control["application"]["enabled"] = True
        control["deployment"]["enabled"] = True
        control["image"]["tag"] = NEW_TAG
        control["image"]["digest"] = NEW_DIGEST
        with tempfile.TemporaryDirectory() as directory:
            enabled_values = Path(directory) / "values.yaml"
            enabled_values.write_text(yaml.safe_dump(control), encoding="utf-8")
            enabled = subprocess.run(
                [*command[:-1], str(enabled_values)], capture_output=True, text=True
            )
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        self.assertIn("kind: Deployment", enabled.stdout)


class FrontendImageUpdaterTest(unittest.TestCase):
    def run_updater(self, values: Path, tag: str, digest: str):
        return subprocess.run(
            [sys.executable, str(UPDATER), "--values", str(values), "--tag", tag, "--digest", digest],
            capture_output=True,
            text=True,
        )

    def test_updater_changes_only_blocked_scaffold_image_and_is_idempotent(self):
        source = VALUES.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(source, encoding="utf-8")
            first = self.run_updater(values, NEW_TAG, NEW_DIGEST)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout.strip(), "changed=true")
            updated = yaml.safe_load(values.read_text(encoding="utf-8"))
            self.assertEqual(updated["image"]["tag"], NEW_TAG)
            self.assertEqual(updated["image"]["digest"], NEW_DIGEST)
            self.assertFalse(updated["application"]["enabled"])
            self.assertFalse(updated["deployment"]["enabled"])
            second = self.run_updater(values, NEW_TAG, NEW_DIGEST)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), "changed=false")

    def test_updater_rejects_mutable_input_without_mutation(self):
        source = VALUES.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(source, encoding="utf-8")
            result = self.run_updater(values, "latest", "sha256:invalid")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(values.read_text(encoding="utf-8"), source)

    def test_inspect_is_read_only_after_an_immutable_candidate_update(self):
        source = VALUES.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(source, encoding="utf-8")
            update = self.run_updater(values, NEW_TAG, NEW_DIGEST)
            self.assertEqual(update.returncode, 0, update.stderr)
            immutable = values.read_text(encoding="utf-8")
            inspect = subprocess.run(
                [sys.executable, str(UPDATER), "--values", str(values), "--inspect"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(inspect.returncode, 0, inspect.stderr)
            self.assertEqual(
                inspect.stdout.splitlines(),
                ["ghcr.io/team-pinlog/front", NEW_TAG, NEW_DIGEST],
            )
            self.assertEqual(values.read_text(encoding="utf-8"), immutable)

    def test_publish_digest_parser_is_run_bound_and_fail_closed(self):
        parser = ROOT / "tools" / "extract_frontend_publish_digest.py"
        valid = (
            "frontend-image / publish\tVerify published image digest\t"
            f"2026-07-28T04:55:11Z Published digest: {NEW_DIGEST}\n"
        )
        success = subprocess.run(
            [sys.executable, str(parser)], input=valid, capture_output=True, text=True
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(success.stdout.strip(), NEW_DIGEST)
        for invalid in ("", valid + valid, valid.replace("frontend-image / publish", "frontend-ci / check")):
            with self.subTest(log=invalid):
                failure = subprocess.run(
                    [sys.executable, str(parser)], input=invalid, capture_output=True, text=True
                )
                self.assertNotEqual(failure.returncode, 0)


class FrontendImageWorkflowContractTest(unittest.TestCase):
    def load(self, path: Path):
        return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_updater_is_bounded_fail_closed_and_creates_only_frontend_pr(self):
        workflow = self.load(UPDATE_WORKFLOW)
        self.assertEqual(set(workflow["on"]), {"schedule", "workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertIn(
            "vars.FRONTEND_IMAGE_AUTOMATION_APPROVED == 'true'",
            workflow["jobs"]["detect-source"]["if"],
        )
        text = UPDATE_WORKFLOW.read_text(encoding="utf-8")
        for contract in (
            "Team-PinLog/front",
            "ci.yml",
            "SOURCE_BRANCH: dev",
            "event=push",
            "status=success",
            "ghcr.io/team-pinlog/front",
            "PINLOG_FRONT_IMAGE_UPDATER_TOKEN",
            "PINLOG_FRONT_IMAGE_UPDATER_USERNAME",
            "tools/extract_frontend_publish_digest.py",
            "tools/update_frontend_image.py",
            "automation/frontend-image-update",
            "add-paths: apps/dev/front/values.yaml",
            "python -m unittest discover -s tests -v",
        ):
            self.assertIn(contract, text)
        self.assertNotIn(":latest", text.lower())

    def test_trusted_auto_merge_is_exact_head_bound_and_front_file_only(self):
        workflow = self.load(AUTO_MERGE_WORKFLOW)
        self.assertEqual(set(workflow["on"]), {"workflow_run"})
        self.assertEqual(
            workflow["permissions"],
            {"checks": "read", "contents": "write", "pull-requests": "write"},
        )
        text = AUTO_MERGE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "vars.FRONTEND_IMAGE_AUTO_MERGE_APPROVED == 'true'",
            workflow["jobs"]["verify-and-merge"]["if"],
        )
        for contract in (
            "automation/frontend-image-update",
            "apps/dev/front/values.yaml",
            "Team-PinLog/front",
            "ci.yml",
            "--match-head-commit",
            "--squash --delete-branch",
            "REQUIRED_CHECKS: guardrails helm pr-policy",
            "PINLOG_FRONT_IMAGE_UPDATER_TOKEN",
        ):
            self.assertIn(contract, text)
        self.assertNotIn("workflow_run.pull_requests[0]", text)
        self.assertNotIn("--auto", text)


if __name__ == "__main__":
    unittest.main()
