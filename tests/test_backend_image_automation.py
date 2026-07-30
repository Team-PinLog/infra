import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from tools.extract_backend_publish_digest import extract_published_digest
from tools.validate_pr_body import validate_pr_body


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "apps" / "prod" / "back" / "values.yaml"
UPDATER = ROOT / "tools" / "update_backend_image.py"
CANDIDATE_TEST = "tests.test_backend_image_candidate"
UPDATE_WORKFLOW = ROOT / ".github" / "workflows" / "backend-image-update.yaml"
AUTO_MERGE_WORKFLOW = ROOT / ".github" / "workflows" / "backend-image-auto-merge.yaml"

OLD_TAG = "c011cffe5cb045acef6454c46feb230f5f02c3f2"
OLD_DIGEST = "sha256:a151caeb5ab1bfa79d109ee3d4c44bee2270a991f94d9b7189da34db5ed1c0da"
NEW_TAG = "a87ec2b57f02c14c9900d4261e53a549eabf4f08"
NEW_DIGEST = "sha256:b14e78df42ed055de500e4dddbc9dfda403ade9b42dbdcab1e017297af191c3b"


class BackendImageUpdaterTest(unittest.TestCase):
    def run_updater(self, values: Path, tag: str, digest: str):
        return subprocess.run(
            [
                sys.executable,
                str(UPDATER),
                "--values",
                str(values),
                "--tag",
                tag,
                "--digest",
                digest,
            ],
            capture_output=True,
            text=True,
        )

    def test_updater_changes_only_immutable_image_fields_and_is_idempotent(self):
        original_text = VALUES.read_text()
        original = yaml.safe_load(original_text)
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(original_text)

            first = self.run_updater(values, NEW_TAG, NEW_DIGEST)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("changed=true", first.stdout)
            updated_text = values.read_text()
            updated = yaml.safe_load(updated_text)
            expected = yaml.safe_load(original_text)
            expected["image"]["tag"] = NEW_TAG
            expected["image"]["digest"] = NEW_DIGEST
            expected_text = original_text.replace(
                f"  tag: {original['image']['tag']}\n",
                f"  tag: {NEW_TAG}\n",
                1,
            ).replace(
                f"  digest: {original['image']['digest']}\n",
                f"  digest: {NEW_DIGEST}\n",
                1,
            )
            self.assertEqual(updated_text, expected_text)
            self.assertEqual(updated, expected)
            self.assertEqual(updated["image"]["repository"], "ghcr.io/team-pinlog/back")

            second = self.run_updater(values, NEW_TAG, NEW_DIGEST)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("changed=false", second.stdout)
            self.assertEqual(yaml.safe_load(values.read_text()), expected)

    def test_updater_rejects_invalid_input_without_mutating_values(self):
        original_text = VALUES.read_text()
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(original_text)
            result = self.run_updater(values, "latest", "sha256:not-a-digest")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(values.read_text(), original_text)

    def test_inspect_is_read_only_and_rejects_duplicate_yaml_image_fields(self):
        original_text = VALUES.read_text()
        current = yaml.safe_load(original_text)["image"]
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(original_text)
            inspect = subprocess.run(
                [
                    sys.executable,
                    str(UPDATER),
                    "--values",
                    str(values),
                    "--inspect",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(inspect.returncode, 0, inspect.stderr)
            self.assertEqual(
                inspect.stdout.splitlines(),
                [current["repository"], current["tag"], current["digest"]],
            )
            self.assertEqual(values.read_text(), original_text)

            duplicate_field = original_text.replace(
                f"  tag: {current['tag']}\n",
                f"  tag: {current['tag']}\n  tag: {NEW_TAG}\n",
                1,
            )
            values.write_text(duplicate_field)
            duplicate = subprocess.run(
                [
                    sys.executable,
                    str(UPDATER),
                    "--values",
                    str(values),
                    "--inspect",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn("duplicate image.tag", duplicate.stderr)

            values.write_text(
                original_text
                + "\nimage:\n"
                + "  repository: ghcr.io/team-pinlog/back\n"
                + f"  tag: {NEW_TAG}\n"
                + f"  digest: {NEW_DIGEST}\n"
            )
            duplicate_mapping = subprocess.run(
                [
                    sys.executable,
                    str(UPDATER),
                    "--values",
                    str(values),
                    "--inspect",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(duplicate_mapping.returncode, 0)
            self.assertIn("exactly one top-level image mapping", duplicate_mapping.stderr)

    def test_candidate_contract_produces_red_and_green_against_expected_image(self):
        current = yaml.safe_load(VALUES.read_text())["image"]
        green_env = os.environ | {
            "BACKEND_IMAGE_TAG": current["tag"],
            "BACKEND_IMAGE_DIGEST": current["digest"],
        }
        green = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", CANDIDATE_TEST],
            cwd=ROOT,
            env=green_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)

        red_env = os.environ | {
            "BACKEND_IMAGE_TAG": NEW_TAG if current["tag"] != NEW_TAG else OLD_TAG,
            "BACKEND_IMAGE_DIGEST": NEW_DIGEST if current["digest"] != NEW_DIGEST else OLD_DIGEST,
        }
        red = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", CANDIDATE_TEST],
            cwd=ROOT,
            env=red_env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(red.returncode, 0)
        self.assertIn("AssertionError", red.stdout + red.stderr)


    def test_publish_digest_parser_is_exact_and_fail_closed(self):
        digest = NEW_DIGEST
        line = (
            "backend-image / publish\tUNKNOWN STEP\t"
            f"2026-07-27T12:03:35Z Published digest: {digest}\n"
        )
        self.assertEqual(extract_published_digest(line), digest)

        invalid_logs = (
            "",
            f"backend-image / check\tUNKNOWN STEP\t2026-07-27T12:03:35Z Published digest: {digest}\n",
            line + line,
            "backend-image / publish\tUNKNOWN STEP\t2026-07-27T12:03:35Z Published digest: $IMAGE_DIGEST\n",
        )
        for invalid in invalid_logs:
            with self.subTest(log=invalid):
                with self.assertRaises(ValueError):
                    extract_published_digest(invalid)


class BackendImageWorkflowContractTest(unittest.TestCase):
    def load_base_yaml(self, path: Path):
        return yaml.load(path.read_text(), Loader=yaml.BaseLoader)

    def required_check_is_ready(self, check_runs, required="pr-policy"):
        workflow = self.load_base_yaml(AUTO_MERGE_WORKFLOW)
        script = next(
            step["run"]
            for step in workflow["jobs"]["verify-and-merge"]["steps"]
            if step.get("name") == "Reverify source image and merge the exact PR head"
        )
        inner_gate = re.search(
            r"(?ms)^\s*for required in \$REQUIRED_CHECKS; do\n(.*?)^\s*done$",
            script,
        )
        if inner_gate is None:
            self.fail("required-check gate not found")
        command = textwrap.dedent(inner_gate.group(1)) + '\nprintf "%s" "$checks_ready"\n'
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HEAD_SHA": "a" * 40,
                "required": required,
                "checks_json": json.dumps({"check_runs": check_runs}),
                "checks_ready": "true",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout == "true"

    @staticmethod
    def check_run(*, name="pr-policy", status="completed", conclusion: str | None = "success", app="github-actions", head_sha=None):
        return {
            "name": name,
            "head_sha": head_sha or "a" * 40,
            "status": status,
            "conclusion": conclusion,
            "app": {"slug": app},
        }

    def test_required_check_gate_accepts_duplicate_trusted_successful_reruns(self):
        runs = [self.check_run(), self.check_run()]
        self.assertTrue(self.required_check_is_ready(runs))

    def test_required_check_gate_rejects_missing_pending_mixed_and_untrusted_results(self):
        invalid_sets = [
            [],
            [self.check_run(status="queued", conclusion=None)],
            [self.check_run(), self.check_run(conclusion="failure")],
            [self.check_run(app="untrusted-publisher")],
            [self.check_run(head_sha="b" * 40)],
        ]
        for runs in invalid_sets:
            with self.subTest(runs=runs):
                self.assertFalse(self.required_check_is_ready(runs))

    def test_update_workflow_is_bounded_fail_closed_and_pr_only(self):
        workflow = self.load_base_yaml(UPDATE_WORKFLOW)
        trigger = workflow["on"]
        self.assertEqual(set(trigger), {"schedule", "workflow_dispatch"})
        self.assertEqual(trigger["schedule"], [{"cron": "17,47 * * * *"}])
        self.assertEqual(
            workflow["permissions"],
            {"contents": "read"},
        )
        self.assertEqual(workflow["concurrency"]["group"], "backend-image-update")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "true")

        text = UPDATE_WORKFLOW.read_text()
        required = (
            "Team-PinLog/back",
            "backend-ci.yml",
            "SOURCE_BRANCH: dev",
            "event=push",
            "status=success",
            "git/ref/heads/$SOURCE_BRANCH",
            "ghcr.io/team-pinlog/back",
            "PINLOG_IMAGE_UPDATER_TOKEN",
            "PINLOG_IMAGE_UPDATER_USERNAME",
            "GH_TOKEN: ${{ secrets.PINLOG_IMAGE_UPDATER_TOKEN }}",
            "automation/backend-image-update",
            "tools/update_backend_image.py",
            "tools/extract_backend_publish_digest.py",
            "BACKEND_IMAGE_TAG",
            "BACKEND_IMAGE_DIGEST",
            "python -m unittest discover -s tests -v",
            "add-paths: apps/prod/back/values.yaml",
            "gh pr close",
            "--disable-auto",
            "grep -F 'AssertionError'",
            "needs.verify-source-ci.outputs.candidate == 'true' && steps.create-pr.outcome != 'success'",
            "needs.verify-source-ci.outputs.candidate == 'false'",
        )
        for contract in required:
            self.assertIn(contract, text)
        create_pr = next(
            step
            for step in workflow["jobs"]["create-infra-pr"]["steps"]
            if step.get("id") == "create-pr"
        )
        self.assertEqual([], validate_pr_body(create_pr["with"]["body"], "image-updater"))
        self.assertEqual(text.count("gh pr merge"), 1)
        self.assertIn("--disable-auto", text)
        self.assertNotIn("--squash", text)
        lowered = text.lower()
        self.assertNotIn(":latest", lowered)
        self.assertNotIn("tag: latest", lowered)

    def test_trusted_auto_merge_is_head_bound_and_limits_changed_files(self):
        workflow = self.load_base_yaml(AUTO_MERGE_WORKFLOW)
        self.assertEqual(set(workflow["on"]), {"workflow_run"})
        self.assertEqual(
            workflow["on"]["workflow_run"],
            {"workflows": ["validate"], "types": ["completed"]},
        )
        self.assertEqual(
            workflow["permissions"],
            {"checks": "read", "contents": "write", "pull-requests": "write"},
        )
        text = AUTO_MERGE_WORKFLOW.read_text()
        job_condition = workflow["jobs"]["verify-and-merge"]["if"]
        self.assertIn(
            "github.event.workflow_run.head_branch == 'automation/backend-image-update'",
            job_condition,
        )
        required = (
            "github.event.workflow_run.event == 'pull_request'",
            "github.event.workflow_run.conclusion == 'success'",
            "RUN_HEAD_SHA: ${{ github.event.workflow_run.head_sha }}",
            "RUN_HEAD_BRANCH: ${{ github.event.workflow_run.head_branch }}",
            'head="Team-PinLog:$EXPECTED_HEAD_REF"',
            'test "$RUN_HEAD_BRANCH" = "$EXPECTED_HEAD_REF"',
            "jq 'length')\" -eq 1",
            "export HEAD_SHA",
            'test "$HEAD_SHA" = "$RUN_HEAD_SHA"',
            "automation/backend-image-update",
            "apps/prod/back/values.yaml",
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "ref: ${{ github.event.repository.default_branch }}",
            "persist-credentials: false",
            "tools/update_backend_image.py",
            "--inspect",
            "cmp --silent",
            "resolved_digest",
            "SOURCE_TOKEN: ${{ secrets.PINLOG_IMAGE_UPDATER_TOKEN }}",
            "SOURCE_USERNAME: ${{ vars.PINLOG_IMAGE_UPDATER_USERNAME }}",
            "GH_TOKEN=\"$SOURCE_TOKEN\" gh api",
            "check-runs?filter=latest&per_page=100",
            '.app.slug == "github-actions"',
            "REQUIRED_CHECKS: guardrails helm pr-policy",
            "git rev-parse HEAD",
            "--match-head-commit",
            "--squash --delete-branch",
        )
        for contract in required:
            self.assertIn(contract, text)
        self.assertNotIn("--auto", text)
        self.assertNotIn("workflow_run.pull_requests[0]", text)
        self.assertNotIn("ref: ${{ github.event.workflow_run.head_sha }}", text)
        self.assertNotIn("ref: $HEAD_SHA", text)


if __name__ == "__main__":
    unittest.main()
