from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "microservice"
VALUES = ROOT / "apps" / "dev" / "ai" / "values.yaml"
SECRET_REFS = [
    {"secretRef": {"name": "ai-owner-secrets"}},
    {"secretRef": {"name": "ai-db-credentials"}},
]


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


class AiDevActivationPhase1Test(unittest.TestCase):
    def setUp(self):
        self.values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))

    def test_phase2_renders_ordered_bootstrap_and_deployment(self):
        self.assertEqual(self.values["application"], {"enabled": True})
        self.assertEqual(self.values["bootstrap"]["enabled"], True)
        self.assertEqual(self.values["deployment"], {"enabled": True})
        image = self.values["image"]
        self.assertEqual(image["repository"], "ghcr.io/team-pinlog/ai")
        self.assertRegex(image["tag"], r"^[0-9a-f]{40}$")
        self.assertRegex(image["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            self.values["podAnnotations"]["provenance.pinlog.io/image-source-sha"],
            image["tag"],
        )
        expected_image = f'{image["repository"]}:{image["tag"]}@{image["digest"]}'

        documents = render(self.values)
        self.assertIn("Deployment", {document["kind"] for document in documents})
        service_account = next(
            document for document in documents if document["kind"] == "ServiceAccount"
        )
        self.assertEqual(
            service_account["metadata"]["annotations"],
            {
                "argocd.argoproj.io/sync-wave": "-1",
                "argocd.argoproj.io/hook": "PreSync",
                "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation",
            },
        )
        jobs = [document for document in documents if document["kind"] == "Job"]
        self.assertEqual(len(jobs), 1)

        job = jobs[0]
        self.assertEqual(job["metadata"]["name"], "ai-bootstrap-preset-204824bd37e6")
        self.assertEqual(
            job["metadata"]["annotations"],
            {
                "argocd.argoproj.io/sync-wave": "0",
                "argocd.argoproj.io/hook": "PreSync",
                "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation",
            },
        )
        pod_spec = job["spec"]["template"]["spec"]
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "ghcr-ai-pull"}])
        container = pod_spec["containers"][0]
        self.assertEqual(container["image"], expected_image)
        self.assertEqual(container["envFrom"], SECRET_REFS)
        self.assertEqual(
            container["command"], ["python", "-m", "app.bootstrap.load_presets"]
        )

    def test_phase1_preserves_cpu_security_and_network_boundaries(self):
        self.assertEqual(
            self.values["resources"],
            {
                "requests": {"cpu": "100m", "memory": "384Mi"},
                "limits": {"cpu": "500m", "memory": "768Mi"},
            },
        )
        self.assertEqual(
            self.values["securityContext"],
            {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
        )
        self.assertEqual(self.values["ingress"], {"enabled": False})
        self.assertNotIn("nvidia.com/gpu", str(self.values))

    def test_rollback_closes_bootstrap_and_application_gates(self):
        rollback = deepcopy(self.values)
        rollback["deployment"]["enabled"] = False
        rollback["application"]["enabled"] = False
        rollback["bootstrap"]["enabled"] = False
        self.assertEqual(rollback["deployment"], {"enabled": False})
        self.assertEqual(render(rollback), [])


if __name__ == "__main__":
    unittest.main()
