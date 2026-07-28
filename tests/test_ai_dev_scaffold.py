from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "microservice"


def render(values: dict) -> list[dict]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as stream:
        yaml.safe_dump(values, stream)
        stream.flush()
        output = subprocess.run(
            [
                "helm",
                "template",
                "ai",
                str(CHART),
                "--namespace",
                "pinlog-dev",
                "--values",
                stream.name,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    return [document for document in yaml.safe_load_all(output) if document]


class BootstrapJobChartContractTest(unittest.TestCase):
    def test_chart_version_is_bumped_for_bootstrap_contract(self):
        metadata = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], "0.2.0")

    def test_versioned_bootstrap_job_precedes_deployment_and_reuses_runtime_boundaries(self):
        values = {
            "image": {
                "repository": "ghcr.io/team-pinlog/ai",
                "tag": "a" * 40,
                "digest": "sha256:" + "b" * 64,
            },
            "imagePullSecrets": [{"name": "ghcr-ai-pull"}],
            "service": {"targetPort": 8000},
            "envFrom": [{"secretRef": {"name": "ai-runtime-secrets"}}],
            "bootstrap": {
                "enabled": True,
                "version": "preset-v1",
                "command": ["python", "-m", "pinlog.bootstrap", "preset-v1"],
            },
        }
        documents = render(values)
        job = next(document for document in documents if document["kind"] == "Job")
        deployment = next(
            document for document in documents if document["kind"] == "Deployment"
        )

        self.assertEqual(job["metadata"]["name"], "ai-bootstrap-preset-v1")
        self.assertEqual(
            job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "0"
        )
        self.assertEqual(
            job["metadata"]["annotations"]["argocd.argoproj.io/hook"], "PreSync"
        )
        self.assertEqual(
            job["metadata"]["annotations"]["argocd.argoproj.io/hook-delete-policy"],
            "BeforeHookCreation",
        )
        self.assertEqual(
            deployment["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
            "1",
        )
        pod_spec = job["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["restartPolicy"], "Never")
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "ghcr-ai-pull"}])
        container = pod_spec["containers"][0]
        self.assertEqual(
            container["image"],
            "ghcr.io/team-pinlog/ai:" + "a" * 40 + "@sha256:" + "b" * 64,
        )
        self.assertEqual(
            container["envFrom"], [{"secretRef": {"name": "ai-runtime-secrets"}}]
        )
        self.assertEqual(
            container["command"], ["python", "-m", "pinlog.bootstrap", "preset-v1"]
        )
        self.assertEqual(container["resources"], values.get("resources", {
            "requests": {"cpu": "100m", "memory": "384Mi"},
            "limits": {"cpu": "500m", "memory": "768Mi"},
        }))

    def test_enabled_bootstrap_fails_closed_without_version_or_command(self):
        invalid_bootstraps = (
            {"enabled": True, "command": ["bootstrap"]},
            {"enabled": True, "version": "preset-v1"},
            {"enabled": True, "version": "v" * 63, "command": ["bootstrap"]},
        )
        for bootstrap in invalid_bootstraps:
            with self.subTest(bootstrap=bootstrap):
                with tempfile.NamedTemporaryFile("w", suffix=".yaml") as stream:
                    yaml.safe_dump({"bootstrap": bootstrap}, stream)
                    stream.flush()
                    result = subprocess.run(
                        ["helm", "template", "ai", str(CHART), "-f", stream.name],
                        capture_output=True,
                        text=True,
                    )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("bootstrap", result.stderr)


class AiDevValuesContractTest(unittest.TestCase):
    VALUES = ROOT / "apps" / "dev" / "ai" / "values.yaml"

    def test_dev_values_encode_approved_cpu_only_internal_contract(self):
        values = yaml.safe_load(self.VALUES.read_text(encoding="utf-8"))
        self.assertEqual(values["replicaCount"], 1)
        self.assertEqual(values["application"], {"enabled": False})
        self.assertEqual(values["deployment"], {"enabled": False})
        self.assertEqual(values["image"]["repository"], "ghcr.io/team-pinlog/ai")
        self.assertEqual(values["imagePullSecrets"], [{"name": "ghcr-ai-pull"}])
        self.assertEqual(values["service"], {"type": "ClusterIP", "port": 8000, "targetPort": 8000})
        self.assertFalse(values["ingress"]["enabled"])
        self.assertEqual(values["resources"], {
            "requests": {"cpu": "100m", "memory": "384Mi"},
            "limits": {"cpu": "500m", "memory": "768Mi"},
        })
        self.assertFalse(values["autoscaling"]["enabled"])
        self.assertEqual(values["terminationGracePeriodSeconds"], 180)
        self.assertEqual(values["probes"]["path"], "/health")
        self.assertTrue(values["probes"]["enabled"])
        self.assertFalse(values["metrics"]["enabled"])
        self.assertEqual(values["env"], [])
        self.assertEqual(values["securityContext"]["runAsUser"], 10001)
        self.assertTrue(values["securityContext"]["runAsNonRoot"])
        self.assertEqual(values["envFrom"], [{"secretRef": {"name": "ai-runtime-secrets"}}])
        self.assertEqual(values["bootstrap"], {
            "enabled": False,
            "version": "",
            "command": [],
            "backoffLimit": 1,
        })
        self.assertNotIn("nvidia.com/gpu", str(values))

    def test_blocked_scaffold_renders_no_resource_until_application_gate_is_opened(self):
        values = yaml.safe_load(self.VALUES.read_text(encoding="utf-8"))
        values["deployment"]["enabled"] = True
        values["bootstrap"].update({
            "enabled": True,
            "version": "preset-v1",
            "command": ["python", "-m", "app.bootstrap"],
        })
        values["ingress"]["enabled"] = True
        values["autoscaling"]["enabled"] = True
        values["metrics"]["enabled"] = True
        values["persistence"]["enabled"] = True
        documents = render(values)
        self.assertEqual(documents, [])


