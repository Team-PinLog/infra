from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "apps" / "dev" / "cowork" / "values.yaml"
TUNNEL = ROOT / "platform" / "cowork-cloudflared-dev" / "deployment.yaml"
ARGO_APP = ROOT / "argocd" / "apps" / "cowork-cloudflared-dev.yaml"
SEALED_SECRET = ROOT / "secrets" / "dev" / "cowork-cloudflared-token.sealedsecret.yaml"


def load_documents(path):
    return [document for document in yaml.safe_load_all(path.read_text(encoding="utf-8")) if document]


class CoworkCloudflaredDevTest(unittest.TestCase):
    def test_cowork_is_active_with_secure_external_cookie(self):
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        self.assertEqual(values["replicaCount"], 1)
        self.assertFalse(values["ingress"]["enabled"])
        env = {item["name"]: item["value"] for item in values["env"]}
        self.assertEqual(env["COWORK_COOKIE_SECURE"], "true")

    def test_tunnel_is_pinned_non_root_and_reads_token_from_file(self):
        deployment = next(
            document for document in load_documents(TUNNEL) if document["kind"] == "Deployment"
        )
        self.assertEqual(deployment["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        self.assertEqual(
            container["image"],
            "cloudflare/cloudflared:2026.7.2@sha256:4f6655284ab3d252b7f28fedb19fe6c8fc82ee5b1295c20ac74d475e5398a52d",
        )
        self.assertEqual(
            container["args"],
            [
                "tunnel",
                "--no-autoupdate",
                "--metrics",
                "0.0.0.0:2000",
                "run",
                "--token-file",
                "/etc/cloudflared/token",
            ],
        )
        self.assertNotIn("env", container)
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/ready", "port": "metrics"})
        self.assertEqual(container["livenessProbe"]["httpGet"], {"path": "/ready", "port": "metrics"})
        token_volume = next(volume for volume in pod["volumes"] if volume["name"] == "tunnel-token")
        self.assertEqual(
            token_volume["secret"],
            {"secretName": "cowork-cloudflared-token", "defaultMode": 0o440},
        )

    def test_argocd_and_sealed_secret_manage_the_tunnel(self):
        app = yaml.safe_load(ARGO_APP.read_text(encoding="utf-8"))
        self.assertEqual(app["spec"]["source"]["path"], "platform/cowork-cloudflared-dev")
        self.assertEqual(app["spec"]["destination"]["namespace"], "pinlog-dev")
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["selfHeal"])

        secret = yaml.safe_load(SEALED_SECRET.read_text(encoding="utf-8"))
        self.assertEqual(secret["kind"], "SealedSecret")
        self.assertEqual(secret["metadata"]["name"], "cowork-cloudflared-token")
        self.assertEqual(secret["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(set(secret["spec"]["encryptedData"]), {"token"})
        self.assertNotIn("data", secret["spec"]["template"])
        self.assertNotIn("stringData", secret["spec"]["template"])


if __name__ == "__main__":
    unittest.main()
