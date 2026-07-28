from pathlib import Path
import re
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
    def test_frontend_dev_is_activated_private_and_immutable(self):
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        self.assertEqual(values["application"], {"enabled": True})
        self.assertEqual(values["deployment"], {"enabled": True})
        self.assertEqual(values["image"]["repository"], "ghcr.io/team-pinlog/front")
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{40}", values["image"]["tag"]))
        self.assertIsNotNone(
            re.fullmatch(r"sha256:[0-9a-f]{64}", values["image"]["digest"])
        )
        self.assertEqual(values["imagePullSecrets"], [{"name": "ghcr-front-pull"}])
        self.assertEqual(values["service"], {"type": "ClusterIP", "port": 80, "targetPort": 8080})
        self.assertEqual(values["ingress"], {"enabled": False})
        self.assertEqual(values["resources"], {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}})
        self.assertEqual(values["env"], [])
        self.assertEqual(values["probes"], {"enabled": True, "path": "/healthz"})
        self.assertEqual(values["podSecurityContext"], {
            "runAsNonRoot": True,
            "runAsUser": 101,
            "runAsGroup": 101,
            "seccompProfile": {"type": "RuntimeDefault"},
        })
        self.assertEqual(values["securityContext"], {
            "runAsNonRoot": True,
            "runAsUser": 101,
            "runAsGroup": 101,
            "readOnlyRootFilesystem": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        })
        self.assertEqual(values["writableTmp"], {"enabled": True, "sizeLimit": "32Mi"})

    def test_frontend_immutable_reference_patterns_reject_non_exact_values(self):
        invalid_tags = (
            "a" * 40 + "\n",
            "A" * 40,
            "a" * 39,
            "prefix" + "a" * 40,
            "a" * 40 + "suffix",
        )
        invalid_digests = (
            "sha256:" + "b" * 64 + "\n",
            "sha256:" + "B" * 64,
            "sha256:" + "b" * 63,
            "prefix-sha256:" + "b" * 64,
            "sha256:" + "b" * 64 + "suffix",
        )
        for value in invalid_tags:
            with self.subTest(tag=value):
                self.assertIsNone(re.fullmatch(r"[0-9a-f]{40}", value))
        for value in invalid_digests:
            with self.subTest(digest=value):
                self.assertIsNone(re.fullmatch(r"sha256:[0-9a-f]{64}", value))

    def test_enabled_control_renders_front_runtime_contract(self):
        control = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        control["application"]["enabled"] = True
        control["deployment"]["enabled"] = True
        control["image"]["tag"] = NEW_TAG
        control["image"]["digest"] = NEW_DIGEST
        with tempfile.TemporaryDirectory() as directory:
            enabled_values = Path(directory) / "values.yaml"
            enabled_values.write_text(yaml.safe_dump(control), encoding="utf-8")
            rendered = subprocess.run(
                [
                    "helm", "template", "front", str(ROOT / "charts" / "microservice"),
                    "--namespace", "pinlog-dev", "--values", str(enabled_values),
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        deployment = next(
            document for document in yaml.safe_load_all(rendered.stdout)
            if document and document.get("kind") == "Deployment"
        )
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(container["ports"][0]["containerPort"], 8080)
        for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
            self.assertEqual(container[probe]["httpGet"], {"path": "/healthz", "port": "http"})
        self.assertEqual(container["securityContext"]["runAsUser"], 101)
        self.assertEqual(container["securityContext"]["runAsGroup"], 101)
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertIn({"name": "tmp", "mountPath": "/tmp"}, container["volumeMounts"])
        self.assertIn({"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}}, pod["volumes"])

    def test_activated_values_render_exactly_one_deployment_and_clusterip_service(self):
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
        rendered = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        resources = [document for document in yaml.safe_load_all(rendered.stdout) if document]
        self.assertEqual(
            [resource["kind"] for resource in resources],
            ["ServiceAccount", "Service", "Deployment"],
        )
        self.assertEqual([resource["kind"] for resource in resources].count("Deployment"), 1)
        services = [resource for resource in resources if resource["kind"] == "Service"]
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["spec"]["type"], "ClusterIP")
        self.assertNotIn("Ingress", [resource["kind"] for resource in resources])


class FrontendImageUpdaterTest(unittest.TestCase):
    def run_updater(self, values: Path, tag: str, digest: str):
        return subprocess.run(
            [sys.executable, str(UPDATER), "--values", str(values), "--tag", tag, "--digest", digest],
            capture_output=True,
            text=True,
        )

    def test_updater_changes_only_activated_front_image_and_is_idempotent(self):
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
            self.assertTrue(updated["application"]["enabled"])
            self.assertTrue(updated["deployment"]["enabled"])
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
            "PINLOG_IMAGE_UPDATER_TOKEN",
            "PINLOG_IMAGE_UPDATER_USERNAME",
            "tools/extract_frontend_publish_digest.py",
            "tools/update_frontend_image.py",
            "automation/frontend-image-update",
            "add-paths: apps/dev/front/values.yaml",
            "python -m unittest discover -s tests -v",
        ):
            self.assertIn(contract, text)
        self.assertNotIn(":latest", text.lower())
        self.assertNotIn("PINLOG_FRONT_IMAGE_UPDATER", text)

    def test_updater_pr_body_describes_activated_rollout_truthfully(self):
        text = UPDATE_WORKFLOW.read_text(encoding="utf-8")
        for contract in (
            "Frontend dev 활성 workload values의 image tag/digest만 변경합니다.",
            "`application.enabled: true`와 `deployment.enabled: true`를 유지합니다.",
            "immutable image Pod rollout이 발생합니다.",
            "ClusterIP-only",
            "Ingress는 생성하지 않습니다.",
        ):
            self.assertIn(contract, text)
        for stale_claim in (
            "Frontend dev 차단 scaffold",
            "`application.enabled: false`와 `deployment.enabled: false`",
            "Pod rollout은 발생하지 않습니다.",
        ):
            self.assertNotIn(stale_claim, text)

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
            "PINLOG_IMAGE_UPDATER_TOKEN",
        ):
            self.assertIn(contract, text)
        self.assertNotIn("workflow_run.pull_requests[0]", text)
        self.assertNotIn("--auto", text)
        self.assertNotIn("PINLOG_FRONT_IMAGE_UPDATER", text)


if __name__ == "__main__":
    unittest.main()
