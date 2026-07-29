import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "platform" / "monitoring" / "kube-prometheus-stack-values.yaml"
DASHBOARD_DIR = ROOT / "platform" / "monitoring" / "dashboards"
DASHBOARD = DASHBOARD_DIR / "pinlog-operations.dashboard"
KUSTOMIZATION = DASHBOARD_DIR / "kustomization.yaml"
ARGO_APP = ROOT / "argocd" / "apps" / "monitoring-prometheus.yaml"


class PinLogOperationsDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))

    def dashboard(self):
        return json.loads(DASHBOARD.read_text(encoding="utf-8"))

    def test_dashboard_is_declaratively_provisioned(self):
        kustomization = yaml.safe_load(KUSTOMIZATION.read_text(encoding="utf-8"))
        generator = next(
            item
            for item in kustomization["configMapGenerator"]
            if item["name"] == "pinlog-operations-dashboard"
        )
        self.assertEqual(generator["namespace"], "monitoring")
        self.assertIn(
            "pinlog-operations.json=pinlog-operations.dashboard",
            generator["files"],
        )
        self.assertEqual(
            kustomization["generatorOptions"]["labels"]["grafana_dashboard"], "1"
        )
        self.assertTrue(kustomization["generatorOptions"]["disableNameSuffixHash"])

        application = yaml.safe_load(ARGO_APP.read_text(encoding="utf-8"))
        source_paths = {
            source.get("path")
            for source in application["spec"]["sources"]
            if source.get("repoURL") == "https://github.com/Team-PinLog/infra.git"
        }
        self.assertIn("platform/monitoring/dashboards", source_paths)

        dashboard = self.dashboard()
        self.assertEqual(dashboard["uid"], "pinlog-operations")
        self.assertEqual(dashboard["title"], "PinLog Operations Overview")
        self.assertGreaterEqual(dashboard["schemaVersion"], 39)
        self.assertEqual(dashboard["refresh"], "30s")

    def test_loki_datasource_has_stable_uid(self):
        loki = next(
            source
            for source in self.values["grafana"]["additionalDataSources"]
            if source["name"] == "Loki"
        )
        self.assertEqual(loki["uid"], "P8E80F9AEF21F6940")

    def test_dashboard_covers_pinlog_operational_signals(self):
        dashboard = self.dashboard()
        panels = dashboard["panels"]
        ids = [panel["id"] for panel in panels]
        self.assertEqual(len(ids), len(set(ids)))

        expressions = [
            target.get("expr", "")
            for panel in panels
            for target in panel.get("targets", [])
        ]
        joined = "\n".join(expressions)

        required_fragments = (
            'up{job="back",namespace="pinlog-prod"}',
            "kube_deployment_status_replicas_available",
            "kube_pod_container_status_restarts_total",
            'node_cpu_seconds_total{mode="idle"}',
            "node_memory_MemAvailable_bytes",
            'node_filesystem_avail_bytes{mountpoint="/"',
            'ALERTS{alertstate="firing",severity=~"warning|critical"}',
            "http_server_requests_seconds_count",
            'status=~"5.."',
            'namespace=~"pinlog-dev|pinlog-prod"',
            '(?i)(error|exception|fail)',
        )
        for fragment in required_fragments:
            self.assertIn(fragment, joined)

        self.assertNotIn("container_cpu_usage_seconds_total", joined)
        self.assertNotIn("container_memory_working_set_bytes", joined)

        datasource_uids = {
            panel.get("datasource", {}).get("uid")
            for panel in panels
            if isinstance(panel.get("datasource"), dict)
        }
        self.assertIn("prometheus", datasource_uids)
        self.assertIn("P8E80F9AEF21F6940", datasource_uids)

    def test_dashboard_is_gitops_owned_and_zero_alerts_are_healthy(self):
        dashboard = self.dashboard()
        self.assertFalse(dashboard["editable"])

        expressions = [
            target["expr"]
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
            if "expr" in target
        ]
        alert_count = next(expr for expr in expressions if expr.startswith("count(ALERTS"))
        self.assertIn("or vector(0)", alert_count)

        backend_runtime = [
            expr
            for expr in expressions
            if "http_server_requests_seconds" in expr or "hikaricp_connections_pending" in expr
        ]
        self.assertTrue(backend_runtime)
        self.assertTrue(all('namespace="pinlog-prod"' in expr for expr in backend_runtime))

    def test_dashboard_has_no_external_or_legacy_host_dependency(self):
        raw = json.dumps(self.dashboard(), ensure_ascii=False)
        self.assertNotIn("localhost", raw)
        self.assertNotIn("i15a705.p.ssafy.io", raw)
        self.assertNotIn("pin-log.com", raw)


if __name__ == "__main__":
    unittest.main()
