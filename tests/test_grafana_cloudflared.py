from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platform" / "grafana-cloudflared"
DEPLOYMENT = PLATFORM / "deployment.yaml"
KUSTOMIZATION = PLATFORM / "kustomization.yaml"
README = PLATFORM / "README.md"
ARGO_APP = ROOT / "argocd" / "apps" / "grafana-cloudflared.yaml"
VALUES = ROOT / "platform" / "monitoring" / "kube-prometheus-stack-values.yaml"
IMAGE = (
    "cloudflare/cloudflared:2026.7.2@"
    "sha256:4f6655284ab3d252b7f28fedb19fe6c8fc82ee5b1295c20ac74d475e5398a52d"
)


class GrafanaCloudflaredContractTest(unittest.TestCase):
    def test_connector_is_dedicated_pinned_hardened_and_uses_token_file(self):
        deployment = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
        self.assertEqual(deployment["kind"], "Deployment")
        self.assertEqual(deployment["metadata"]["name"], "grafana-cloudflared")
        self.assertEqual(deployment["metadata"]["namespace"], "monitoring")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})

        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("serviceAccountName", pod)
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        container = pod["containers"][0]
        self.assertEqual(container["image"], IMAGE)
        self.assertEqual(
            container["args"],
            ["tunnel", "--no-autoupdate", "--metrics", "0.0.0.0:2000", "run", "--token-file", "/etc/cloudflared/token"],
        )
        self.assertNotIn("env", container)
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe]["httpGet"], {"path": "/ready", "port": "metrics"})
        self.assertTrue(container["resources"]["requests"])
        self.assertTrue(container["resources"]["limits"])
        security = container["securityContext"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        token_volume = next(item for item in pod["volumes"] if item["name"] == "tunnel-token")
        self.assertEqual(
            token_volume["secret"],
            {
                "secretName": "grafana-cloudflared-token",
                "defaultMode": 0o440,
                "items": [{"key": "token", "path": "token"}],
            },
        )

    def test_public_hostname_and_direct_clusterip_contract_are_documented(self):
        text = README.read_text(encoding="utf-8")
        for value in (
            "monitoring.pin-log.com",
            "Type: `HTTP`",
            "Path: blank",
            "kube-prometheus-stack-grafana.monitoring.svc.cluster.local:80",
            "grafana-cloudflared-token",
            "secrets/monitoring/grafana-cloudflared-token.sealedsecret.yaml",
        ):
            self.assertIn(value, text)
        for forbidden in ("originServerName", "httpHostHeader", "No TLS Verify", "Traefik", "pinlog-web-cloudflared-token"):
            self.assertNotIn(forbidden, text)

    def test_grafana_canonical_url_changes_without_removing_legacy_ingress(self):
        values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        grafana = values["grafana"]
        server = grafana["grafana.ini"]["server"]
        self.assertEqual(server["root_url"], "https://monitoring.pin-log.com")
        self.assertFalse(server["serve_from_sub_path"])
        self.assertNotIn("enforce_domain", server)
        self.assertTrue(grafana["ingress"]["enabled"])
        self.assertEqual(grafana["ingress"]["hosts"], ["i15a705.p.ssafy.io"])
        self.assertEqual(grafana["ingress"]["path"], "/grafana")
        self.assertEqual(grafana["ingress"]["pathType"], "Prefix")
        self.assertEqual(grafana["ingress"]["tls"], [{"hosts": ["i15a705.p.ssafy.io"]}])

    def test_rollback_requires_root_url_revert_before_tunnel_shutdown(self):
        readme = README.read_text(encoding="utf-8")
        values = VALUES.read_text(encoding="utf-8")
        for text in (readme, values):
            self.assertIn("reviewed Git revert", text)
            self.assertIn("old root_url", text)
            self.assertIn("serve_from_sub_path=true", text)
            self.assertIn("not an instant live fallback", text)
        self.assertIn("before disabling the connector and Public Hostname", readme)
        self.assertIn("avoids recreating the route", readme)

    def test_argocd_root_and_monitoring_secret_permissions_cover_connector(self):
        app = yaml.safe_load(ARGO_APP.read_text(encoding="utf-8"))
        self.assertEqual(app["metadata"]["name"], "grafana-cloudflared")
        self.assertEqual(app["spec"]["project"], "pinlog")
        self.assertEqual(app["spec"]["source"]["path"], "platform/grafana-cloudflared")
        self.assertEqual(app["spec"]["destination"]["namespace"], "monitoring")
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["selfHeal"])

        root = yaml.safe_load((ROOT / "argocd/root/root-app.yaml").read_text(encoding="utf-8"))
        self.assertEqual(root["spec"]["source"]["path"], "argocd")
        self.assertTrue(root["spec"]["source"]["directory"]["recurse"])
        secrets = yaml.safe_load((ROOT / "argocd/apps/secrets-monitoring.yaml").read_text(encoding="utf-8"))
        self.assertEqual(secrets["spec"]["source"]["path"], "secrets/monitoring")
        self.assertEqual(secrets["spec"]["destination"]["namespace"], "monitoring")
        self.assertFalse(secrets["spec"]["syncPolicy"]["automated"]["prune"])
        project = yaml.safe_load((ROOT / "argocd/projects/pinlog.yaml").read_text(encoding="utf-8"))
        self.assertIn("monitoring", {item["namespace"] for item in project["spec"]["destinations"]})

    def test_kustomization_contains_only_connector_deployment(self):
        kustomization = yaml.safe_load(KUSTOMIZATION.read_text(encoding="utf-8"))
        self.assertEqual(kustomization["resources"], ["deployment.yaml"])


if __name__ == "__main__":
    unittest.main()
