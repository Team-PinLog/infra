from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "microservice"


class StatefulMicroserviceChartTest(unittest.TestCase):
    def test_renders_recreate_digest_pvc_and_non_root_volume_access(self):
        values = {
            "image": {
                "repository": "ghcr.io/team-pinlog/cowork",
                "tag": "1e38ec2b1631373679c5715515948231cd8de5e1",
                "digest": "sha256:" + "a" * 64,
            },
            "deploymentStrategy": {"type": "Recreate"},
            "podSecurityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "fsGroupChangePolicy": "OnRootMismatch",
            },
            "persistence": {
                "enabled": True,
                "mountPath": "/data",
                "size": "1Gi",
                "storageClass": "local-path-retain",
                "accessModes": ["ReadWriteOnce"],
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as values_file:
            yaml.safe_dump(values, values_file)
            values_file.flush()
            rendered = subprocess.run(
                [
                    "helm",
                    "template",
                    "cowork",
                    str(CHART),
                    "--namespace",
                    "pinlog-prod",
                    "--values",
                    values_file.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        documents = [document for document in yaml.safe_load_all(rendered) if document]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        claim = next(doc for doc in documents if doc["kind"] == "PersistentVolumeClaim")
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]

        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertEqual(
            container["image"],
            "ghcr.io/team-pinlog/cowork:1e38ec2b1631373679c5715515948231cd8de5e1@sha256:"
            + "a" * 64,
        )
        self.assertEqual(pod_spec["securityContext"]["fsGroup"], 1000)
        self.assertEqual(container["volumeMounts"], [{"name": "data", "mountPath": "/data"}])
        self.assertEqual(
            pod_spec["volumes"],
            [{"name": "data", "persistentVolumeClaim": {"claimName": "cowork"}}],
        )
        self.assertEqual(claim["metadata"]["name"], "cowork")
        self.assertEqual(claim["spec"]["storageClassName"], "local-path-retain")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "1Gi")


if __name__ == "__main__":
    unittest.main()
