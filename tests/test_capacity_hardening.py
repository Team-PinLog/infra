from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "platform" / "monitoring" / "kube-prometheus-stack-values.yaml"
RUNBOOK = ROOT / "docs" / "capacity-hardening.md"
METRICS_SERVER = ROOT / "docs" / "metrics-server.md"


class CapacityHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
        cls.rule_map = cls.values["additionalPrometheusRulesMap"]

    def rules(self, name):
        return self.rule_map[name]["groups"][0]["rules"]

    def test_cpu_steal_and_psi_use_node_exporter_metric_contract(self):
        rules = self.rules("pinlog-node-capacity")
        alerts = {rule.get("alert"): rule for rule in rules if "alert" in rule}
        expected = {
            "PinLogNodeCPUStealSustained": ("warning", "15m"),
            "PinLogNodeCPUStealCritical": ("critical", "10m"),
            "PinLogNodeCPUPressureSustained": ("warning", "15m"),
            "PinLogNodeCPUPressureCritical": ("critical", "10m"),
            "PinLogNodeCPUStealMetricAbsent": ("warning", "15m"),
            "PinLogNodeCPUPressureMetricAbsent": ("warning", "15m"),
        }
        self.assertEqual(set(alerts), set(expected))
        for name, (severity, duration) in expected.items():
            with self.subTest(alert=name):
                self.assertEqual(alerts[name]["labels"]["severity"], severity)
                self.assertEqual(alerts[name]["for"], duration)
                annotations = alerts[name]["annotations"]
                self.assertTrue(all(key in annotations for key in ("status", "impact", "check")))
                self.assertNotIn("@channel", str(annotations))

        steal_exprs = "\n".join(rule["expr"] for rule in rules if "Steal" in rule.get("alert", ""))
        psi_exprs = "\n".join(rule["expr"] for rule in rules if "Pressure" in rule.get("alert", ""))
        self.assertIn('node_cpu_seconds_total{mode="steal"}', steal_exprs)
        self.assertIn("absent(node_cpu_seconds_total", steal_exprs)
        self.assertIn("node_pressure_cpu_waiting_seconds_total", psi_exprs)
        self.assertIn("absent(node_pressure_cpu_waiting_seconds_total)", psi_exprs)
        self.assertNotIn("node_pressure_cpu_wait_seconds_total", psi_exprs)

    def test_cpu_overcommit_is_recorded_and_only_warns_on_sustained_requests(self):
        rules = self.rules("pinlog-node-cpu-overcommit")
        records = {rule.get("record") for rule in rules if "record" in rule}
        self.assertEqual(
            records,
            {"pinlog:node_cpu_requests_allocatable:ratio", "pinlog:node_cpu_limits_allocatable:ratio"},
        )
        rendered = str(rules)
        self.assertIn("kube_pod_container_resource_requests", rendered)
        self.assertIn("kube_pod_container_resource_limits", rendered)
        self.assertIn("kube_node_status_allocatable", rendered)
        alerts = [rule for rule in rules if "alert" in rule]
        self.assertEqual([rule["alert"] for rule in alerts], ["PinLogNodeCPURequestsOvercommit"])
        self.assertEqual(alerts[0]["labels"]["severity"], "warning")
        self.assertEqual(alerts[0]["for"], "30m")
        self.assertIn("> 0.85", alerts[0]["expr"])

    def test_long_observation_and_permissions_blocker_are_documented(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for required in (
            "24시간", "7일", "14일", "CPU steal", "CPU PSI", "requests / allocatable",
            "limits / allocatable", "CloudWatch", "권한 blocker", "AWS mutation 금지",
        ):
            self.assertIn(required, text)

    def test_metrics_server_is_intentionally_disabled_and_recovery_is_acceptance_gated(self):
        text = METRICS_SERVER.read_text(encoding="utf-8")
        for required in (
            "DESIRED_REPLICAS=0", "의도적", "복구 설계", "acceptance", "resource budget",
            "무조건 enable하지 않는다", "live 적용 전 영향",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
