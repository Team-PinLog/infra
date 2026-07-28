import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "microservice"
VALUES = ROOT / "apps" / "prod" / "back" / "values.yaml"
PULL_SECRET = ROOT / "secrets" / "prod" / "ghcr-back-pull.sealedsecret.yaml"
TAG_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BackendGitOpsTest(unittest.TestCase):
    def test_backend_values_and_private_pull_secret_are_declared(self):
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        self.assertEqual(chart["version"], "0.2.0")
        values = yaml.safe_load(VALUES.read_text())
        self.assertEqual(values["replicaCount"], 1)
        image = values["image"]
        self.assertEqual(image["repository"], "ghcr.io/team-pinlog/back")
        self.assertRegex(image["tag"], TAG_RE)
        self.assertRegex(image["digest"], DIGEST_RE)
        self.assertEqual(image["pullPolicy"], "IfNotPresent")
        self.assertEqual(values["imagePullSecrets"], [{"name": "ghcr-back-pull"}])
        self.assertEqual(
            values["deploymentStrategy"],
            {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
            },
        )
        self.assertEqual(values["service"], {"type": "ClusterIP", "port": 80, "targetPort": 8080})
        self.assertFalse(values["ingress"]["enabled"])
        self.assertEqual(values["terminationGracePeriodSeconds"], 40)
        self.assertEqual(values["lifecycle"]["preStop"]["exec"]["command"], ["sh", "-c", "sleep 5"])

        probes = values["probes"]
        self.assertTrue(probes["enabled"])
        self.assertEqual(probes["startup"]["path"], "/api/core/actuator/health/liveness")
        self.assertEqual(probes["liveness"]["path"], "/api/core/actuator/health/liveness")
        self.assertEqual(probes["readiness"]["path"], "/api/core/actuator/health/readiness")
        self.assertTrue(values["metrics"]["enabled"])
        self.assertEqual(values["metrics"]["path"], "/api/core/actuator/prometheus")

        env = {item["name"]: item for item in values["env"]}
        self.assertEqual(env["SPRING_DATASOURCE_URL"]["value"], "jdbc:postgresql://postgres:5432/pinlog")
        self.assertEqual(env["SPRING_DATASOURCE_USERNAME"]["value"], "pinlog")
        self.assertEqual(
            env["SPRING_DATASOURCE_PASSWORD"]["valueFrom"]["secretKeyRef"],
            {"name": "postgres-credentials", "key": "password"},
        )
        self.assertEqual(env["SPRING_DATA_REDIS_HOST"]["value"], "redis")
        self.assertEqual(env["SPRING_DATA_REDIS_PORT"]["value"], "6379")

        sealed = yaml.safe_load(PULL_SECRET.read_text())
        self.assertEqual(sealed["kind"], "SealedSecret")
        self.assertEqual(sealed["metadata"], {"name": "ghcr-back-pull", "namespace": "pinlog-prod"})
        self.assertEqual(
            sealed["spec"]["template"]["metadata"],
            {"name": "ghcr-back-pull", "namespace": "pinlog-prod"},
        )
        self.assertEqual(sealed["spec"]["template"]["type"], "kubernetes.io/dockerconfigjson")
        self.assertEqual(set(sealed["spec"]["encryptedData"]), {".dockerconfigjson"})

    def test_exact_render_separates_probes_for_internal_singleton(self):
        values = yaml.safe_load(VALUES.read_text())
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "back",
                str(CHART),
                "--namespace",
                "pinlog-prod",
                "--values",
                str(VALUES),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [document for document in yaml.safe_load_all(rendered) if document]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        service = next(doc for doc in documents if doc["kind"] == "Service")
        monitor = next(doc for doc in documents if doc["kind"] == "ServiceMonitor")
        self.assertFalse(any(doc["kind"] == "Ingress" for doc in documents))
        self.assertFalse(any(doc["kind"] == "HorizontalPodAutoscaler" for doc in documents))

        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(
            deployment["spec"]["strategy"],
            {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        self.assertEqual(
            container["image"],
            f"{values['image']['repository']}:{values['image']['tag']}@{values['image']['digest']}",
        )
        self.assertEqual(pod_spec["imagePullSecrets"], [{"name": "ghcr-back-pull"}])
        self.assertEqual(pod_spec["terminationGracePeriodSeconds"], 40)
        self.assertEqual(container["lifecycle"]["preStop"]["exec"]["command"], ["sh", "-c", "sleep 5"])
        self.assertEqual(container["startupProbe"]["httpGet"]["path"], "/api/core/actuator/health/liveness")
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/api/core/actuator/health/liveness")
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/api/core/actuator/health/readiness")
        self.assertEqual(
            service["spec"]["ports"][0],
            {"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"},
        )
        self.assertEqual(monitor["spec"]["endpoints"][0]["path"], "/api/core/actuator/prometheus")

    def test_legacy_single_probe_path_remains_backward_compatible(self):
        values = {
            "image": {"repository": "example.invalid/legacy", "tag": "test"},
            "probes": {"enabled": True, "path": "/legacy-health"},
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as values_file:
            yaml.safe_dump(values, values_file)
            values_file.flush()
            rendered = subprocess.run(
                ["helm", "template", "legacy", str(CHART), "--values", values_file.name],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        documents = [document for document in yaml.safe_load_all(rendered) if document]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["startupProbe"]["httpGet"]["path"], "/legacy-health")
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/legacy-health")
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/legacy-health")


if __name__ == "__main__":
    unittest.main()
