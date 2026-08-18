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
        self.assertEqual(metadata["version"], "0.2.1")

    def test_versioned_bootstrap_job_precedes_deployment_and_reuses_runtime_boundaries(self):
        values = {
            "image": {
                "repository": "ghcr.io/team-pinlog/ai",
                "tag": "a" * 40,
                "digest": "sha256:" + "b" * 64,
            },
            "imagePullSecrets": [{"name": "ghcr-ai-pull"}],
            "service": {"targetPort": 8000},
            "envFrom": [
                {"secretRef": {"name": "ai-owner-secrets"}},
                {"secretRef": {"name": "ai-db-credentials"}},
            ],
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
        self.assertEqual(container["envFrom"], values["envFrom"])
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
        self.assertEqual(values["application"], {"enabled": True})
        self.assertEqual(values["deployment"], {"enabled": True})
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
        self.assertEqual(values["probes"]["startup"]["path"], "/health")
        self.assertEqual(values["probes"]["liveness"]["path"], "/health")
        self.assertEqual(values["probes"]["readiness"]["path"], "/ready")
        self.assertTrue(values["probes"]["enabled"])
        self.assertFalse(values["metrics"]["enabled"])
        self.assertEqual(values["env"], [])
        self.assertEqual(values["securityContext"]["runAsUser"], 10001)
        self.assertTrue(values["securityContext"]["runAsNonRoot"])
        self.assertEqual(values["envFrom"], [
            {"secretRef": {"name": "ai-owner-secrets"}},
            {"secretRef": {"name": "ai-db-credentials"}},
        ])
        self.assertEqual(
            values["podAnnotations"]["provenance.pinlog.io/image-source-sha"],
            values["image"]["tag"],
        )
        self.assertEqual(
            values["podAnnotations"]["provenance.pinlog.io/owner-secret-source-sha"],
            "e09457b6304d00b48c1dd5b5755cba6fa505c3ee",
        )
        self.assertEqual(values["bootstrap"], {
            "enabled": True,
            "version": "preset-204824bd37e6",
            "command": ["python", "-m", "app.bootstrap.load_presets"],
            "backoffLimit": 1,
        })
        self.assertNotIn("nvidia.com/gpu", str(values))

    def test_phase2_renders_bootstrap_and_deployment(self):
        values = yaml.safe_load(self.VALUES.read_text(encoding="utf-8"))
        documents = render(values)
        self.assertEqual(sum(document["kind"] == "Job" for document in documents), 1)
        self.assertEqual(sum(document["kind"] == "Deployment" for document in documents), 1)
        deployment = next(document for document in documents if document["kind"] == "Deployment")
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(
            annotations["provenance.pinlog.io/image-source-sha"], values["image"]["tag"]
        )
        self.assertEqual(
            annotations["provenance.pinlog.io/owner-secret-source-sha"],
            "e09457b6304d00b48c1dd5b5755cba6fa505c3ee",
        )


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
            self.assertEqual(
                updated["podAnnotations"]["provenance.pinlog.io/image-source-sha"], tag
            )
            self.assertTrue(updated["deployment"]["enabled"])
            self.assertTrue(updated["application"]["enabled"])
            self.assertTrue(updated["bootstrap"]["enabled"])
            expected_lines = []
            for line in source.splitlines(keepends=True):
                if line.startswith("  tag:"):
                    line = f"  tag: {tag}\n"
                elif line.startswith("  digest:"):
                    line = f"  digest: {digest}\n"
                elif line.startswith("  provenance.pinlog.io/image-source-sha:"):
                    line = f"  provenance.pinlog.io/image-source-sha: {tag}\n"
                expected_lines.append(line)
            expected_text = "".join(expected_lines)
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
    AUTO_MERGE_WORKFLOW = ROOT / ".github" / "workflows" / "ai-image-auto-merge.yaml"

    def test_workflow_is_fail_closed_pr_only_and_reuses_the_proven_token(self):
        workflow = yaml.load(self.WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"schedule", "workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["detect-source"]
        self.assertIn("vars.AI_IMAGE_AUTOMATION_APPROVED == 'true'", job["if"])
        readonly_jobs = {
            name: workflow["jobs"][name]
            for name in ("detect-source", "verify-source-ci", "verify-image")
        }
        self.assertNotIn(
            "PINLOG_AI_INFRA_PR_TOKEN",
            yaml.safe_dump(readonly_jobs),
        )
        text = self.WORKFLOW.read_text(encoding="utf-8")
        required = (
            "Team-PinLog/ai",
            "ghcr.io/team-pinlog/ai",
            "AI_SOURCE_BRANCH",
            "AI_SOURCE_WORKFLOW",
            "AI_PROVENANCE_ARTIFACT",
            "AI_INFRA_JIRA_KEY",
            "PINLOG_IMAGE_UPDATER_TOKEN",
            "PINLOG_IMAGE_UPDATER_USERNAME",
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
            "필수 PR checks 성공 후 trusted workflow",
            "candidate=false",
            "gh pr close",
            "--disable-auto",
            "steps.change.outputs.changed == 'false'",
            "steps.create-pr.outcome != 'success'",
        )
        for contract in required:
            self.assertIn(contract, text)
        for approved_source_contract in (
            'test "$SOURCE_BRANCH" = main',
            'test "$SOURCE_WORKFLOW" = ai-ci.yml',
            'test "$PROVENANCE_ARTIFACT" = ai-image-provenance',
        ):
            self.assertIn(approved_source_contract, text)
        self.assertEqual(text.count("gh pr merge"), 1)
        self.assertNotIn("--squash", text)
        self.assertNotIn("--auto", text)
        self.assertNotIn(":latest", text.lower())
        self.assertNotIn("PINLOG_AI_SOURCE_READER_TOKEN", text)
        self.assertNotIn("PINLOG_AI_INFRA_PR_TOKEN", text)
        self.assertNotIn("PINLOG_AI_IMAGE_UPDATER_USERNAME", text)
        self.assertNotIn("PINLOG_AI_IMAGE_UPDATER_TOKEN", text)

    def test_trusted_auto_merge_is_exact_head_bound_and_ai_file_only(self):
        workflow = yaml.load(
            self.AUTO_MERGE_WORKFLOW.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(set(workflow["on"]), {"workflow_run"})
        self.assertEqual(
            workflow["permissions"],
            {"checks": "read", "contents": "write", "pull-requests": "write"},
        )
        condition = workflow["jobs"]["verify-and-merge"]["if"]
        self.assertIn("vars.AI_IMAGE_AUTO_MERGE_APPROVED == 'true'", condition)
        text = self.AUTO_MERGE_WORKFLOW.read_text(encoding="utf-8")
        for contract in (
            "automation/ai-image-update",
            "apps/dev/ai/values.yaml",
            "Team-PinLog/ai",
            "ai-ci.yml",
            "ai-image-provenance",
            "PINLOG_IMAGE_UPDATER_TOKEN",
            "PINLOG_IMAGE_UPDATER_USERNAME",
            "REQUIRED_CHECKS: guardrails helm pr-policy",
            "--match-head-commit",
            "--squash --delete-branch",
        ):
            self.assertIn(contract, text)
        self.assertNotIn("workflow_run.pull_requests[0]", text)
        self.assertNotIn("--auto", text)
        self.assertNotIn("PINLOG_AI_SOURCE_READER_TOKEN", text)
        self.assertNotIn("PINLOG_AI_INFRA_PR_TOKEN", text)


class AiOperationsGateDocumentationTest(unittest.TestCase):
    DOCUMENT = ROOT / "docs" / "ai-serving.md"
    PULL_SECRET = ROOT / "secrets" / "dev" / "ghcr-ai-pull.sealedsecret.yaml"

    def test_runbook_records_blocked_contracts_activation_order_and_rollback(self):
        text = self.DOCUMENT.read_text(encoding="utf-8")
        required = (
            "ApplicationSet",
            "ai-dev",
            "application.enabled: true",
            "deployment.enabled: true",
            "bootstrap.enabled: true",
            "ghcr-ai-pull",
            "ai-owner-secrets",
            "ai-db-credentials",
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
            "PINLOG_IMAGE_UPDATER_TOKEN",
            "AI_IMAGE_AUTO_MERGE_APPROVED=true",
            "NetworkPolicy",
            "python -m app.bootstrap.load_presets",
            "preset-204824bd37e6",
            "204824bd37e6e1f056f1636ec1bb86d2585994a8cdbfd99bb188096cfca04034",
            "27 presets",
            "pinlog_ai_dev",
            "김세민",
            "text-embedding-3-small",
            "1536",
            "cosine",
            "openai-text-embedding-3-small-1536-cosine-v1",
            "30428472911",
            "299f6a6435f4f4c92cad59fa8eca4bacdf1e597e",
            "sha256:a02cb48b84b1cb474d4cdaa7c9aa6a2e99f1162b16d324b396fcfcfcf0dae101",
            "30431247125",
            "ai-owner-secrets-sealed",
            "Team-PinLog/ai",
            "cc7753c6a32e6fe12bee694b4ca8004c8a8a4cbc",
            "sha256:57e2845efd62e7ba5c857ff39d1d4d59974908c06c0885fcf6aa50870626a8a3",
            "--spring.main.web-application-type=none",
            "--spring.main.banner-mode=off",
            "65.024s",
            "exit 0",
            "V1 → V2 → V3 → V100 → V101 → V102",
            "AI_SOURCE_BRANCH=main",
            "AI_SOURCE_WORKFLOW=ai-ci.yml",
            "AI_PROVENANCE_ARTIFACT=ai-image-provenance",
            "bootstrap 데이터는 단순 image revert만으로 복원되지 않는다",
        )
        for contract in required:
            self.assertIn(contract, text)
        self.assertNotIn("ai-runtime-secrets", text)
        prerequisites = (ROOT / "docs/ai-dev-prerequisites.md").read_text(encoding="utf-8")
        self.assertNotIn("ai-runtime-secrets", prerequisites)
        db_secret = ROOT / "secrets/dev/ai-db-credentials.sealedsecret.yaml"
        ai_secret_paths = sorted((ROOT / "secrets/dev").glob("*ai*"))
        owner_secret = ROOT / "secrets/dev/ai-owner-secrets.sealedsecret.yaml"
        self.assertEqual(
            ai_secret_paths,
            sorted([db_secret, owner_secret, self.PULL_SECRET]),
        )
        self.assertFalse(
            (ROOT / "secrets/dev/ai-runtime-secrets.sealedsecret.yaml").exists()
        )

    def test_ai_runtime_secrets_are_ciphertext_only_strict_and_split(self):
        path = ROOT / "secrets/dev/ai-db-credentials.sealedsecret.yaml"
        sealed = yaml.safe_load(path.read_text(encoding="utf-8"))
        expected_metadata = {"name": "ai-db-credentials", "namespace": "pinlog-dev"}
        self.assertEqual(sealed["metadata"], expected_metadata)
        self.assertEqual(sealed["spec"]["template"]["metadata"], expected_metadata)
        self.assertEqual(set(sealed["spec"]["encryptedData"]), {"DATABASE_URL"})
        self.assertNotIn("data", sealed)
        self.assertNotIn("stringData", sealed)
        self.assertNotIn("data", sealed["spec"]["template"])
        self.assertNotIn("stringData", sealed["spec"]["template"])

        owner_path = ROOT / "secrets/dev/ai-owner-secrets.sealedsecret.yaml"
        owner = yaml.safe_load(owner_path.read_text(encoding="utf-8"))
        owner_metadata = {"name": "ai-owner-secrets", "namespace": "pinlog-dev"}
        owner_keys = {
            "GMS_API_KEY",
            "GMS_BASE_URL",
            "INTERNAL_SHARED_SECRET",
            "KAKAO_REST_API_KEY",
            "PINLOG_EMBEDDING_MODEL",
            "PINLOG_EMBEDDING_DIMENSION",
            "PINLOG_EMBEDDING_DISTANCE",
            "PINLOG_EMBEDDING_PROFILE",
        }
        self.assertEqual(
            {key: value for key, value in owner["metadata"].items() if key != "annotations"},
            owner_metadata,
        )
        self.assertEqual(
            {
                key: value
                for key, value in owner["spec"]["template"]["metadata"].items()
                if key != "annotations"
            },
            owner_metadata,
        )
        self.assertEqual(set(owner["spec"]["encryptedData"]), owner_keys)
        self.assertTrue(all(
            value.startswith("Ag") and len(value) >= 100
            for value in owner["spec"]["encryptedData"].values()
        ))
        self.assertNotIn("data", owner)
        self.assertNotIn("stringData", owner)
        self.assertNotIn("data", owner["spec"]["template"])
        self.assertNotIn("stringData", owner["spec"]["template"])

    def test_ai_pull_secret_is_encrypted_and_scoped_to_dev(self):
        sealed = yaml.safe_load(self.PULL_SECRET.read_text(encoding="utf-8"))
        self.assertEqual(sealed["apiVersion"], "bitnami.com/v1alpha1")
        self.assertEqual(sealed["kind"], "SealedSecret")
        self.assertEqual(
            sealed["metadata"],
            {"name": "ghcr-ai-pull", "namespace": "pinlog-dev"},
        )
        self.assertEqual(
            sealed["spec"]["template"]["metadata"],
            {"name": "ghcr-ai-pull", "namespace": "pinlog-dev"},
        )
        self.assertEqual(
            sealed["spec"]["template"]["type"],
            "kubernetes.io/dockerconfigjson",
        )
        self.assertEqual(set(sealed["spec"]["encryptedData"]), {".dockerconfigjson"})
        self.assertNotIn("data", sealed)
        self.assertNotIn("stringData", sealed)
        self.assertNotIn("data", sealed["spec"]["template"])
        self.assertNotIn("stringData", sealed["spec"]["template"])


if __name__ == "__main__":
    unittest.main()
