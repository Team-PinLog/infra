import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platform" / "pinlog-web-domain"
DEPLOYMENT = PLATFORM / "deployment.yaml"
INGRESSES = PLATFORM / "ingress.yaml"
README = PLATFORM / "README.md"
ARGO_APP = ROOT / "argocd" / "apps" / "pinlog-web-domain.yaml"
SEALED_SECRET = ROOT / "secrets" / "dev" / "pinlog-web-cloudflared-token.sealedsecret.yaml"
CLOUDFLARED_IMAGE = (
    "cloudflare/cloudflared:2026.7.2@"
    "sha256:4f6655284ab3d252b7f28fedb19fe6c8fc82ee5b1295c20ac74d475e5398a52d"
)


def load_documents(path):
    return [document for document in yaml.safe_load_all(path.read_text(encoding="utf-8")) if document]


def ingress_paths(document):
    return {
        path["path"]: path["backend"]["service"]["name"]
        for rule in document["spec"]["rules"]
        for path in rule["http"]["paths"]
    }


class PinlogWebDomainContractTest(unittest.TestCase):
    def test_cloudflared_is_independent_pinned_hardened_and_gated(self):
        deployment = yaml.safe_load(DEPLOYMENT.read_text(encoding="utf-8"))
        self.assertEqual(deployment["kind"], "Deployment")
        self.assertEqual(deployment["metadata"]["name"], "pinlog-web-cloudflared")
        self.assertEqual(deployment["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(deployment["spec"]["replicas"], 1)

        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("serviceAccountName", pod)
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")

        container = pod["containers"][0]
        self.assertEqual(container["image"], CLOUDFLARED_IMAGE)
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
        self.assertEqual(container["ports"][0], {"name": "metrics", "containerPort": 2000, "protocol": "TCP"})
        for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(
                container[probe_name]["httpGet"],
                {"path": "/ready", "port": "metrics"},
            )
        self.assertTrue(container["resources"]["requests"])
        self.assertTrue(container["resources"]["limits"])
        security = container["securityContext"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])

        token_volume = next(volume for volume in pod["volumes"] if volume["name"] == "tunnel-token")
        self.assertEqual(
            token_volume["secret"],
            {
                "secretName": "pinlog-web-cloudflared-token",
                "defaultMode": 0o440,
                "items": [{"key": "token", "path": "token"}],
            },
        )

    def test_new_host_routes_only_approved_services_in_their_namespaces(self):
        ingresses = load_documents(INGRESSES)
        self.assertEqual(len(ingresses), 2)
        self.assertEqual({item["kind"] for item in ingresses}, {"Ingress"})
        by_namespace = {item["metadata"]["namespace"]: item for item in ingresses}
        self.assertEqual(set(by_namespace), {"pinlog-dev", "pinlog-prod"})

        dev = by_namespace["pinlog-dev"]
        prod = by_namespace["pinlog-prod"]
        self.assertEqual(dev["spec"]["ingressClassName"], "traefik")
        self.assertEqual(prod["spec"]["ingressClassName"], "traefik")
        self.assertEqual(dev["spec"]["rules"][0]["host"], "pin-log.com")
        self.assertEqual(prod["spec"]["rules"][0]["host"], "pin-log.com")
        self.assertEqual(ingress_paths(dev), {"/": "front", "/api-console": "api-console"})
        self.assertEqual(ingress_paths(prod), {"/api/core": "back"})
        for ingress in ingresses:
            self.assertEqual(ingress["spec"]["tls"], [{"hosts": ["pin-log.com"]}])
            for rule in ingress["spec"]["rules"]:
                for path in rule["http"]["paths"]:
                    self.assertEqual(path["pathType"], "Prefix")
                    self.assertEqual(path["backend"]["service"]["port"]["number"], 80)

        text = INGRESSES.read_text(encoding="utf-8").lower()
        self.assertNotRegex(text, r"(?:^|[/\s-])(ai|grafana)(?:$|[/\s:.-])")
        self.assertNotIn("stripprefix", text)
        self.assertNotIn("middlewares", text)

    def test_existing_ssafy_ingresses_remain_enabled(self):
        front = yaml.safe_load((ROOT / "apps/dev/front/values.yaml").read_text(encoding="utf-8"))
        back = yaml.safe_load((ROOT / "apps/prod/back/values.yaml").read_text(encoding="utf-8"))
        console = yaml.safe_load((ROOT / "platform/api-console-dev/ingress.yaml").read_text(encoding="utf-8"))
        self.assertEqual(front["ingress"]["host"], "i15a705.p.ssafy.io")
        self.assertTrue(front["ingress"]["enabled"])
        self.assertEqual(back["ingress"]["host"], "i15a705.p.ssafy.io")
        self.assertTrue(back["ingress"]["enabled"])
        self.assertEqual(console["spec"]["rules"][0]["host"], "i15a705.p.ssafy.io")

    def test_argocd_application_uses_existing_project_and_root_discovery(self):
        app = yaml.safe_load(ARGO_APP.read_text(encoding="utf-8"))
        self.assertEqual(app["metadata"]["name"], "pinlog-web-domain")
        self.assertEqual(app["metadata"]["namespace"], "argocd")
        self.assertEqual(
            app["metadata"].get("finalizers"),
            ["resources-finalizer.argocd.argoproj.io"],
        )
        self.assertEqual(app["spec"]["project"], "pinlog")
        self.assertEqual(app["spec"]["source"]["path"], "platform/pinlog-web-domain")
        self.assertEqual(app["spec"]["source"]["targetRevision"], "main")
        self.assertEqual(app["spec"]["destination"]["namespace"], "pinlog-dev")
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["selfHeal"])

        root = yaml.safe_load((ROOT / "argocd/root/root-app.yaml").read_text(encoding="utf-8"))
        self.assertEqual(root["spec"]["source"]["path"], "argocd")
        self.assertTrue(root["spec"]["source"]["directory"]["recurse"])
        project = yaml.safe_load((ROOT / "argocd/projects/pinlog.yaml").read_text(encoding="utf-8"))
        allowed = {item["namespace"] for item in project["spec"]["destinations"]}
        self.assertTrue({"pinlog-dev", "pinlog-prod", "argocd"}.issubset(allowed))

    def test_readme_documents_dashboard_handoff_activation_rollback_and_smoke(self):
        text = README.read_text(encoding="utf-8")
        for contract in (
            "pin-log-service",
            "https://traefik.kube-system.svc.cluster.local:443",
            "Origin Server Name",
            "i15a705.p.ssafy.io",
            "HTTP Host Header",
            "pin-log.com",
            "No TLS Verify",
            "false",
            "pinlog-web-cloudflared-token",
            "token",
            "replicas: 1",
            "strict-scoped SealedSecret",
            "activation",
            "rollback",
            "smoke",
        ):
            self.assertIn(contract, text)
        self.assertRegex(text, r"(?i)(hidden|평문.*금지|노출.*금지)")
        self.assertNotRegex(text, r"(?i)(encryptedData|stringData|placeholder|<token>|TOKEN=)")

    def test_connector_token_is_strict_scoped_encrypted_sealedsecret(self):
        self.assertTrue(SEALED_SECRET.is_file())
        sealed = yaml.safe_load(SEALED_SECRET.read_text(encoding="utf-8"))
        self.assertEqual(sealed["apiVersion"], "bitnami.com/v1alpha1")
        self.assertEqual(sealed["kind"], "SealedSecret")
        self.assertEqual(sealed["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(sealed["metadata"]["name"], "pinlog-web-cloudflared-token")
        self.assertEqual(set(sealed["spec"]["encryptedData"]), {"token"})
        template = sealed["spec"]["template"]
        self.assertEqual(template["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(template["metadata"]["name"], "pinlog-web-cloudflared-token")
        annotations = template["metadata"].get("annotations", {})
        self.assertNotEqual(annotations.get("sealedsecrets.bitnami.com/cluster-wide"), "true")
        self.assertNotEqual(annotations.get("sealedsecrets.bitnami.com/namespace-wide"), "true")

    def test_rollback_is_ordered_and_credentials_require_separate_retirement(self):
        text = README.read_text(encoding="utf-8")
        stage_one = text.find("1단계")
        stage_two = text.find("2단계")
        self.assertNotEqual(stage_one, -1)
        self.assertNotEqual(stage_two, -1)
        self.assertLess(stage_one, stage_two)
        for contract in (
            "replicas: 0",
            "Deployment/Ingress 삭제 확인",
            "Application 제거",
            "secrets-dev",
            "prune: false",
            "별도 승인",
        ):
            self.assertIn(contract, text)

        secrets_app = yaml.safe_load(
            (ROOT / "argocd" / "apps" / "secrets-dev.yaml").read_text(encoding="utf-8")
        )
        self.assertFalse(secrets_app["spec"]["syncPolicy"]["automated"]["prune"])

    def test_cowork_tunnel_contract_is_not_reused(self):
        deployment_text = DEPLOYMENT.read_text(encoding="utf-8")
        self.assertNotIn("cowork-cloudflared", deployment_text)
        self.assertNotIn("cowork-cloudflared-token", deployment_text)
        cowork = yaml.safe_load((ROOT / "platform/cowork-cloudflared-dev/deployment.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cowork["metadata"]["name"], "cowork-cloudflared")
        self.assertEqual(cowork["spec"]["replicas"], 1)


if __name__ == "__main__":
    unittest.main()
