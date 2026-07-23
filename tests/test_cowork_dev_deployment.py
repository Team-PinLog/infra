from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "apps" / "dev" / "cowork" / "values.yaml"
PULL_SECRET = ROOT / "secrets" / "dev" / "ghcr-cowork-pull.sealedsecret.yaml"
SECRETS_APP = ROOT / "argocd" / "apps" / "secrets-dev.yaml"


class CoworkDevDeploymentTest(unittest.TestCase):
    def test_dev_values_pin_private_image_and_run_for_ticket_tunnel(self):
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        self.assertEqual(values["replicaCount"], 1)
        self.assertEqual(values["deploymentStrategy"], {"type": "Recreate"})
        self.assertEqual(values["image"]["repository"], "ghcr.io/team-pinlog/cowork")
        self.assertEqual(values["image"]["tag"], "0a9952e99208845a28214bc1507f00d971e14e7a")
        self.assertEqual(
            values["image"]["digest"],
            "sha256:7b25b37eca172f7a9d9845c771ec485534312810c364dccd6272b38223b7c8ff",
        )
        self.assertEqual(values["imagePullSecrets"], [{"name": "ghcr-cowork-pull"}])
        self.assertFalse(values["ingress"]["enabled"])
        self.assertTrue(values["persistence"]["enabled"])
        self.assertEqual(values["persistence"]["mountPath"], "/data")
        self.assertEqual(values["persistence"]["storageClass"], "local-path-retain")
        self.assertNotIn("envFrom", values)

        env = {item["name"]: item["value"] for item in values["env"]}
        self.assertNotIn("COWORK_DB_PATH", env)
        self.assertEqual(env["COWORK_DATABASE_PATH"], "/data/cowork/cowork.db")
        self.assertEqual(env["HERMES_HOME"], "/data/cowork/hermes")
        self.assertEqual(env["XDG_CACHE_HOME"], "/data/cowork/cache")
        self.assertEqual(env["COWORK_ENV"], "development")
        self.assertEqual(env["COWORK_COOKIE_SECURE"], "true")
        self.assertEqual(env["JIRA_PROJECT_KEY"], "S15P11A705")


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
