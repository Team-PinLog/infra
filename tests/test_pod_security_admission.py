from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
NAMESPACES = ROOT / "platform" / "namespaces" / "namespaces.yaml"
CHART = ROOT / "charts" / "microservice"
COWORK_VALUES = ROOT / "apps" / "dev" / "cowork" / "values.yaml"
CONTRACT = ROOT / "docs" / "pod-security-admission.md"
README = ROOT / "README.md"
PSA_VERSION = "v1.36"


def _documents(path: Path) -> list[dict]:
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


class PodSecurityAdmissionTest(unittest.TestCase):
    def test_managed_namespaces_use_non_blocking_restricted_audit_and_warn(self):
        namespaces = {
            document["metadata"]["name"]: document
            for document in _documents(NAMESPACES)
            if document.get("kind") == "Namespace"
        }
        self.assertEqual(set(namespaces), {"pinlog-dev", "pinlog-prod", "monitoring"})

        expected = {
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/audit-version": PSA_VERSION,
            "pod-security.kubernetes.io/warn": "restricted",
            "pod-security.kubernetes.io/warn-version": PSA_VERSION,
        }
        for name, namespace in namespaces.items():
            with self.subTest(namespace=name):
                labels = namespace["metadata"]["labels"]
                self.assertEqual(
                    {key: labels.get(key) for key in expected},
                    expected,
                )
                self.assertNotIn("pod-security.kubernetes.io/enforce", labels)
                self.assertNotIn("pod-security.kubernetes.io/enforce-version", labels)

    def test_microservice_chart_defaults_to_runtime_default_seccomp(self):
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        self.assertEqual(
            values["podSecurityContext"]["seccompProfile"],
            {"type": "RuntimeDefault"},
        )

        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cowork",
                str(CHART),
                "--namespace",
                "pinlog-dev",
                "-f",
                str(COWORK_VALUES),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        deployment = next(
            document
            for document in yaml.safe_load_all(rendered)
            if document and document.get("kind") == "Deployment"
        )
        self.assertEqual(
            deployment["spec"]["template"]["spec"]["securityContext"]["seccompProfile"],
            {"type": "RuntimeDefault"},
        )

    def test_contract_documents_enforcement_gate_and_namespace_exceptions(self):
        text = CONTRACT.read_text()
        self.assertIn("docs/pod-security-admission.md", README.read_text())
        self.assertIn(
            "`pod-security.kubernetes.io/enforce` label은 이 단계에서 사용하지 않는다.",
            text,
        )
        self.assertIn(
            "node-exporter는 `hostNetwork`·`hostPID`·`hostPath` 외에도 "
            "`allowPrivilegeEscalation: false`·`capabilities.drop: [\"ALL\"]`·seccomp가 누락됐다.",
            text,
        )
        self.assertIn(
            "Loki와 `loki-sc-rules`는 seccomp 누락",
            text,
        )
        self.assertIn(
            "node-exporter의 host 접근은 운영 예외 후보이고 나머지는 보완 대상이다. "
            "restricted enforce 금지",
            text,
        )
        for required in (
            "audit/warn",
            "enforce 전환 조건",
            "pinlog-dev",
            "pinlog-prod",
            "monitoring",
            "argocd",
            "kube-system",
            "hostPath",
            "runAsNonRoot",
            "allowPrivilegeEscalation",
            "capabilities.drop",
            "seccompProfile",
            "RuntimeDefault",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
