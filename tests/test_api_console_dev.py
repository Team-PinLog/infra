import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platform" / "api-console-dev"


def load(name):
    return yaml.safe_load((PLATFORM / name).read_text(encoding="utf-8"))


class ApiConsoleDevContractTest(unittest.TestCase):
    def test_manifests_exist_and_have_expected_kinds(self):
        expected = {
            "configmap.yaml": "ConfigMap",
            "deployment.yaml": "Deployment",
            "service.yaml": "Service",
            "ingress.yaml": "Ingress",
        }
        self.assertEqual({path.name for path in PLATFORM.glob("*.yaml")}, set(expected))
        for name, kind in expected.items():
            self.assertEqual(load(name)["kind"], kind)

    def test_swagger_ui_uses_same_origin_cookie_and_csrf_without_persistence(self):
        config = load("configmap.yaml")
        self.assertEqual(config["metadata"]["namespace"], "pinlog-dev")
        script = config["data"]["swagger-initializer.js"]
        self.assertIn("/api/core/v3/api-docs", script)
        self.assertRegex(script, r"credentials\s*=\s*['\"]include['\"]")
        self.assertIn("XSRF-TOKEN", script)
        self.assertIn("X-XSRF-TOKEN", script)
        self.assertIn("persistAuthorization: false", script)
        self.assertNotRegex(script, r"(?i)localStorage|sessionStorage|access_token|refresh_token")

    def test_late_entrypoint_rebuilds_gzip_after_custom_initializer(self):
        config = load("configmap.yaml")
        entrypoint = config["data"]["50-pinlog-config.sh"]
        raw_path = "/usr/share/nginx/html/swagger-initializer.js"
        gzip_path = f"{raw_path}.gz"
        self.assertIn(f"cp /pinlog-config/swagger-initializer.js {raw_path}", entrypoint)
        self.assertIn(gzip_path, entrypoint)
        self.assertRegex(entrypoint, r"gzip\s+-[0-9]+\s+-c")
        self.assertLess(entrypoint.index(raw_path), entrypoint.index(gzip_path))

    def test_deployment_is_minimal_pinned_and_hardened(self):
        deployment = load("deployment.yaml")
        self.assertEqual(deployment["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        pod = deployment["spec"]["template"]
        self.assertEqual(pod["metadata"]["labels"]["networking.pinlog.io/ingress"], "true")
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        self.assertNotIn("serviceAccountName", pod["spec"])
        container = pod["spec"]["containers"][0]
        self.assertRegex(container["image"], r"^swaggerapi/swagger-ui:[^@]+@sha256:[0-9a-f]{64}$")
        self.assertEqual(container["ports"][0], {"name": "http", "containerPort": 8080})
        security = container["securityContext"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        self.assertTrue(container["resources"]["requests"])
        self.assertTrue(container["resources"]["limits"])
        text = (PLATFORM / "deployment.yaml").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?i)secret|envFrom")
        self.assertFalse(any("serviceAccountToken" in volume for volume in pod["spec"].get("volumes", [])))
        self.assertEqual(pod["spec"]["initContainers"][0]["name"], "copy-swagger-ui")
        self.assertEqual(
            pod["spec"]["initContainers"][0]["image"],
            container["image"],
        )
        volume_names = {volume["name"] for volume in pod["spec"]["volumes"]}
        self.assertTrue({"swagger-ui", "pinlog-config", "nginx-conf"}.issubset(volume_names))
        mounts = {mount["mountPath"] for mount in container["volumeMounts"]}
        self.assertIn("/usr/share/nginx/html", mounts)
        self.assertIn("/etc/nginx/conf.d", mounts)
        self.assertIn("/docker-entrypoint.d/50-pinlog-config.sh", mounts)

    def test_service_and_ingress_route_without_prefix_stripping(self):
        service = load("service.yaml")
        self.assertEqual(service["metadata"]["namespace"], "pinlog-dev")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(service["spec"]["ports"][0]["port"], 80)
        self.assertEqual(service["spec"]["ports"][0]["targetPort"], "http")
        ingress = load("ingress.yaml")
        self.assertEqual(ingress["spec"]["ingressClassName"], "traefik")
        rule = ingress["spec"]["rules"][0]
        self.assertEqual(rule["host"], "i15a705.p.ssafy.io")
        self.assertEqual(rule["http"]["paths"][0]["path"], "/api-console")
        self.assertNotRegex((PLATFORM / "ingress.yaml").read_text(), r"(?i)StripPrefix|middlewares")

    def test_argocd_application_is_dev_only(self):
        app = yaml.safe_load((ROOT / "argocd/apps/api-console-dev.yaml").read_text())
        self.assertEqual(app["metadata"]["name"], "api-console-dev")
        self.assertEqual(app["spec"]["source"]["path"], "platform/api-console-dev")
        self.assertEqual(app["spec"]["source"]["targetRevision"], "main")
        self.assertEqual(app["spec"]["destination"]["namespace"], "pinlog-dev")
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["selfHeal"])

    def test_front_ingress_is_enabled_at_root(self):
        values = yaml.safe_load((ROOT / "apps/dev/front/values.yaml").read_text())
        self.assertEqual(values["ingress"], {
            "enabled": True,
            "className": "traefik",
            "host": "i15a705.p.ssafy.io",
            "path": "/",
            "pathType": "Prefix",
        })


if __name__ == "__main__":
    unittest.main()
