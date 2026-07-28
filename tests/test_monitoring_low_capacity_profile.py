from pathlib import Path
import copy
import unittest

import yaml

from tools.validate_rendered_resource_guardrails import (
    EXPECTED_CONFIG_RELOADER_ARGS,
    EXPECTED_PROMETHEUS_WORKLOADS,
    validate_config_reloader_args,
    validate_alloy_contract,
    validate_loki_contract,
    validate_low_capacity_render_contract,
    validate_prometheus_workload_contract,
)
from tools.verify_monitoring_pvc_inventory import validate_inventory


ROOT = Path(__file__).resolve().parents[1]
PROMETHEUS_VALUES = ROOT / "platform" / "monitoring" / "kube-prometheus-stack-values.yaml"
LOKI_VALUES = ROOT / "platform" / "monitoring" / "loki-values.yaml"
ALLOY_VALUES = ROOT / "platform" / "monitoring" / "alloy-values.yaml"
BACK_VALUES = ROOT / "apps" / "prod" / "back" / "values.yaml"
MONITORING_DOC = ROOT / "docs" / "monitoring.md"
ARCHITECTURE_DOC = ROOT / "docs" / "architecture.md"
METRICS_SERVER_DOC = ROOT / "docs" / "metrics-server.md"
RUNBOOK_DOC = ROOT / "docs" / "runbook.md"


