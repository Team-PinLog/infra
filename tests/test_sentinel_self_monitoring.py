from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES_PATH = ROOT / "platform" / "monitoring" / "kube-prometheus-stack-values.yaml"
ALERTING_DOC = ROOT / "docs" / "alerting.md"
COMPONENT = "sentinel-self-monitoring"
WEBHOOK_FILE = "/etc/alertmanager/secrets/mattermost-alert-webhook/url"


def _values() -> dict:
    return yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))


def _rule_by_alert(values: dict) -> dict:
    groups = values["additionalPrometheusRulesMap"]["pinlog-sentinel-self-monitoring"]["groups"]
    return {rule["alert"]: rule for group in groups for rule in group["rules"]}


class SentinelSelfMonitoringTest(unittest.TestCase):
    def test_fallback_route_is_first_and_strict_without_changing_ordinary_routes(self):
        config = _values()["alertmanager"]["config"]
        routes = config["route"]["routes"]
        self.assertEqual(
            routes[0],
            {
                "receiver": "sentinel-fallback",
                "matchers": [f'component="{COMPONENT}"'],
                "repeat_interval": "1h",
            },
        )
        self.assertEqual(
            routes[1:],
            [
                {"receiver": "null", "matchers": ['alertname="Watchdog"']},
                {
                    "receiver": "pinlog-sentinel",
                    "matchers": ['severity="critical"'],
                    "repeat_interval": "1h",
                },
                {
                    "receiver": "pinlog-sentinel",
                    "matchers": ['severity="warning"'],
                    "repeat_interval": "6h",
                },
            ],
        )

    def test_fallback_uses_only_mounted_file_credential_and_safe_message_contract(self):
        values = _values()
        secrets = values["alertmanager"]["alertmanagerSpec"]["secrets"]
        self.assertIn("mattermost-alert-webhook", secrets)
        receiver = next(
            receiver
            for receiver in values["alertmanager"]["config"]["receivers"]
            if receiver["name"] == "sentinel-fallback"
        )
        self.assertEqual(set(receiver), {"name", "slack_configs"})
        self.assertEqual(len(receiver["slack_configs"]), 1)
        slack = receiver["slack_configs"][0]
        self.assertEqual(slack["api_url_file"], WEBHOOK_FILE)
        self.assertNotIn("api_url", slack)
        self.assertTrue(slack["send_resolved"])
        for forbidden in ("username", "icon_emoji", "icon_url"):
            self.assertNotIn(forbidden, slack)
        self.assertIn("Sentinel 자체 감시", slack["title"])
        self.assertNotIn("{{", slack["title"])
        text = slack["text"]
        self.assertEqual(text.count("@channel"), 1)
        self.assertIn('eq .CommonLabels.severity "critical"', text)
        self.assertNotIn(".Annotations", text)
        self.assertNotIn("@all", text)
        self.assertNotIn("@here", text)
        self.assertNotIn("http", text)

        serialized = VALUES_PATH.read_text(encoding="utf-8")
        self.assertNotIn("hooks/", serialized)
        self.assertNotIn("encryptedData", serialized)

    def test_rules_cover_down_delivery_dead_letter_and_input_health(self):
        rules = _rule_by_alert(_values())
        self.assertEqual(
            set(rules),
            {
                "PinLogSentinelDown",
                "PinLogSentinelDeliveryFailed",
                "PinLogSentinelDeadLetterAdded",
                "PinLogSentinelInputRejectedOrBusy",
            },
        )
        for rule in rules.values():
            self.assertEqual(rule["labels"]["component"], COMPONENT)

        down = rules["PinLogSentinelDown"]
        self.assertEqual(down["labels"]["severity"], "critical")
        self.assertEqual(down["for"], "3m")
        self.assertIn('up{job="scrapeConfig/monitoring/pinlog-sentinel-receiver"} == 0', down["expr"])
        self.assertIn('absent(up{job="scrapeConfig/monitoring/pinlog-sentinel-receiver"})', down["expr"])

        failed = rules["PinLogSentinelDeliveryFailed"]
        self.assertEqual(failed["labels"]["severity"], "warning")
        self.assertEqual(
            failed["expr"].strip(),
            'increase(pinlog_sentinel_receiver_events_total{result="failed"}[10m]) > 0',
        )

        dead_letter = rules["PinLogSentinelDeadLetterAdded"]
        self.assertEqual(dead_letter["labels"]["severity"], "warning")
        self.assertEqual(
            dead_letter["expr"].strip(),
            "delta(pinlog_sentinel_receiver_dead_letters[10m]) > 0",
        )
        self.assertNotIn("pinlog_sentinel_receiver_dead_letters >", dead_letter["expr"])

        rejected = rules["PinLogSentinelInputRejectedOrBusy"]
        self.assertEqual(rejected["labels"]["severity"], "warning")
        self.assertEqual(
            rejected["expr"].strip(),
            'sum(increase(pinlog_sentinel_receiver_events_total{result=~"rejected|busy"}[10m])) > 0',
        )

    def test_runbook_documents_circular_dependency_and_gitops_rollback(self):
        doc = ALERTING_DOC.read_text(encoding="utf-8")
        self.assertIn("Sentinel 자체 감시 fallback", doc)
        self.assertIn("순환 의존", doc)
        self.assertIn("git revert", doc)
        self.assertIn("mattermost-alert-webhook", doc)
        self.assertIn("현재 dead_letters 값 자체", doc)


if __name__ == "__main__":
    unittest.main()
