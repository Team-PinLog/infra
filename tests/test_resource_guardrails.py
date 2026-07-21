from pathlib import Path
import shlex
import unittest

import yaml

from tools.validate_rendered_resource_guardrails import _validate_resources


ROOT = Path(__file__).resolve().parents[1]
NAMESPACES = ROOT / "platform" / "namespaces" / "namespaces.yaml"


def _documents(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _executable_helm_versions(run_script: str) -> dict[str, str]:
    logical_lines: list[str] = []
    continued = ""
    for raw_line in run_script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            continued += line[:-1] + " "
        else:
            logical_lines.append(continued + line)
            continued = ""
    if continued:
        raise ValueError("unterminated shell continuation")

    versions: dict[str, str] = {}
    for logical_line in logical_lines:
        lexer = shlex.shlex(logical_line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        segment: list[str] = []
        segments: list[list[str]] = []
        for token in lexer:
            if token in {";", "&", "&&", "|", "||"}:
                if segment:
                    segments.append(segment)
                    segment = []
            else:
                segment.append(token)
        if segment:
            segments.append(segment)

        for tokens in segments:
            helm_locations = [
                index
                for index in range(len(tokens) - 1)
                if tokens[index : index + 2] == ["helm", "template"]
            ]
            if not helm_locations:
                if "helm template" in " ".join(tokens):
                    raise ValueError("unrecognized helm template execution")
                continue
            if helm_locations != [0]:
                raise ValueError("helm template must be a direct command")
            if tokens.count("--version") != 1:
                raise ValueError("helm template requires exactly one --version")
            chart = tokens[3].rsplit("/", 1)[-1]
            if chart in versions:
                raise ValueError(f"duplicate helm template command for {chart}")
            versions[chart] = tokens[tokens.index("--version") + 1]
    return versions


class ResourceGuardrailsTest(unittest.TestCase):
    def test_prod_namespace_has_capacity_based_quota_and_defaults(self):
        documents = _documents(NAMESPACES)
        prod_namespace = next(
            doc
            for doc in documents
            if doc.get("kind") == "Namespace" and doc["metadata"]["name"] == "pinlog-prod"
        )
        quota = next(
            doc
            for doc in documents
            if doc.get("kind") == "ResourceQuota"
            and doc["metadata"].get("namespace") == "pinlog-prod"
        )
        self.assertEqual(
            quota["spec"]["hard"],
            {
                "requests.cpu": "2",
                "requests.memory": "6Gi",
                "limits.cpu": "4",
                "limits.memory": "8Gi",
                "pods": "30",
                "persistentvolumeclaims": "10",
                "requests.storage": "50Gi",
            },
        )

        limit_range = next(
            doc
            for doc in documents
            if doc.get("kind") == "LimitRange"
            and doc["metadata"].get("namespace") == "pinlog-prod"
        )
        container = next(
            item
            for item in limit_range["spec"]["limits"]
            if item["type"] == "Container"
        )
        self.assertEqual(container["defaultRequest"], {"cpu": "100m", "memory": "128Mi"})
        self.assertEqual(container["default"], {"cpu": "500m", "memory": "768Mi"})
        self.assertEqual(container["max"], {"cpu": "2", "memory": "2Gi"})
        self.assertEqual(
            limit_range["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
            "-1",
        )
        self.assertEqual(
            prod_namespace["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
            "-2",
        )
        for namespace in (doc for doc in documents if doc.get("kind") == "Namespace"):
            self.assertEqual(
                namespace["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
                "-2",
            )
        self.assertEqual(
            quota["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"],
            "0",
        )
        waves = [
            int(prod_namespace["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
            int(limit_range["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
            int(quota["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
        ]
        self.assertEqual(waves, sorted(waves))
        self.assertEqual(len(waves), len(set(waves)))

        postgres = _documents(ROOT / "platform" / "postgres" / "statefulset.yaml")[0]
        backup_docs = _documents(ROOT / "platform" / "postgres" / "backup-cronjob.yaml")
        pvc_requests = [
            claim["spec"]["resources"]["requests"]["storage"]
            for claim in postgres["spec"]["volumeClaimTemplates"]
        ]
        pvc_requests.extend(
            doc["spec"]["resources"]["requests"]["storage"]
            for doc in backup_docs
            if doc.get("kind") == "PersistentVolumeClaim"
        )
        requested_gib = sum(int(value.removesuffix("Gi")) for value in pvc_requests)
        quota_gib = int(quota["spec"]["hard"]["requests.storage"].removesuffix("Gi"))
        self.assertEqual(requested_gib, 30)
        self.assertLess(requested_gib, quota_gib)

    def test_prod_workloads_declare_cpu_and_memory_requests_and_limits(self):
        workload_files = [
            ROOT / "platform" / "postgres" / "statefulset.yaml",
            ROOT / "platform" / "postgres" / "backup-cronjob.yaml",
            ROOT / "platform" / "redis" / "deployment.yaml",
        ]
        containers: list[tuple[str, dict]] = []
        for path in workload_files:
            for document in _documents(path):
                kind = document.get("kind")
                if kind in {"Deployment", "StatefulSet"}:
                    pod_spec = document["spec"]["template"]["spec"]
                elif kind == "CronJob":
                    pod_spec = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                else:
                    continue
                containers.extend(
                    (f"{path.name}:{container['name']}", container)
                    for container in pod_spec.get("containers", [])
                )

        chart_values = yaml.safe_load(
            (ROOT / "charts" / "microservice" / "values.yaml").read_text()
        )
        containers.append(("microservice defaults", {"resources": chart_values["resources"]}))

        for name, container in containers:
            with self.subTest(container=name):
                resources = container.get("resources", {})
                self.assertEqual(set(resources.get("requests", {})), {"cpu", "memory"})
                self.assertEqual(set(resources.get("limits", {})), {"cpu", "memory"})

    def test_gitops_monitoring_workloads_have_complete_resource_guards(self):
        prometheus = yaml.safe_load(
            (ROOT / "platform" / "monitoring" / "kube-prometheus-stack-values.yaml").read_text()
        )
        loki = yaml.safe_load(
            (ROOT / "platform" / "monitoring" / "loki-values.yaml").read_text()
        )
        alloy = yaml.safe_load(
            (ROOT / "platform" / "monitoring" / "alloy-values.yaml").read_text()
        )

        guarded = {
            "alertmanager": prometheus["alertmanager"]["alertmanagerSpec"]["resources"],
            "prometheus": prometheus["prometheus"]["prometheusSpec"]["resources"],
            "prometheus-operator": prometheus["prometheusOperator"]["resources"],
            "prometheus-admission-hooks": prometheus["prometheusOperator"]["admissionWebhooks"]["patch"]["resources"],
            "prometheus-config-reloader": prometheus["prometheusOperator"]["prometheusConfigReloader"]["resources"],
            "kube-state-metrics": prometheus["kube-state-metrics"]["resources"],
            "node-exporter": prometheus["prometheus-node-exporter"]["resources"],
            "grafana": prometheus["grafana"]["resources"],
            "grafana-sidecars": prometheus["grafana"]["sidecar"]["resources"],
            "grafana-init-chown-data": prometheus["grafana"]["initChownData"]["resources"],
            "loki": loki["singleBinary"]["resources"],
            "loki-rules-sidecar": loki["sidecar"]["resources"],
            "alloy": alloy["alloy"]["resources"],
            "alloy-config-reloader": alloy["configReloader"]["resources"],
        }
        for name, resources in guarded.items():
            with self.subTest(workload=name):
                self.assertEqual(set(resources.get("requests", {})), {"cpu", "memory"})
                self.assertEqual(set(resources.get("limits", {})), {"cpu", "memory"})

    def test_resource_guardrail_alerts_route_actionable_failures(self):
        values = yaml.safe_load(
            (ROOT / "platform" / "monitoring" / "kube-prometheus-stack-values.yaml").read_text()
        )
        groups = values["additionalPrometheusRulesMap"]["pinlog-resource-guardrails"]["groups"]
        alerts = {
            rule["alert"]: rule
            for group in groups
            for rule in group["rules"]
            if "alert" in rule
        }
        self.assertEqual(
            set(alerts),
            {
                "PinLogProdQuotaHigh",
                "PinLogProdContainerOOMKilled",
                "PinLogProdPodUnschedulable",
            },
        )
        for name, rule in alerts.items():
            with self.subTest(alert=name):
                self.assertEqual(rule["labels"]["severity"], "warning")
                self.assertIn("pinlog-prod", str(rule["expr"]))
                self.assertTrue(rule["annotations"]["summary"])
                self.assertTrue(rule["annotations"]["description"])

        normalize = lambda expression: " ".join(str(expression).split())
        self.assertEqual(
            normalize(alerts["PinLogProdQuotaHigh"]["expr"]),
            'max by (namespace, resource) ( kube_resourcequota{namespace="pinlog-prod", type="used"} / ignoring(type) kube_resourcequota{namespace="pinlog-prod", type="hard"} ) > 0.8',
        )
        self.assertEqual(alerts["PinLogProdQuotaHigh"]["for"], "10m")
        self.assertEqual(
            normalize(alerts["PinLogProdContainerOOMKilled"]["expr"]),
            '( kube_pod_container_status_last_terminated_reason{ namespace="pinlog-prod", reason="OOMKilled" } == 1 ) and on (namespace, pod, container) ( time() - kube_pod_container_status_last_terminated_timestamp{ namespace="pinlog-prod" } < 600 )',
        )
        self.assertNotIn("for", alerts["PinLogProdContainerOOMKilled"])
        self.assertEqual(
            normalize(alerts["PinLogProdPodUnschedulable"]["expr"]),
            'kube_pod_status_unschedulable{namespace="pinlog-prod"} == 1',
        )
        self.assertEqual(alerts["PinLogProdPodUnschedulable"]["for"], "5m")

        routes = values["alertmanager"]["config"]["route"]["routes"]
        warning_route = next(
            route for route in routes if 'severity="warning"' in route.get("matchers", [])
        )
        self.assertEqual(warning_route["receiver"], "pinlog-sentinel")
        self.assertEqual(warning_route["repeat_interval"], "6h")

    def test_ci_renders_and_validates_exact_pinned_monitoring_charts(self):
        workflow = (ROOT / ".github" / "workflows" / "validate.yaml").read_text()
        workflow_document = yaml.safe_load(workflow)
        render_step = next(
            step
            for step in workflow_document["jobs"]["helm"]["steps"]
            if step.get("name") == "monitoring exact pinned render + resource guardrails"
        )
        rendered_versions = _executable_helm_versions(render_step["run"])
        apps = {
            "kube-prometheus-stack": ROOT / "argocd" / "apps" / "monitoring-prometheus.yaml",
            "loki": ROOT / "argocd" / "apps" / "monitoring-loki.yaml",
            "alloy": ROOT / "argocd" / "apps" / "monitoring-alloy.yaml",
        }
        for chart, path in apps.items():
            app = yaml.safe_load(path.read_text())
            source = next(item for item in app["spec"]["sources"] if item.get("chart") == chart)
            self.assertEqual(rendered_versions.get(chart), source["targetRevision"])

        self.assertIn("tools/validate_rendered_resource_guardrails.py", workflow)
        self.assertIn("prometheus-3.13.1.linux-amd64.tar.gz", workflow)
        self.assertIn(
            "962b812371aff838d152b6ff2d56fdb7a6396f5542f48ebf73421b9721f0d103",
            workflow,
        )

    def test_chart_pin_parser_rejects_shell_and_flag_bypasses(self):
        with self.assertRaisesRegex(ValueError, "duplicate helm template"):
            _executable_helm_versions(
                "helm template loki grafana/loki --version 7.1.0 > good; "
                "helm template loki grafana/loki --version 99.0.0 > actual"
            )
        with self.assertRaisesRegex(ValueError, "exactly one --version"):
            _executable_helm_versions(
                "helm template loki grafana/loki --version 7.1.0 --version 99.0.0"
            )

    def test_render_validator_rejects_zero_empty_and_invalid_quantities(self):
        invalid_resources = [
            {"requests": {"cpu": "", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "128Mi"}},
            {"requests": {"cpu": "0", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "128Mi"}},
            {"requests": {"cpu": "10m", "memory": "bogus"}, "limits": {"cpu": "100m", "memory": "128Mi"}},
        ]
        for resources in invalid_resources:
            with self.subTest(resources=resources):
                errors = _validate_resources(
                    "Deployment/test", {"name": "test", "resources": resources}
                )
                self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
