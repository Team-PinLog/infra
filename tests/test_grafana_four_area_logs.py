import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ALLOY = ROOT / "platform/monitoring/alloy-values.yaml"
DASH_DIR = ROOT / "platform/monitoring/dashboards"
KUSTOMIZATION = DASH_DIR / "kustomization.yaml"

EXPECTED = {
    "pinlog-area-logs": "PinLog 영역별 로그 관제",
    "pinlog-frontend-logs": "PinLog Frontend 로그",
    "pinlog-backend-logs": "PinLog Backend 로그",
    "pinlog-ai-logs": "PinLog AI 로그",
    "pinlog-infra-logs": "PinLog Infra 로그",
}
AREAS = ("frontend", "backend", "ai", "infra")


def dashboard_files():
    return sorted(DASH_DIR.glob("pinlog-*-logs.dashboard"))


def dashboards():
    return [json.loads(path.read_text(encoding="utf-8")) for path in dashboard_files()]


def leaves(dashboard):
    return [panel for panel in dashboard["panels"] if panel.get("type") != "row"]


def alloy_replace_stages(config):
    """Extract replace stages and execute the RE2-compatible subset with Go expansion."""
    stages = re.findall(
        r"stage\.replace\s*\{\s*expression\s*=\s*`([^`]*)`\s*replace\s*=\s*`([^`]*)`\s*\}",
        config,
        re.S,
    )
    if not stages:
        raise AssertionError("no Alloy stage.replace blocks extracted")
    return stages


def go_replace_all(expression, replacement, value):
    # These are the unsupported constructs most likely to accidentally make an
    # expression work in Python while failing in Go RE2.
    if re.search(r"\(\?(?:[=!<]|P<)|\\[1-9]", expression):
        raise AssertionError(f"not RE2-compatible: {expression}")
    pattern = re.compile(expression)

    def expand(match):
        return re.sub(
            r"\$\{(\d+)\}|\$(\d+)",
            lambda token: match.group(int(token.group(1) or token.group(2))) or "",
            replacement,
        )

    return pattern.sub(expand, value)


def sanitize(config, value):
    for expression, replacement in alloy_replace_stages(config):
        value = go_replace_all(expression, replacement, value)
    return value


def area_for(config, namespace, app):
    for block in re.findall(r"rule\s*\{(.*?)\n\s*\}", config, re.S):
        if 'target_label  = "area"' not in block:
            continue
        expression = re.search(r'regex\s*=\s*"([^"]+)"', block).group(1)
        replacement = re.search(r'replacement\s*=\s*"([^"]+)"', block).group(1)
        if re.fullmatch(expression, f"{namespace};{app}"):
            return replacement
    return None


class AlloyFourAreaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = ALLOY.read_text(encoding="utf-8")
        cls.config = yaml.safe_load(cls.raw)["alloy"]["configMap"]["content"]

    def test_exact_four_area_rules_and_future_ai_selector(self):
        replacements = re.findall(r'target_label\s*=\s*"area".*?replacement\s*=\s*"([^"]+)"', self.config, re.S)
        self.assertEqual(set(replacements), set(AREAS))
        self.assertEqual(len(replacements), 4)
        self.assertRegex(self.config, r'regex\s*=\s*"pinlog-dev;(?:front|\\(front\\))"')
        self.assertRegex(self.config, r'regex\s*=\s*"pinlog-prod;(?:back|\\(back\\))"')
        self.assertIn('regex         = "pinlog-(dev|prod);ai"', self.config)

    def test_infra_area_is_an_exact_allowlist(self):
        for allowed in ("argocd", "monitoring", "traefik", "sealed-secrets", "cloudflared"):
            self.assertIn(allowed, self.config)
        self.assertNotRegex(self.config, r'kube-system;\.\*')
        self.assertNotRegex(self.config, r'regex\s*=\s*"kube-system".*?replacement\s*=\s*"infra"')
        self.assertNotRegex(self.config, r'namespace.*replacement\s*=\s*"infra"')

    def test_canonical_low_cardinality_labels_only(self):
        for label in ("area", "environment", "namespace", "workload", "pod", "container"):
            self.assertIn(f'target_label  = "{label}"', self.config)
        for forbidden in ("user_id", "request_id", "query_string", "message"):
            self.assertNotIn(f'target_label  = "{forbidden}"', self.config)

    def test_redaction_precedes_loki_write_and_covers_sensitive_classes(self):
        process = self.config.index('loki.process "sanitize"')
        write = self.config.index('loki.write "default"')
        self.assertLess(process, write)
        self.assertIn("[REDACTED]", self.config)
        for token in ("Authorization", "Bearer", "Cookie", "Set-Cookie", "code", "state", "access_token", "refresh_token", "id_token", "client_secret", "eyJ", "@"):
            self.assertIn(token, self.config)
        self.assertIn("forward_to = [loki.process.sanitize.receiver]", self.config)
        self.assertIn("forward_to = [loki.write.default.receiver]", self.config[process:write])

    def test_authorization_json_and_plain_values_are_redacted(self):
        fixtures = (
            ('{"Authorization":"Bearer synthetic-A1", "route":"/pins", "status":200}',
             '{"Authorization":"[REDACTED]", "route":"/pins", "status":200}'),
            ('authorization=synthetic-B2 route=/login level=info',
             'authorization=[REDACTED] route=/login level=info'),
            ('AUTHORIZATION: bearer synthetic-Case3 status=401',
             'AUTHORIZATION: [REDACTED] status=401'),
        )
        for source, expected in fixtures:
            with self.subTest(source=source):
                self.assertEqual(sanitize(self.config, source), expected)

    def test_standalone_bearer_is_redacted_without_swallowing_route(self):
        self.assertEqual(
            sanitize(self.config, 'token Bearer synthetic-D4 route=/oauth status=302'),
            'token Bearer [REDACTED] route=/oauth status=302',
        )

    def test_cookie_json_and_plain_semicolon_values_are_redacted(self):
        fixtures = (
            ('{"Cookie":"sid=fake; theme=dark", "route":"/pins", "level":"info"}',
             '{"Cookie":"[REDACTED]", "route":"/pins", "level":"info"}'),
            ('Set-Cookie: sid=fake; Path=/; HttpOnly status=200 route=/login',
             'Set-Cookie: [REDACTED] status=200 route=/login'),
            ('cookie=sid=fake;theme=dark level=debug status=200',
             'cookie=[REDACTED] level=debug status=200'),
        )
        for source, expected in fixtures:
            with self.subTest(source=source):
                self.assertEqual(sanitize(self.config, source), expected)

    def test_oauth_query_form_and_json_values_are_redacted(self):
        fixtures = (
            ('GET /cb?code=fake-code&state=fake-state route=/cb status=302',
             'GET /cb?code=[REDACTED]&state=[REDACTED] route=/cb status=302'),
            ('access_token=fake-a&refresh_token=fake-r level=info',
             'access_token=[REDACTED]&refresh_token=[REDACTED] level=info'),
            ('{"id_token":"fake-i","client_secret":"fake-s","status":400}',
             '{"id_token":"[REDACTED]","client_secret":"[REDACTED]","status":400}'),
        )
        for source, expected in fixtures:
            with self.subTest(source=source):
                self.assertEqual(sanitize(self.config, source), expected)

    def test_jwt_and_email_remain_redacted(self):
        source = 'email=person@example.test jwt=eyJabcdefgh.ijklmnop.qrstuvwx route=/me'
        self.assertEqual(
            sanitize(self.config, source),
            '[REDACTED] jwt=[REDACTED] route=/me',
        )

    def test_replace_expressions_use_re2_supported_constructs(self):
        for expression, _ in alloy_replace_stages(self.config):
            go_replace_all(expression, "[X]", "synthetic fixture")

    def test_live_dev_tunnel_app_names_map_to_infra(self):
        for app in ("cowork-cloudflared", "pinlog-web-cloudflared"):
            self.assertEqual(area_for(self.config, "pinlog-dev", app), "infra")

    def test_arbitrary_dev_apps_do_not_map_to_infra(self):
        for app in ("api", "worker", "cloudflared-copy", "unrelated"):
            self.assertIsNone(area_for(self.config, "pinlog-dev", app))

    def test_kube_system_infra_allowlist_remains_exact(self):
        for app in ("traefik", "sealed-secrets"):
            self.assertEqual(area_for(self.config, "kube-system", app), "infra")
        for app in ("coredns", "metrics-server", "traefik-copy"):
            self.assertIsNone(area_for(self.config, "kube-system", app))

    def test_frontend_backend_and_future_ai_contract_remains_exact(self):
        self.assertEqual(area_for(self.config, "pinlog-dev", "front"), "frontend")
        self.assertEqual(area_for(self.config, "pinlog-prod", "back"), "backend")
        self.assertEqual(area_for(self.config, "pinlog-dev", "ai"), "ai")
        self.assertEqual(area_for(self.config, "pinlog-prod", "ai"), "ai")
        self.assertIsNone(area_for(self.config, "pinlog-prod", "front"))
        self.assertIsNone(area_for(self.config, "pinlog-dev", "back"))


class GrafanaFourAreaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = dashboards()
        cls.by_uid = {item["uid"]: item for item in cls.items}

    def test_exact_five_stable_uids_titles_and_gitops_provisioning(self):
        self.assertEqual({item["uid"]: item["title"] for item in self.items}, EXPECTED)
        kustomization = yaml.safe_load(KUSTOMIZATION.read_text(encoding="utf-8"))
        files = {entry for generator in kustomization["configMapGenerator"] for entry in generator["files"]}
        for uid in EXPECTED:
            self.assertIn(f"{uid}.json={uid}.dashboard", files)

    def test_overview_layout_and_all_area_query_coverage(self):
        overview = self.by_uid["pinlog-area-logs"]
        panels = leaves(overview)
        cards = [p for p in panels if p["type"] == "stat" and p["gridPos"]["y"] == 0]
        self.assertEqual(len(cards), 4)
        self.assertEqual([(p["gridPos"]["w"], p["gridPos"]["x"]) for p in cards], [(6, 0), (6, 6), (6, 12), (6, 18)])
        warning_logs = [p for p in panels if p["type"] == "logs" and p["gridPos"]["w"] == 12]
        self.assertEqual(len(warning_logs), 4)
        self.assertEqual({(p["gridPos"]["x"], p["gridPos"]["y"]) for p in warning_logs}, {(0, 4), (12, 4), (0, 12), (12, 12)})
        joined = json.dumps(overview, ensure_ascii=False)
        for area in AREAS:
            self.assertIn(f'area=\\"{area}\\"', joined)
        trend = next(p for p in panels if p["type"] == "timeseries")
        self.assertEqual(len(trend["targets"]), 4)

    def test_detail_variables_queries_wording_and_bounded_raw_logs(self):
        expected_vars = {"DS_LOKI", "environment", "namespace", "workload", "pod", "container", "search"}
        for area in AREAS:
            dashboard = self.by_uid[f"pinlog-{area}-logs"]
            names = {item["name"] for item in dashboard["templating"]["list"]}
            self.assertEqual(names, expected_vars)
            raw = next(p for p in leaves(dashboard) if "전체 로그" in p["title"])
            self.assertEqual(raw["gridPos"]["w"], 24)
            self.assertLessEqual(raw["options"]["maxLines"], 500)
            expressions = [target["expr"] for panel in leaves(dashboard) for target in panel["targets"]]
            self.assertTrue(all(f'area="{area}"' in expression for expression in expressions))
            self.assertTrue(any("일반 로그" in p["title"] for p in leaves(dashboard)))
            self.assertTrue(any("경고·오류" in p["title"] for p in leaves(dashboard)))
        self.assertIn("배포되지 않음", json.dumps(self.by_uid["pinlog-ai-logs"], ensure_ascii=False))
        self.assertIn("수집기 장애", json.dumps(self.by_uid["pinlog-ai-logs"], ensure_ascii=False))
        self.assertIn("브라우저 JS console", json.dumps(self.by_uid["pinlog-frontend-logs"], ensure_ascii=False))

    def test_leaf_descriptions_neutral_volume_datasource_and_links(self):
        for dashboard in self.items:
            self.assertFalse(dashboard["editable"])
            self.assertTrue(any(link.get("type") == "link" and "explore" in link.get("url", "").lower() for link in dashboard["links"]))
            for panel in leaves(dashboard):
                for marker in ("의미:", "정상:", "확인:"):
                    self.assertEqual(panel.get("description", "").count(marker), 1, (dashboard["uid"], panel["id"], marker))
                self.assertEqual(panel["datasource"], {"type": "loki", "uid": "$DS_LOKI"})
                if panel["type"] in ("stat", "timeseries"):
                    self.assertNotIn("thresholds", panel.get("fieldConfig", {}).get("defaults", {}))
            self.assertNotIn("P8E80F9AEF21F6940", json.dumps(dashboard))

    def test_dashboard_navigation_uses_stable_uid_urls(self):
        overview = self.by_uid["pinlog-area-logs"]
        urls = {link.get("url") for link in overview["links"]}
        for area in AREAS:
            self.assertTrue(any(url and f"/d/pinlog-{area}-logs/" in url for url in urls))
        for area in AREAS:
            detail_urls = {link.get("url") for link in self.by_uid[f"pinlog-{area}-logs"]["links"]}
            self.assertTrue(any(url and "/d/pinlog-area-logs/" in url for url in detail_urls))

    def test_no_raw_credentials_or_high_cardinality_query_labels(self):
        raw = "\n".join(path.read_text(encoding="utf-8") for path in dashboard_files())
        for forbidden in ("password=", "access_token=", "refresh_token=", "client_secret=", "Authorization: Bearer "):
            self.assertNotIn(forbidden, raw)
        for label in ("user_id", "request_id", "query_string", "message"):
            self.assertNotRegex(raw, rf'[{{,"]\s*{label}\s*=')


if __name__ == "__main__":
    unittest.main()
