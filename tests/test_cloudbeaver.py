from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platform" / "cloudbeaver"
STATEFULSET = PLATFORM / "statefulset.yaml"
SERVICE = PLATFORM / "service.yaml"
NETWORK_POLICY = PLATFORM / "networkpolicy.yaml"
TUNNEL_DEPLOYMENT = PLATFORM / "cloudflared-deployment.yaml"
TUNNEL_NETWORK_POLICY = PLATFORM / "cloudflared-networkpolicy.yaml"
KUSTOMIZATION = PLATFORM / "kustomization.yaml"
README = PLATFORM / "README.md"
ARGO_APP = ROOT / "argocd" / "apps" / "cloudbeaver.yaml"
SECRETS_KUSTOMIZATION = ROOT / "secrets" / "prod" / "kustomization.yaml"
IMAGE = (
    "dbeaver/cloudbeaver:26.1.3@"
    "sha256:a4b7286a88b9b7c05013b624654a7c5997fbbe8f974604a1274a8246cc57c026"
)


class CloudBeaverContractTest(unittest.TestCase):
    def test_dedicated_tunnel_is_pinned_hardened_and_reads_only_token_file(self):
        deployment = yaml.safe_load(TUNNEL_DEPLOYMENT.read_text(encoding="utf-8"))
        self.assertEqual(deployment["metadata"], {"name": "cloudbeaver-cloudflared", "namespace": "pinlog-prod", "labels": {"app.kubernetes.io/name": "cloudbeaver-cloudflared", "app.kubernetes.io/component": "tunnel"}})
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        container = pod["containers"][0]
        self.assertEqual(container["image"], "cloudflare/cloudflared:2026.7.2@sha256:4f6655284ab3d252b7f28fedb19fe6c8fc82ee5b1295c20ac74d475e5398a52d")
        self.assertEqual(container["args"][-2:], ["--token-file", "/etc/cloudflared/token"])
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
        token = next(v for v in pod["volumes"] if v["name"] == "tunnel-token")["secret"]
        self.assertEqual(token["secretName"], "cloudbeaver-cloudflared-token")
        self.assertEqual(token["items"], [{"key": "token", "path": "token"}])

    def test_network_policy_allows_only_tunnel_ingress_and_practical_tunnel_egress(self):
        cloudbeaver = yaml.safe_load(NETWORK_POLICY.read_text(encoding="utf-8"))
        self.assertEqual(cloudbeaver["spec"]["ingress"], [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "cloudbeaver-cloudflared"}}}], "ports": [{"protocol": "TCP", "port": 8978}]}])

        tunnel = yaml.safe_load(TUNNEL_NETWORK_POLICY.read_text(encoding="utf-8"))
        self.assertEqual(tunnel["spec"]["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(tunnel["spec"]["ingress"], [])
        egress = tunnel["spec"]["egress"]
        self.assertEqual(len(egress), 3)
        self.assertEqual({(p["protocol"], p["port"]) for p in egress[0]["ports"]}, {("UDP", 53), ("TCP", 53)})
        self.assertEqual(egress[1], {"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "cloudbeaver"}}}], "ports": [{"protocol": "TCP", "port": 8978}]})
        self.assertEqual(egress[2]["to"], [{"ipBlock": {"cidr": "0.0.0.0/0"}}])
        self.assertEqual({(p["protocol"], p["port"]) for p in egress[2]["ports"]}, {("TCP", 7844), ("UDP", 7844), ("TCP", 443)})

    def test_tunnel_handoff_and_missing_ciphertext_reservation_are_explicit(self):
        text = README.read_text(encoding="utf-8")
        for required in ("db.pin-log.com", "http://cloudbeaver.pinlog-prod.svc.cluster.local:8978", "Cloudflare Access", "token owner", "CNI", "0.0.0.0/0"):
            self.assertIn(required, text)
        resources = yaml.safe_load(KUSTOMIZATION.read_text(encoding="utf-8"))["resources"]
        self.assertIn("cloudflared-deployment.yaml", resources)
        self.assertIn("cloudflared-networkpolicy.yaml", resources)
        secret_resources = yaml.safe_load(SECRETS_KUSTOMIZATION.read_text(encoding="utf-8"))["resources"]
        self.assertIn("cloudbeaver-cloudflared-token.sealedsecret.yaml", secret_resources)
        self.assertFalse((SECRETS_KUSTOMIZATION.parent / "cloudbeaver-cloudflared-token.sealedsecret.yaml").exists())

    def test_gitops_workload_is_pinned_hardened_bounded_and_persistent(self):
        statefulset = yaml.safe_load(STATEFULSET.read_text(encoding="utf-8"))
        self.assertEqual(statefulset["kind"], "StatefulSet")
        self.assertEqual(statefulset["metadata"]["namespace"], "pinlog-prod")
        self.assertEqual(statefulset["spec"]["replicas"], 1)
        self.assertEqual(statefulset["spec"]["updateStrategy"], {"type": "RollingUpdate"})
        self.assertEqual(
            statefulset["spec"]["persistentVolumeClaimRetentionPolicy"],
            {"whenDeleted": "Retain", "whenScaled": "Retain"},
        )

        pod = statefulset["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "pinlog-master"})
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertEqual(pod["securityContext"]["fsGroup"], 8978)
        container = pod["containers"][0]
        self.assertEqual(container["image"], IMAGE)
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        self.assertNotIn("env", container)
        self.assertNotIn("envFrom", container)
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "768Mi"},
            },
        )
        security = container["securityContext"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertEqual(security["runAsUser"], 8978)
        self.assertEqual(security["runAsGroup"], 8978)
        self.assertFalse(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe]["tcpSocket"], {"port": "http"})
        mounts = {item["name"]: item for item in container["volumeMounts"]}
        self.assertEqual(mounts["workspace"]["mountPath"], "/opt/cloudbeaver/workspace")
        self.assertEqual(mounts["tmp"]["mountPath"], "/tmp")
        claims = statefulset["spec"]["volumeClaimTemplates"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["metadata"]["name"], "workspace")
        self.assertEqual(claims[0]["spec"]["storageClassName"], "local-path-retain")
        self.assertEqual(claims[0]["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claims[0]["spec"]["resources"]["requests"]["storage"], "2Gi")

    def test_service_is_clusterip_only_and_no_public_route_or_secret_is_created(self):
        service = yaml.safe_load(SERVICE.read_text(encoding="utf-8"))
        self.assertEqual(service["kind"], "Service")
        self.assertEqual(service["metadata"]["namespace"], "pinlog-prod")
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertNotIn("nodePort", service["spec"]["ports"][0])
        for forbidden in ("externalIPs", "externalName", "loadBalancerIP", "loadBalancerClass"):
            self.assertNotIn(forbidden, service["spec"])
        self.assertEqual(
            service["spec"]["ports"],
            [{"name": "http", "protocol": "TCP", "port": 8978, "targetPort": "http"}],
        )

        resources = yaml.safe_load(KUSTOMIZATION.read_text(encoding="utf-8"))["resources"]
        self.assertEqual(
            resources,
            [
                "service.yaml",
                "statefulset.yaml",
                "networkpolicy.yaml",
                "cloudflared-deployment.yaml",
                "cloudflared-networkpolicy.yaml",
            ],
        )
        for path in PLATFORM.iterdir():
            if path.suffix in {".yaml", ".yml"} and path.name != "kustomization.yaml":
                for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                    if document:
                        self.assertNotIn(document["kind"], {"Ingress", "Secret", "SealedSecret"})

    def test_argocd_retains_state_and_authenticated_access_handoff_is_explicit(self):
        app = yaml.safe_load(ARGO_APP.read_text(encoding="utf-8"))
        self.assertEqual(app["metadata"]["name"], "cloudbeaver")
        self.assertEqual(app["spec"]["project"], "pinlog")
        self.assertEqual(app["spec"]["source"]["path"], "platform/cloudbeaver")
        self.assertEqual(app["spec"]["source"]["targetRevision"], "main")
        self.assertEqual(app["spec"]["destination"]["namespace"], "pinlog-prod")
        self.assertFalse(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["selfHeal"])

        text = README.read_text(encoding="utf-8")
        for required in (
            "ClusterIP",
            "Ingress",
            "Tailscale",
            "kubectl port-forward",
            "postgres.pinlog-prod.svc.cluster.local:5432",
            "조회 전용",
            "외부 handoff",
            "credential",
            "rollback",
            "PVC",
        ):
            self.assertIn(required, text)
        self.assertNotRegex(text, r"(?i)(password\s*[:=]|stringData:|kind:\s*Secret|sample credential|placeholder)")


if __name__ == "__main__":
    unittest.main()