def _values(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class MonitoringLowCapacityProfileTest(unittest.TestCase):
    def test_prometheus_workload_contract_rejects_inflated_and_extra_workloads(self):
        documents = []
        for (kind, name), (replicas, compact) in EXPECTED_PROMETHEUS_WORKLOADS.items():
            containers = [
                {"name": container, "resources": {"requests": requests, "limits": limits}}
                for container, (requests, limits) in compact.items()
            ]
            documents.append(
                {
                    "kind": kind,
                    "metadata": {"name": name},
                    "spec": {"replicas": replicas, "template": {"spec": {"containers": containers}}},
                }
            )
        self.assertEqual(validate_prometheus_workload_contract(documents), [])
        inflated = copy.deepcopy(documents)
        operator = next(
            item
            for item in inflated
            if item["metadata"]["name"] == "kube-prometheus-stack-operator"
        )
        operator["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "99Gi"
        self.assertTrue(validate_prometheus_workload_contract(inflated))
        extra = copy.deepcopy(documents)
        extra.append(
            {"kind": "Deployment", "metadata": {"name": "unexpected"}, "spec": {}}
        )
        self.assertTrue(validate_prometheus_workload_contract(extra))
        duplicate = copy.deepcopy(documents)
        duplicate.append(copy.deepcopy(documents[0]))
        self.assertTrue(validate_prometheus_workload_contract(duplicate))

    def test_pvc_inventory_rejects_deletion_of_a_preexisting_claim(self):
        before = [
            {"name": "prometheus-data", "phase": "Bound"},
            {"name": "alertmanager-data", "phase": "Bound"},
        ]
        current = {
            "items": [
                {
                    "metadata": {"name": "prometheus-data"},
                    "status": {"phase": "Bound"},
                }
            ]
        }
        errors = validate_inventory(before, current)
        self.assertTrue(any("alertmanager-data" in error for error in errors))

    def test_phase_a_unpauses_only_prometheus(self):
        root = _values(ROOT / "argocd/root/root-app.yaml")
        self.assertTrue(root["spec"]["syncPolicy"]["automated"]["selfHeal"])
        self.assertIn("root/*", root["spec"]["source"]["directory"]["exclude"])

        prometheus = _values(ROOT / "argocd/apps/monitoring-prometheus.yaml")
        self.assertNotIn(
            "argocd.argoproj.io/skip-reconcile",
            prometheus["metadata"].get("annotations", {}),
        )
        for name in ("monitoring-loki", "monitoring-alloy"):
            with self.subTest(application=name):
                app = _values(ROOT / "argocd" / "apps" / f"{name}.yaml")
                self.assertEqual(
                    app["metadata"]["annotations"]["argocd.argoproj.io/skip-reconcile"],
                    "true",
                )

    def test_rendered_loki_and_alloy_contracts_reject_topology_and_config_drift(self):
        loki_documents = [
            {
                "kind": "StatefulSet",
                "metadata": {"name": "loki"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "loki",
                                    "resources": {"requests": {"cpu": "50m", "memory": "256Mi"}, "limits": {"cpu": "250m", "memory": "768Mi"}},
                                },
                                {
                                    "name": "loki-sc-rules",
                                    "resources": {"requests": {"cpu": "5m", "memory": "48Mi"}, "limits": {"cpu": "50m", "memory": "96Mi"}},
                                },
                            ]
                        }
                    },
                },
            },
            {
                "kind": "ConfigMap",
                "metadata": {"name": "loki"},
                "data": {"config.yaml": "limits_config:\n  retention_period: 72h\n  ingestion_rate_mb: 2\n  ingestion_burst_size_mb: 4\n"},
            },
        ]
        self.assertEqual(validate_loki_contract(loki_documents), [])
        scalable = copy.deepcopy(loki_documents)
        scalable.append({"kind": "Deployment", "metadata": {"name": "loki-read"}, "spec": {}})
        self.assertTrue(validate_loki_contract(scalable))
        bad_retention = copy.deepcopy(loki_documents)
        bad_retention[1]["data"]["config.yaml"] = (
            "limits_config:\n  retention_period: 168h\n"
            "  ingestion_rate_mb: 2\n  ingestion_burst_size_mb: 4\n"
            "# retention_period: 72h\n"
        )
        self.assertTrue(validate_loki_contract(bad_retention))
        duplicate_loki = copy.deepcopy(loki_documents)
        duplicate_loki_workload = copy.deepcopy(loki_documents[0])
        duplicate_loki_workload["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "99Gi"
        duplicate_loki.append(duplicate_loki_workload)
        self.assertTrue(validate_loki_contract(duplicate_loki))
        duplicate_loki_config = copy.deepcopy(loki_documents)
        bad_loki_config = copy.deepcopy(loki_documents[1])
        bad_loki_config["data"]["config.yaml"] = "limits_config:\n  retention_period: 999h\n"
        duplicate_loki_config.append(bad_loki_config)
        self.assertTrue(validate_loki_contract(duplicate_loki_config))

        alloy_documents = [
            {
                "kind": "DaemonSet",
                "metadata": {"name": "alloy"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": "alloy", "resources": {"requests": {"cpu": "25m", "memory": "96Mi"}, "limits": {"cpu": "150m", "memory": "192Mi"}}},
                                {"name": "config-reloader", "resources": {"requests": {"cpu": "5m", "memory": "32Mi"}, "limits": {"cpu": "50m", "memory": "64Mi"}}},
                            ]
                        }
                    }
                },
            },
            {
                "kind": "ConfigMap",
                "metadata": {"name": "alloy"},
                "data": {"config.alloy": (
                    'loki.source.kubernetes "pods" {\n'
                    "  targets = discovery.relabel.pod_logs.output\n"
                    "  forward_to = [loki.process.drop_noise.receiver]\n}\n"
                    'loki.write "default" {\n  endpoint {\n'
                    '    url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"\n'
                    "  }\n}\n"
                )},
            },
        ]
        self.assertEqual(validate_alloy_contract(alloy_documents), [])
        duplicate_alloy = copy.deepcopy(alloy_documents)
        duplicate_alloy.append(copy.deepcopy(alloy_documents[0]))
        self.assertTrue(validate_alloy_contract(duplicate_alloy))
        duplicate_alloy_config = copy.deepcopy(alloy_documents)
        bad_alloy_config = copy.deepcopy(alloy_documents[1])
        bad_alloy_config["data"]["config.alloy"] = "invalid {"
        duplicate_alloy_config.append(bad_alloy_config)
        self.assertTrue(validate_alloy_contract(duplicate_alloy_config))
        valid_alloy_config = alloy_documents[1]["data"]["config.alloy"]
        for malformed in (
            "}\n" + valid_alloy_config,
            valid_alloy_config + "\nunmatched {\n",
            valid_alloy_config + "\ntext = `unterminated\n",
            valid_alloy_config + '\ntext = "unterminated\n',
            valid_alloy_config + "\n/* unterminated\n",
        ):
            malformed_documents = copy.deepcopy(alloy_documents)
            malformed_documents[1]["data"]["config.alloy"] = malformed
            self.assertTrue(validate_alloy_contract(malformed_documents))
        wrong_kind = copy.deepcopy(alloy_documents)
        wrong_kind[0]["kind"] = "Deployment"
        self.assertTrue(validate_alloy_contract(wrong_kind))
        commented_only = copy.deepcopy(alloy_documents)
        commented_only[1]["data"]["config.alloy"] = (
            '// loki.source.kubernetes "pods" {}\n'
            '// loki.write "default" {}\n'
        )
        self.assertTrue(validate_alloy_contract(commented_only))
        nested_bypass = copy.deepcopy(alloy_documents)
        nested_bypass[1]["data"]["config.alloy"] = (
            'loki.source.kubernetes "pods" {\n  unrelated {\n'
            "    targets = discovery.relabel.pod_logs.output\n"
            "    forward_to = [loki.process.drop_noise.receiver]\n  }\n}\n"
            'loki.write "default" {\n  unrelated {\n'
            '    url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"\n'
            "  }\n}\n"
        )
        self.assertTrue(validate_alloy_contract(nested_bypass))
        nested_components = copy.deepcopy(alloy_documents)
        nested_components[1]["data"]["config.alloy"] = (
            "unrelated {\n"
            '  loki.source.kubernetes "pods" {\n'
            "    targets = discovery.relabel.pod_logs.output\n"
            "    forward_to = [loki.process.drop_noise.receiver]\n  }\n"
            '  loki.write "default" {\n    endpoint {\n'
            '      url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"\n'
            "    }\n  }\n}\n"
        )
        self.assertTrue(validate_alloy_contract(nested_components))
        raw_string_bypass = copy.deepcopy(alloy_documents)
        raw_string_bypass[1]["data"]["config.alloy"] = (
            "unrelated {\n  text = `}`\n"
            '  loki.source.kubernetes "pods" {\n'
            "    targets = discovery.relabel.pod_logs.output\n"
            "    forward_to = [loki.process.drop_noise.receiver]\n  }\n"
            '  loki.write "default" {\n    endpoint {\n'
            '      url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"\n'
            "    }\n  }\n}\n"
        )
        self.assertTrue(validate_alloy_contract(raw_string_bypass))
        raw_component_text = copy.deepcopy(alloy_documents)
        raw_component_text[1]["data"]["config.alloy"] = (
            "text = `\n"
            'loki.source.kubernetes "pods" {\n'
            "  targets = discovery.relabel.pod_logs.output\n"
            "  forward_to = [loki.process.drop_noise.receiver]\n}\n"
            'loki.write "default" { endpoint {\n'
            '  url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"\n'
            "} }\n`\n"
        )
        self.assertTrue(validate_alloy_contract(raw_component_text))
        raw_attribute_text = copy.deepcopy(alloy_documents)
        raw_attribute_text[1]["data"]["config.alloy"] = (
            'loki.source.kubernetes "pods" {\n  text = `\n'
            "targets = discovery.relabel.pod_logs.output\n"
            "forward_to = [loki.process.drop_noise.receiver]\n`\n}\n"
            'loki.write "default" {\n  endpoint {\n    text = `\n'
            'url = "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"\n'
            "`\n  }\n}\n"
        )
        self.assertTrue(validate_alloy_contract(raw_attribute_text))

    def test_rendered_low_capacity_contract_rejects_dangerous_mutations(self):
        documents = [
            {
                "kind": "Prometheus",
                "metadata": {"name": "kube-prometheus-stack-prometheus"},
                "spec": {
                    "scrapeInterval": "60s",
                    "scrapeTimeout": "15s",
                    "evaluationInterval": "60s",
                    "retention": "3d",
                    "retentionSize": "4GB",
                },
            },
            {
                "kind": "ServiceMonitor",
                "metadata": {"name": "kube-prometheus-stack-kubelet"},
                "spec": {
                    "endpoints": [
                        {"path": "/metrics", "interval": "120s", "scrapeTimeout": "30s"}
                    ]
                },
            },
            {
                "kind": "Deployment",
                "metadata": {"name": "kube-prometheus-stack-grafana"},
                "spec": {"replicas": 0},
            },
        ]
        self.assertEqual(validate_low_capacity_render_contract(documents), [])

        grafana_enabled = copy.deepcopy(documents)
        grafana_enabled[2]["spec"]["replicas"] = 1
        self.assertTrue(validate_low_capacity_render_contract(grafana_enabled))

        cadvisor_enabled = copy.deepcopy(documents)
        cadvisor_enabled[1]["spec"]["endpoints"].append(
            {"path": "/metrics/cadvisor", "interval": "120s", "scrapeTimeout": "30s"}
        )
        self.assertTrue(validate_low_capacity_render_contract(cadvisor_enabled))

    def test_render_validator_enforces_low_capacity_config_reloader_args(self):
        self.assertEqual(
            EXPECTED_CONFIG_RELOADER_ARGS,
            {
                "--config-reloader-cpu-request=5m",
                "--config-reloader-cpu-limit=50m",
                "--config-reloader-memory-request=32Mi",
                "--config-reloader-memory-limit=96Mi",
            },
        )
        self.assertEqual(
            validate_config_reloader_args(list(EXPECTED_CONFIG_RELOADER_ARGS)), []
        )
        conflicting = list(EXPECTED_CONFIG_RELOADER_ARGS) + [
            "--config-reloader-memory-limit=1Gi"
        ]
        self.assertTrue(validate_config_reloader_args(conflicting))

    def test_expensive_runtime_stats_are_disabled_and_scrapes_are_slowed(self):
        values = _values(PROMETHEUS_VALUES)
        kubelet = values["kubelet"]["serviceMonitor"]
        self.assertEqual(kubelet["interval"], "120s")
        self.assertEqual(kubelet["scrapeTimeout"], "30s")
        self.assertFalse(kubelet["cAdvisor"])
        self.assertFalse(kubelet["probes"])
        self.assertFalse(kubelet["resource"])

        spec = values["prometheus"]["prometheusSpec"]
        self.assertEqual(spec["scrapeInterval"], "60s")
        self.assertEqual(spec["scrapeTimeout"], "15s")
        self.assertEqual(spec["evaluationInterval"], "60s")

        back = _values(BACK_VALUES)
        self.assertEqual(back["metrics"]["interval"], "60s")
        self.assertEqual(back["metrics"]["scrapeTimeout"], "10s")

        sentinel = next(
            manifest
            for manifest in values["extraManifests"]
            if manifest.get("kind") == "ServiceMonitor"
            and manifest["metadata"]["name"] == "pinlog-sentinel-receiver"
        )
        self.assertEqual(sentinel["spec"]["endpoints"][0]["interval"], "60s")

    def test_phase_a_keeps_metrics_alerting_bounded_and_grafana_off(self):
        values = _values(PROMETHEUS_VALUES)
        spec = values["prometheus"]["prometheusSpec"]
        self.assertEqual(spec["retention"], "3d")
        self.assertEqual(spec["retentionSize"], "4GB")
        self.assertEqual(
            spec["resources"],
            {
                "requests": {"memory": "768Mi", "cpu": "100m"},
                "limits": {"memory": "1536Mi", "cpu": "400m"},
            },
        )

        expected_resources = {
            "alertmanager": (
                values["alertmanager"]["alertmanagerSpec"]["resources"],
                {"requests": {"memory": "64Mi", "cpu": "10m"}, "limits": {"memory": "192Mi", "cpu": "100m"}},
            ),
            "operator": (
                values["prometheusOperator"]["resources"],
                {"requests": {"memory": "64Mi", "cpu": "25m"}, "limits": {"memory": "192Mi", "cpu": "100m"}},
            ),
            "config-reloader": (
                values["prometheusOperator"]["prometheusConfigReloader"]["resources"],
                {"requests": {"memory": "32Mi", "cpu": "5m"}, "limits": {"memory": "96Mi", "cpu": "50m"}},
            ),
            "kube-state-metrics": (
                values["kube-state-metrics"]["resources"],
                {"requests": {"memory": "48Mi", "cpu": "10m"}, "limits": {"memory": "128Mi", "cpu": "75m"}},
            ),
            "node-exporter": (
                values["prometheus-node-exporter"]["resources"],
                {"requests": {"memory": "24Mi", "cpu": "10m"}, "limits": {"memory": "64Mi", "cpu": "75m"}},
            ),
        }
        for name, (actual, expected) in expected_resources.items():
            with self.subTest(component=name):
                self.assertEqual(actual, expected)

        grafana = values["grafana"]
        self.assertTrue(grafana["enabled"])
        self.assertEqual(grafana["replicas"], 0)
        self.assertEqual(
            grafana["resources"],
            {"requests": {"memory": "128Mi", "cpu": "25m"}, "limits": {"memory": "256Mi", "cpu": "150m"}},
        )

    def test_phase_b_logs_have_short_retention_and_strict_limits(self):
        loki = _values(LOKI_VALUES)
        self.assertEqual(loki["loki"]["limits_config"]["retention_period"], "72h")
        self.assertEqual(loki["loki"]["limits_config"]["ingestion_rate_mb"], 2)
        self.assertEqual(loki["loki"]["limits_config"]["ingestion_burst_size_mb"], 4)
        self.assertEqual(
            loki["singleBinary"]["resources"],
            {"requests": {"memory": "256Mi", "cpu": "50m"}, "limits": {"memory": "768Mi", "cpu": "250m"}},
        )
        self.assertEqual(
            loki["sidecar"]["resources"],
            {"requests": {"memory": "48Mi", "cpu": "5m"}, "limits": {"memory": "96Mi", "cpu": "50m"}},
        )

        alloy = _values(ALLOY_VALUES)
        self.assertEqual(
            alloy["alloy"]["resources"],
            {"requests": {"memory": "96Mi", "cpu": "25m"}, "limits": {"memory": "192Mi", "cpu": "150m"}},
        )
        self.assertEqual(
            alloy["configReloader"]["resources"],
            {"requests": {"memory": "32Mi", "cpu": "5m"}, "limits": {"memory": "64Mi", "cpu": "50m"}},
        )

    def test_runbook_documents_staged_resume_losses_and_abort_contract(self):
        text = MONITORING_DOC.read_text(encoding="utf-8")
        for required in (
            "저용량 상시 프로필",
            "Phase A",
            "Phase B",
            "Phase C",
            "cAdvisor",
            "metrics-server",
            "CPU steal",
            "CPU PSI avg60",
            "즉시 pause 복귀",
            "Grafana replicas를 0",
            "for app in monitoring-prometheus monitoring-loki monitoring-alloy",
            "annotate application root",
            "pre-merge gate",
            "EXPECTED_REVISION",
            "EXPECTED_ROLLBACK_REVISION",
            "status.sync.revision",
            "--request-timeout=5s",
            "scale deployment/kube-prometheus-stack-operator",
            "--replicas=0 --request-timeout=5s",
            "scale_if_exists",
            "PVC_INVENTORY_FILE",
            "verify_monitoring_pvc_inventory.py",
            "endpointslices",
            "select(.conditions.ready != false)",
            "최대 3일",
            "4GB",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)

        marker = "```bash\nset -euo pipefail\n\n# 1) child Application"
        start = text.index(marker) + len("```bash\n")
        rollback = text[start : text.index("\n```", start)].replace("\\\n", " ")
        for command in rollback.splitlines():
            if "kubectl " in command:
                with self.subTest(command=command):
                    self.assertIn("--request-timeout=", command)

        pre_merge = text[text.index("**pre-merge gate**") : text.index("merge 후 root를 unpause")]
        self.assertIn("```bash\nset -euo pipefail", pre_merge)

    def test_canonical_docs_agree_metrics_server_is_off_in_low_capacity_mode(self):
        architecture = ARCHITECTURE_DOC.read_text(encoding="utf-8")
        metrics_server = METRICS_SERVER_DOC.read_text(encoding="utf-8")
        runbook = RUNBOOK_DOC.read_text(encoding="utf-8")
        self.assertIn("저용량 상시 프로필에서는 metrics-server Deployment를 0", architecture)
        self.assertIn("저용량 상시 프로필에서는 Deployment replicas를 0", metrics_server)
        self.assertIn("metrics-server가 Ready일 때만", runbook)

    def test_disabled_metrics_server_suppresses_expected_aggregated_api_alert(self):
        values = _values(PROMETHEUS_VALUES)
        self.assertIs(
            values["defaultRules"]["disabled"]["KubeAggregatedAPIDown"],
            True,
        )
        groups = values["additionalPrometheusRulesMap"][
            "pinlog-aggregated-api-availability"
        ]["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "pinlog-aggregated-api-availability")
        self.assertEqual(len(groups[0]["rules"]), 1)
        rule = groups[0]["rules"][0]
        self.assertEqual(rule["alert"], "KubeAggregatedAPIDown")
        self.assertEqual(rule["for"], "5m")
        self.assertEqual(rule["labels"], {"severity": "warning"})
        self.assertEqual(
            rule["expr"].strip(),
            "(1 - max by (name, namespace, cluster) (\n"
            "  avg_over_time(aggregator_unavailable_apiservice{\n"
            '    job="apiserver", name!="v1beta1.metrics.k8s.io"\n'
            "  }[10m])\n"
            ")) * 100 < 85",
        )
        self.assertEqual(
            rule["annotations"],
            {
                "description": "Kubernetes aggregated API {{ $labels.name }}/{{ $labels.namespace }} has been only {{ $value | humanize }}% available over the last 10m on cluster {{ $labels.cluster }}.",
                "runbook_url": "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubeaggregatedapidown",
                "summary": "Kubernetes aggregated API is down.",
            },
        )

    def test_external_probe_matches_grafana_off_phase(self):
        workflow = (ROOT / ".github/workflows/external-https-monitor.yaml").read_text(
            encoding="utf-8"
        )
        probe = (ROOT / ".github/scripts/external_https_probe.py").read_text(
            encoding="utf-8"
        )
        alerting = (ROOT / "docs/alerting.md").read_text(encoding="utf-8")

        self.assertNotIn("/grafana/login", workflow)
        self.assertNotIn("/grafana/login", probe)
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertNotIn("github.event.inputs", workflow)
        self.assertEqual(workflow.count("secrets.MATTERMOST_WEBHOOK_URL"), 1)
        self.assertIn("https://i15a705.p.ssafy.io/", workflow)
        self.assertIn('os.getenv("EXPECT_STATUS", "404")', probe)
        self.assertIn('EXPECT_STATUS: "404"', workflow)
        self.assertIn("Traefik", alerting)
        self.assertIn("기대 HTTP status `404`", alerting)
        self.assertIn("Phase C", alerting)


if __name__ == "__main__":
    unittest.main()
