import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from yaml.nodes import ScalarNode


ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "platform" / "network-policies"
TARGET_NAMESPACES = {"pinlog-prod", "pinlog-dev", "monitoring"}
EXPECTED_POLICY_FILES = {"dev.yaml", "monitoring.yaml", "prod.yaml"}
EXPECTED_POLICY_IDENTITIES = {
    ("pinlog-prod", "allow-traefik-to-ingress-pods"),
    ("pinlog-prod", "allow-prometheus-to-metrics-pods"),
    ("pinlog-prod", "allow-same-namespace-ingress"),
    ("pinlog-prod", "default-deny-ingress"),
    ("pinlog-dev", "allow-traefik-to-ingress-pods"),
    ("pinlog-dev", "allow-prometheus-to-metrics-pods"),
    ("pinlog-dev", "allow-same-namespace-ingress"),
    ("pinlog-dev", "default-deny-ingress"),
    ("monitoring", "allow-traefik-to-grafana"),
    ("monitoring", "allow-same-namespace-ingress"),
    ("monitoring", "default-deny-ingress"),
}


def _documents(path: Path) -> list[dict]:
    content = path.read_text()
    nodes = list(yaml.compose_all(content))
    documents = list(yaml.safe_load_all(content))
    if len(nodes) != len(documents):
        raise AssertionError(f"YAML parser document mismatch: {path.name}")
    # PyYAML은 empty/comment-only document를 null tag + 빈 ScalarNode로 표현한다.
    # 명시적인 null/~ 토큰은 node.value가 비어 있지 않으므로 남겨 kind 검사에서 거부한다.
    return [
        doc
        for node, doc in zip(nodes, documents)
        if not (
            isinstance(node, ScalarNode)
            and node.tag == "tag:yaml.org,2002:null"
            and node.value == ""
        )
    ]


def _load_policy_directory(policy_dir: Path) -> list[dict]:
    entries = list(policy_dir.iterdir())
    if {path.name for path in entries} != EXPECTED_POLICY_FILES:
        raise AssertionError("unexpected file in deployable policy directory")

    policies: list[dict] = []
    for path in sorted(entries):
        if path.is_symlink() or not path.is_file() or path.suffix != ".yaml":
            raise AssertionError(f"unsupported policy path: {path.name}")
        for doc in _documents(path):
            if not isinstance(doc, dict) or doc.get("kind") != "NetworkPolicy":
                raise AssertionError(
                    f"only standalone NetworkPolicy documents are allowed: {path.name}"
                )
            policies.append(doc)
    return policies


def _all_policies() -> list[dict]:
    return _load_policy_directory(POLICY_DIR)


