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

EXPECTED_TITLES = {
    1: "백엔드 연결 상태",
    2: "확인 필요한 알림",
    3: "서버 CPU 사용률",
    4: "서버 메모리 사용률",
    5: "서버 디스크 사용률",
    6: "초당 백엔드 요청",
    7: "실행 가능한 서비스 인스턴스",
    8: "컨테이너 재시작",
    9: "요청 및 서버 오류 추이",
    10: "평균 응답 시간",
    11: "DB 연결 대기 요청",
    12: "현재 발생한 알림 상세",
    13: "서비스별 로그 발생량",
    14: "최근 오류 로그",
}

EXPECTED_ROWS = {
    "① 지금 서비스가 정상인가요?",
    "② 사용자가 느끼는 요청 상태",
    "③ 서버 자원이 충분한가요?",
    "④ 지금 확인할 알림과 로그",
}


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

    def test_dashboard_has_four_question_rows_and_korean_leaf_titles(self):
        panels = self.dashboard()["panels"]
        rows = [panel for panel in panels if panel["type"] == "row"]
        leaves = [panel for panel in panels if panel["type"] != "row"]

        self.assertEqual(len(rows), 4)
        self.assertEqual({row["title"] for row in rows}, EXPECTED_ROWS)
        self.assertEqual([panel["id"] for panel in leaves], [1, 2, 7, 8, 6, 9, 10, 11, 3, 4, 5, 12, 13, 14])
        self.assertEqual({panel["id"] for panel in leaves}, set(range(1, 15)))
        self.assertEqual(
            {panel["id"]: panel["title"] for panel in leaves}, EXPECTED_TITLES
        )

    def test_every_leaf_panel_explains_meaning_normal_and_first_check(self):
        leaves = [
            panel for panel in self.dashboard()["panels"] if panel["type"] != "row"
        ]
        for panel in leaves:
            with self.subTest(panel_id=panel["id"]):
                description = panel.get("description", "")
                for marker in ("의미:", "정상:", "확인:"):
                    self.assertEqual(description.count(marker), 1)

    def test_health_mappings_thresholds_and_neutral_panels_are_explicit(self):
        panels = {panel["id"]: panel for panel in self.dashboard()["panels"]}

        mappings_1 = panels[1]["fieldConfig"]["defaults"].get("mappings", [])
        mappings_2 = panels[2]["fieldConfig"]["defaults"].get("mappings", [])
        self.assertTrue(any(mapping.get("options", {}).get("1", {}).get("text") == "정상" and mapping.get("options", {}).get("0", {}).get("text") == "연결 안 됨" for mapping in mappings_1))
        self.assertTrue(any(mapping.get("options", {}).get("0", {}).get("text") == "정상" for mapping in mappings_2))
        self.assertTrue(any(mapping.get("options", {}).get("result", {}).get("text") == "확인 필요" and mapping.get("options", {}).get("from") == 1 for mapping in mappings_2))

        expected_thresholds = {
            3: [("green", None), ("yellow", 70), ("red", 90)],
            4: [("green", None), ("yellow", 75), ("red", 90)],
            5: [("green", None), ("yellow", 75), ("red", 90)],
        }
        for panel_id, expected in expected_thresholds.items():
            steps = panels[panel_id]["fieldConfig"]["defaults"]["thresholds"]["steps"]
            self.assertEqual([(step["color"], step["value"]) for step in steps], expected)
        for panel_id in (6, 10, 11):
            self.assertNotIn("thresholds", panels[panel_id]["fieldConfig"]["defaults"])

    def test_http_rate_uses_korean_legends_and_red_5xx_override(self):
        panel = next(panel for panel in self.dashboard()["panels"] if panel["id"] == 9)
        self.assertEqual(
            [target["legendFormat"] for target in panel["targets"]],
            ["전체 요청", "5xx 서버 오류"],
        )
        self.assertTrue(
            any(
                override.get("matcher", {}).get("options") == "5xx 서버 오류"
                and any(
                    prop.get("id") == "color" and prop.get("value", {}).get("fixedColor") == "red"
                    for prop in override.get("properties", [])
                )
                for override in panel["fieldConfig"]["overrides"]
            )
        )

    def test_dashboard_identity_time_and_datasources_are_preserved(self):
        dashboard = self.dashboard()
        self.assertEqual(dashboard["uid"], "pinlog-operations")
        self.assertEqual(dashboard["refresh"], "30s")
        self.assertEqual(dashboard["time"], {"from": "now-6h", "to": "now"})
        self.assertFalse(dashboard["editable"])
        datasource_uids = {
            panel.get("datasource", {}).get("uid")
            for panel in dashboard["panels"]
            if isinstance(panel.get("datasource"), dict)
        }
        self.assertEqual(datasource_uids, {"prometheus", "P8E80F9AEF21F6940"})


if __name__ == "__main__":
    unittest.main()
