from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "apps" / "dev" / "cowork" / "values.yaml"
PULL_SECRET = ROOT / "secrets" / "dev" / "ghcr-cowork-pull.sealedsecret.yaml"
API_SECRET = ROOT / "secrets" / "dev" / "cowork-api-credentials.sealedsecret.yaml"
SECRETS_APP = ROOT / "argocd" / "apps" / "secrets-dev.yaml"


class CoworkDevDeploymentTest(unittest.TestCase):
    def test_dev_values_pin_private_image_and_run_for_ticket_tunnel(self):
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        self.assertEqual(values["replicaCount"], 1)
        self.assertEqual(values["deploymentStrategy"], {"type": "Recreate"})
        self.assertEqual(values["image"]["repository"], "ghcr.io/team-pinlog/cowork")
        self.assertEqual(values["image"]["tag"], "e53b95498efb13cb3cd39f9cd7ce862766a9b74e")
        self.assertEqual(
            values["image"]["digest"],
            "sha256:08a9e9d30b10c68372e43783409903281baf0beb58093d67c23975ca7bc30de7",
        )
        self.assertEqual(values["imagePullSecrets"], [{"name": "ghcr-cowork-pull"}])
        self.assertFalse(values["ingress"]["enabled"])
        self.assertTrue(values["persistence"]["enabled"])
        self.assertEqual(values["persistence"]["mountPath"], "/data")
        self.assertEqual(values["persistence"]["storageClass"], "local-path-retain")
        self.assertNotIn("envFrom", values)

        env = {item["name"]: item["value"] for item in values["env"] if "value" in item}
        secret_env = {
            item["name"]: item["valueFrom"]["secretKeyRef"]
            for item in values["env"] if "valueFrom" in item
        }
        self.assertNotIn("COWORK_DB_PATH", env)
        self.assertEqual(env["COWORK_DATABASE_PATH"], "/data/cowork/cowork.db")
        self.assertNotIn("HERMES_HOME", env)
        self.assertNotIn("HERMES_PYTHON", env)
        self.assertEqual(env["COWORK_ENV"], "development")
        self.assertEqual(env["COWORK_COOKIE_SECURE"], "true")
        self.assertEqual(env["JIRA_PROJECT_KEY"], "S15P11A705")
        self.assertEqual(
            secret_env,
            {
                key: {"name": "cowork-api-credentials", "key": key}
                for key in ("GMS_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN")
            },
        )


    def test_dev_pull_secret_is_sealed_and_gitops_managed(self):
        secret = yaml.safe_load(PULL_SECRET.read_text(encoding="utf-8"))
        self.assertEqual(secret["kind"], "SealedSecret")
        self.assertEqual(secret["metadata"]["name"], "ghcr-cowork-pull")
        self.assertEqual(secret["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(set(secret["spec"]["encryptedData"]), {".dockerconfigjson"})
        self.assertNotIn("data", secret["spec"]["template"])
        self.assertNotIn("stringData", secret["spec"]["template"])

        application = yaml.safe_load(SECRETS_APP.read_text(encoding="utf-8"))
        self.assertEqual(application["spec"]["source"]["path"], "secrets/dev")
        self.assertEqual(application["spec"]["destination"]["namespace"], "pinlog-dev")
        self.assertFalse(application["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertTrue(application["spec"]["syncPolicy"]["automated"]["selfHeal"])

        api_secret = yaml.safe_load(API_SECRET.read_text(encoding="utf-8"))
        self.assertEqual(api_secret["kind"], "SealedSecret")
        self.assertEqual(api_secret["metadata"]["name"], "cowork-api-credentials")
        self.assertEqual(api_secret["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(
            set(api_secret["spec"]["encryptedData"]),
            {"GMS_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN"},
        )
        self.assertNotIn("data", api_secret["spec"]["template"])
        self.assertNotIn("stringData", api_secret["spec"]["template"])

    def test_dev_values_render_valid_singleton_resources(self):
        rendered = subprocess.run(
            [
                "helm", "template", "cowork", str(ROOT / "charts" / "microservice"),
                "--namespace", "pinlog-dev", "--values", str(VALUES),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [document for document in yaml.safe_load_all(rendered) if document]
        deployment = next(document for document in documents if document["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertEqual(
            deployment["spec"]["template"]["spec"]["imagePullSecrets"],
            [{"name": "ghcr-cowork-pull"}],
        )
        self.assertTrue(any(document["kind"] == "PersistentVolumeClaim" for document in documents))


if __name__ == "__main__":
    unittest.main()