class AiImageUpdaterTest(unittest.TestCase):
    UPDATER = ROOT / "tools" / "update_ai_image.py"

    def run_updater(self, values: Path, tag: str, digest: str):
        return subprocess.run(
            [
                "python3",
                str(self.UPDATER),
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

    def test_updater_replaces_only_scaffold_image_fields_and_is_idempotent(self):
        source = (ROOT / "apps/dev/ai/values.yaml").read_text(encoding="utf-8")
        tag = "c" * 40
        digest = "sha256:" + "d" * 64
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(source, encoding="utf-8")
            first = self.run_updater(values, tag, digest)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout.strip(), "changed=true")
            updated = yaml.safe_load(values.read_text(encoding="utf-8"))
            self.assertEqual(updated["image"]["tag"], tag)
            self.assertEqual(updated["image"]["digest"], digest)
            self.assertFalse(updated["deployment"]["enabled"])
            self.assertFalse(updated["bootstrap"]["enabled"])
            expected_text = source.replace(
                "  tag: BLOCKED_UNVERIFIED_IMAGE\n", f"  tag: {tag}\n", 1
            ).replace("  digest: \"\"\n", f"  digest: {digest}\n", 1)
            self.assertEqual(values.read_text(encoding="utf-8"), expected_text)

            second = self.run_updater(values, tag, digest)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), "changed=false")
            self.assertEqual(values.read_text(encoding="utf-8"), expected_text)

    def test_updater_rejects_mutable_or_malformed_image_without_mutation(self):
        source = (ROOT / "apps/dev/ai/values.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text(source, encoding="utf-8")
            result = self.run_updater(values, "latest", "sha256:not-valid")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(values.read_text(encoding="utf-8"), source)


class AiImageWorkflowContractTest(unittest.TestCase):
    WORKFLOW = ROOT / ".github" / "workflows" / "ai-image-update.yaml"

    def test_workflow_is_fail_closed_pr_only_and_initially_manual_merge(self):
        workflow = yaml.load(self.WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"schedule", "workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["update"]
        self.assertIn("vars.AI_IMAGE_AUTOMATION_APPROVED == 'true'", job["if"])
        text = self.WORKFLOW.read_text(encoding="utf-8")
        required = (
            "Team-PinLog/ai",
            "ghcr.io/team-pinlog/ai",
            "AI_SOURCE_BRANCH",
            "AI_SOURCE_WORKFLOW",
            "AI_PROVENANCE_ARTIFACT",
            "AI_INFRA_JIRA_KEY",
            "PINLOG_AI_SOURCE_READER_TOKEN",
            "PINLOG_AI_INFRA_PR_TOKEN",
            "PINLOG_AI_IMAGE_UPDATER_USERNAME",
            "status=success",
            "event=push",
            "tools/update_ai_image.py",
            "gh run download",
            "provenance.json",
            "DOCKER_CONFIG",
            "docker logout",
            "apps/dev/ai/values.yaml",
            "automation/ai-image-update",
            "add-paths: apps/dev/ai/values.yaml",
            "초기 수동 merge",
        )
        for contract in required:
            self.assertIn(contract, text)
        self.assertNotIn("gh pr merge", text)
        self.assertNotIn("--auto", text)
        self.assertNotIn(":latest", text.lower())
        self.assertNotIn("PINLOG_AI_IMAGE_UPDATER_TOKEN", text)


class AiOperationsGateDocumentationTest(unittest.TestCase):
    DOCUMENT = ROOT / "docs" / "ai-serving.md"

    def test_runbook_records_blocked_contracts_activation_order_and_rollback(self):
        text = self.DOCUMENT.read_text(encoding="utf-8")
        required = (
            "ApplicationSet",
            "ai-dev",
            "application.enabled: false",
            "deployment.enabled: false",
            "bootstrap.enabled: false",
            "ghcr-ai-pull",
            "ai-runtime-secrets",
            "pinlog_dev",
            "Backend Flyway",
            "versioned/idempotent",
            "AI standalone smoke",
            "dev Backend E2E",
            "readiness",
            "metrics",
            "rollback",
            "AI_IMAGE_AUTOMATION_APPROVED",
            "AI_SOURCE_BRANCH",
            "AI_SOURCE_WORKFLOW",
            "AI_PROVENANCE_ARTIFACT",
            "PINLOG_AI_SOURCE_READER_TOKEN",
            "PINLOG_AI_INFRA_PR_TOKEN",
            "NetworkPolicy",
        )
        for contract in required:
            self.assertIn(contract, text)
        self.assertFalse(any((ROOT / "secrets/dev").glob("*ai*")))


if __name__ == "__main__":
    unittest.main()