def _policy(namespace: str, name: str) -> dict:
    matches = [
        policy
        for policy in _all_policies()
        if policy["metadata"]["namespace"] == namespace
        and policy["metadata"]["name"] == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one {namespace}/{name}, found {len(matches)}")
    return matches[0]


def _wave(policy: dict) -> int:
    return int(policy["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])


def _assert_closed_policy_inventory(test: unittest.TestCase, policies: list[dict]) -> None:
    identities = [
        (policy["metadata"]["namespace"], policy["metadata"]["name"])
        for policy in policies
    ]
    test.assertEqual(len(identities), len(set(identities)), "duplicate policy identity")
    test.assertEqual(set(identities), EXPECTED_POLICY_IDENTITIES)

    for policy in policies:
        test.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        test.assertNotIn("egress", policy["spec"])
        ingress = policy["spec"].get("ingress", [])
        if policy["metadata"]["name"] == "default-deny-ingress":
            test.assertEqual(ingress, [])
            continue
        test.assertTrue(ingress, policy["metadata"])
        for rule in ingress:
            test.assertTrue(rule.get("from"), policy["metadata"])
            for peer in rule["from"]:
                test.assertTrue(peer, policy["metadata"])
                test.assertTrue(
                    set(peer).issubset({"namespaceSelector", "podSelector"}),
                    policy["metadata"],
                )
                for selector in peer.values():
                    test.assertEqual(set(selector), {"matchLabels"}, policy["metadata"])
                    test.assertTrue(selector["matchLabels"], policy["metadata"])


class NetworkPoliciesTest(unittest.TestCase):
    def test_argocd_application_manages_network_policies_after_namespaces(self):
        app = yaml.safe_load((ROOT / "argocd/apps/network-policies.yaml").read_text())
        self.assertEqual(app["kind"], "Application")
        self.assertEqual(app["spec"]["source"]["path"], "platform/network-policies")
        self.assertEqual(app["spec"]["source"]["targetRevision"], "main")
        self.assertEqual(
            app["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
            "4",
        )
        self.assertIn(
            "resources-finalizer.argocd.argoproj.io",
            app["metadata"]["finalizers"],
        )
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["selfHeal"])
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["prune"])
        root_app = yaml.safe_load((ROOT / "argocd/root/root-app.yaml").read_text())
        self.assertTrue(root_app["spec"]["syncPolicy"]["automated"]["prune"])

    def test_allow_rules_precede_ingress_default_deny_in_every_namespace(self):
        policies = _all_policies()
        _assert_closed_policy_inventory(self, policies)
        self.assertEqual(
            {policy["metadata"]["namespace"] for policy in policies},
            TARGET_NAMESPACES,
        )
        for policy in policies:
            self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
            self.assertNotIn("egress", policy["spec"])
        for namespace in TARGET_NAMESPACES:
            same_namespace = _policy(namespace, "allow-same-namespace-ingress")
            default_deny = _policy(namespace, "default-deny-ingress")

            self.assertLess(_wave(same_namespace), _wave(default_deny))
            self.assertEqual(same_namespace["spec"]["podSelector"], {})
            self.assertEqual(same_namespace["spec"]["policyTypes"], ["Ingress"])
            self.assertEqual(
                same_namespace["spec"]["ingress"],
                [
                    {
                        "from": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": namespace
                                    }
                                }
                            }
                        ]
                    }
                ],
            )
            self.assertEqual(default_deny["spec"]["podSelector"], {})
            self.assertEqual(default_deny["spec"]["policyTypes"], ["Ingress"])
            self.assertEqual(default_deny["spec"]["ingress"], [])

            for policy in [same_namespace, default_deny]:
                self.assertNotIn("Egress", policy["spec"]["policyTypes"])
                self.assertNotIn("egress", policy["spec"])

    def test_policy_inventory_rejects_unioned_allow_all_bypass(self):
        malicious = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": "allow-all-cross-namespace",
                "namespace": "pinlog-prod",
            },
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [{}],
            },
        }
        with self.assertRaises(AssertionError):
            _assert_closed_policy_inventory(self, _all_policies() + [malicious])

        widened = copy.deepcopy(_all_policies())
        target = next(
            policy
            for policy in widened
            if policy["metadata"]["namespace"] == "pinlog-prod"
            and policy["metadata"]["name"] == "allow-traefik-to-ingress-pods"
        )
        target["spec"]["ingress"][0]["from"].append({"namespaceSelector": {}})
        with self.assertRaises(AssertionError):
            _assert_closed_policy_inventory(self, widened)

    def test_deployable_directory_rejects_yml_and_list_bypasses(self):
        allow_all = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": "allow-all-cross-namespace",
                "namespace": "pinlog-prod",
            },
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [{}],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            mutated = Path(temp) / "network-policies"
            shutil.copytree(POLICY_DIR, mutated)
            (mutated / "extra.yml").write_text(yaml.safe_dump(allow_all))
            with self.assertRaises(AssertionError):
                _load_policy_directory(mutated)

        with tempfile.TemporaryDirectory() as temp:
            mutated = Path(temp) / "network-policies"
            shutil.copytree(POLICY_DIR, mutated)
            with (mutated / "prod.yaml").open("a") as stream:
                stream.write(
                    "---\n"
                    + yaml.safe_dump(
                        {
                            "apiVersion": "v1",
                            "kind": "List",
                            "items": [allow_all],
                        }
                    )
                )
            with self.assertRaises(AssertionError):
                _load_policy_directory(mutated)

        for empty_document in ("---\n", "---\n# comment-only document\n"):
            with self.subTest(empty_document=empty_document):
                with tempfile.TemporaryDirectory() as temp:
                    mutated = Path(temp) / "network-policies"
                    shutil.copytree(POLICY_DIR, mutated)
                    with (mutated / "prod.yaml").open("a") as stream:
                        stream.write(empty_document)
                    self.assertEqual(
                        len(_load_policy_directory(mutated)),
                        len(_all_policies()),
                    )

        for falsey_document in (
            "{}",
            "[]",
            "false",
            "0",
            '\"\"',
            "null",
            "~",
            "Null",
            "NULL",
        ):
            with self.subTest(falsey_document=falsey_document):
                with tempfile.TemporaryDirectory() as temp:
                    mutated = Path(temp) / "network-policies"
                    shutil.copytree(POLICY_DIR, mutated)
                    with (mutated / "prod.yaml").open("a") as stream:
                        stream.write(f"---\n{falsey_document}\n")
                    with self.assertRaises(AssertionError):
                        _load_policy_directory(mutated)

    def test_traefik_and_prometheus_cross_namespace_ingress_is_label_scoped(self):
        for namespace in ("pinlog-prod", "pinlog-dev"):
            traefik = _policy(namespace, "allow-traefik-to-ingress-pods")
            metrics = _policy(namespace, "allow-prometheus-to-metrics-pods")
            same_namespace = _policy(namespace, "allow-same-namespace-ingress")

            self.assertLess(_wave(traefik), _wave(same_namespace))
            self.assertLess(_wave(metrics), _wave(same_namespace))
            self.assertEqual(
                traefik["spec"]["podSelector"]["matchLabels"],
                {"networking.pinlog.io/ingress": "true"},
            )
            traefik_peer = traefik["spec"]["ingress"][0]["from"][0]
            self.assertEqual(
                traefik_peer["namespaceSelector"]["matchLabels"],
                {"kubernetes.io/metadata.name": "kube-system"},
            )
            self.assertEqual(
                traefik_peer["podSelector"]["matchLabels"],
                {"app.kubernetes.io/name": "traefik"},
            )
            self.assertEqual(
                traefik["spec"]["ingress"][0]["ports"],
                [{"port": "http", "protocol": "TCP"}],
            )
            self.assertEqual(len(traefik["spec"]["ingress"]), 1)
            self.assertEqual(len(traefik["spec"]["ingress"][0]["from"]), 2)
            self.assertEqual(
                traefik["spec"]["ingress"][0]["from"][1],
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": namespace
                        }
                    }
                },
            )

            self.assertEqual(
                metrics["spec"]["podSelector"]["matchLabels"],
                {"networking.pinlog.io/metrics": "true"},
            )
            metrics_peer = metrics["spec"]["ingress"][0]["from"][0]
            self.assertEqual(
                metrics_peer["namespaceSelector"]["matchLabels"],
                {"kubernetes.io/metadata.name": "monitoring"},
            )
            self.assertEqual(
                metrics_peer["podSelector"]["matchLabels"],
                {"app.kubernetes.io/name": "prometheus"},
            )
            self.assertEqual(
                metrics["spec"]["ingress"][0]["ports"],
                [{"port": "http", "protocol": "TCP"}],
            )
            self.assertEqual(len(metrics["spec"]["ingress"]), 1)
            self.assertEqual(len(metrics["spec"]["ingress"][0]["from"]), 2)
            self.assertEqual(
                metrics["spec"]["ingress"][0]["from"][1],
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": namespace
                        }
                    }
                },
            )

        grafana = _policy("monitoring", "allow-traefik-to-grafana")
        self.assertEqual(
            grafana["spec"]["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "grafana"},
        )
        grafana_peer = grafana["spec"]["ingress"][0]["from"][0]
        self.assertEqual(
            grafana_peer["namespaceSelector"]["matchLabels"],
            {"kubernetes.io/metadata.name": "kube-system"},
        )
        self.assertEqual(
            grafana_peer["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "traefik"},
        )
        self.assertEqual(
            grafana["spec"]["ingress"][0]["ports"],
            [{"port": "grafana", "protocol": "TCP"}],
        )
        self.assertEqual(len(grafana["spec"]["ingress"]), 1)
        self.assertEqual(len(grafana["spec"]["ingress"][0]["from"]), 2)
        self.assertEqual(
            grafana["spec"]["ingress"][0]["from"][1],
            {
                "namespaceSelector": {
                    "matchLabels": {
                        "kubernetes.io/metadata.name": "monitoring"
                    }
                }
            },
        )

    def test_microservice_chart_labels_network_contract_conditionally(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/validate.yaml").read_text())
        guardrail_steps = workflow["jobs"]["guardrails"]["steps"]
        self.assertTrue(
            any(
                step.get("uses")
                == "azure/setup-helm@9bc31f4ebc9c6b171d7bfbaa5d006ae7abdb4310"
                and step.get("with", {}).get("version") == "v3.16.3"
                for step in guardrail_steps
            )
        )

        def render(*sets: str) -> dict:
            command = [
                "helm",
                "template",
                "contract-test",
                str(ROOT / "charts/microservice"),
            ]
            for value in sets:
                command.extend(["--set", value])
            output = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            return next(
                doc
                for doc in yaml.safe_load_all(output)
                if doc and doc.get("kind") == "Deployment"
            )

        default_labels = render()["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(default_labels["app.kubernetes.io/part-of"], "pinlog")
        self.assertEqual(default_labels["networking.pinlog.io/ingress"], "true")
        self.assertNotIn("networking.pinlog.io/metrics", default_labels)

        metrics_labels = render("metrics.enabled=true")["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(metrics_labels["networking.pinlog.io/metrics"], "true")

        internal_labels = render("ingress.enabled=false")["spec"]["template"]["metadata"]["labels"]
        self.assertNotIn("networking.pinlog.io/ingress", internal_labels)

        for reserved in (
            "app.kubernetes.io/part-of",
            "networking.pinlog.io/ingress",
            "networking.pinlog.io/metrics",
        ):
            escaped = reserved.replace(".", "\\.")
            result = subprocess.run(
                [
                    "helm",
                    "template",
                    "contract-test",
                    str(ROOT / "charts/microservice"),
                    "--set-string",
                    f"podLabels.{escaped}=false",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0, reserved)
            self.assertIn("reserved NetworkPolicy label", result.stderr)

    def test_documentation_records_matrix_staging_verification_and_rollback(self):
        document = (ROOT / "docs/network-policies.md").read_text()
        for required in (
            "Ingress default-deny",
            "Egress default-deny",
            "Frontend → Backend",
            "Backend → PostgreSQL",
            "Backend → Redis",
            "Prometheus → metrics-enabled pod",
            "Traefik → ingress-enabled pod",
            "Sentinel ExternalName",
            "rollback",
            "resources-finalizer.argocd.argoproj.io",
            "workload가 Ready",
            "kubectl apply --dry-run=server",
        ):
            self.assertIn(required, document)


if __name__ == "__main__":
    unittest.main()
