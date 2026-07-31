from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES_PATH = ROOT / "platform" / "monitoring" / "kube-prometheus-stack-values.yaml"
ALERTING_DOC = ROOT / "docs" / "alerting.md"
COMPONENT = "sentinel-self-monitoring"
WEBHOOK_FILE = "/etc/alertmanager/secrets/mattermost-alert-webhook/url"
FALLBACK_TITLE_LINK = "https://monitoring.pin-log.com/alerting/list"


def _values() -> dict:
    return yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))


def _rule_by_alert(values: dict) -> dict:
    groups = values["additionalPrometheusRulesMap"]["pinlog-sentinel-self-monitoring"]["groups"]
    return {rule["alert"]: rule for group in groups for rule in group["rules"]}


def _fallback_mention_count(text: str, *, status: str, severity: str) -> int:
    """Evaluate the deliberately bounded fallback mention template contract."""
    match = re.fullmatch(
        r'\{\{ if and \(eq \.Status "firing"\) '
        r'\(eq \.CommonLabels\.severity "critical"\) \}\}'
        r'(?P<mention>@channel)\n\{\{ end \}\}'
        r'.*',
        text,
        flags=re.DOTALL,
    )
    if not match:
        raise AssertionError("fallback mention must use the bounded critical FIRING condition")
    return int(status == "firing" and severity == "critical")


def _render_fallback(template: str, *, status: str, alertname: str, severity: str) -> str:
    """Render the restricted Go-template branch subset used by this fallback."""
    values = {".Status": status, ".CommonLabels.alertname": alertname, ".CommonLabels.severity": severity}

    def condition(expression: str) -> bool:
        eq = re.fullmatch(r'eq (\.[A-Za-z.]+) "([^"]+)"', expression)
        if eq:
            return values[eq.group(1)] == eq.group(2)
        both = re.fullmatch(r'and \(eq (\.[A-Za-z.]+) "([^"]+)"\) \(eq (\.[A-Za-z.]+) "([^"]+)"\)', expression)
        if both:
            return values[both.group(1)] == both.group(2) and values[both.group(3)] == both.group(4)
        raise AssertionError(f"unsupported fallback condition: {expression}")

    tokens = re.split(r"(\{\{\s*(?:if .*?|else if .*?|else|end)\s*\}\})", template)
    active, stack, output = True, [], []
    for token in tokens:
        directive = re.fullmatch(r"\{\{\s*(.*?)\s*\}\}", token)
        if not directive:
            if active:
                output.append(token)
            continue
        command = directive.group(1)
        if command.startswith("if "):
            matched = condition(command[3:])
            stack.append({"parent": active, "matched": matched})
            active = active and matched
        elif command.startswith("else if "):
            frame = stack[-1]
            matched = not frame["matched"] and condition(command[8:])
            frame["matched"] = frame["matched"] or matched
            active = frame["parent"] and matched
        elif command == "else":
            frame = stack[-1]
            active = frame["parent"] and not frame["matched"]
            frame["matched"] = True
        elif command == "end":
            active = stack.pop()["parent"]
    if stack:
        raise AssertionError("unclosed fallback template branch")
    return "".join(output)


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
        self.assertEqual(slack["title_link"], FALLBACK_TITLE_LINK)
        self.assertTrue(slack["title_link"].startswith("https://"))
        for forbidden in (".svc", "alertmanager", "{{", ".Annotations", ".ExternalURL"):
            self.assertNotIn(forbidden, slack["title_link"])
        text = slack["text"]
        self.assertEqual(text.count("@channel"), 1)
        self.assertIn(
            '{{ if and (eq .Status "firing") (eq .CommonLabels.severity "critical") }}',
            text,
        )
        self.assertNotIn(".Annotations", text)
        self.assertNotIn("@all", text)
        self.assertNotIn("@here", text)
        self.assertNotIn("http", text)

        serialized = VALUES_PATH.read_text(encoding="utf-8")
        self.assertNotIn("hooks/", serialized)
        self.assertNotIn("encryptedData", serialized)

    def test_fallback_mentions_only_critical_firing(self):
        receiver = next(
            receiver
            for receiver in _values()["alertmanager"]["config"]["receivers"]
            if receiver["name"] == "sentinel-fallback"
        )
        text = receiver["slack_configs"][0]["text"]
        cases = {
            ("firing", "critical"): 1,
            ("resolved", "critical"): 0,
            ("firing", "warning"): 0,
            ("resolved", "warning"): 0,
        }
        for (status, severity), expected in cases.items():
            with self.subTest(status=status, severity=severity):
                self.assertEqual(
                    _fallback_mention_count(text, status=status, severity=severity),
                    expected,
                )

    def test_fallback_renders_fixed_child_friendly_korean_for_known_and_unknown_alerts(self):
        receiver = next(
            receiver for receiver in _values()["alertmanager"]["config"]["receivers"]
            if receiver["name"] == "sentinel-fallback"
        )
        slack = receiver["slack_configs"][0]
        firing = {
            "PinLogSentinelDown": ("🚨 알림 전달 로봇이 멈췄어요", "무슨 일: Sentinel이 3분 동안 대답하지 않았어요.\n어떤 영향: 서비스에 문제가 생겨도 Mattermost 알림이 안 올 수 있어요.\n서비스 상태: 서비스 자체가 고장났다는 뜻은 아니에요.\n담당자: Infra · 김세민\n지금 할 일: Sentinel 상태를 확인해 주세요."),
            "PinLogSentinelDeliveryFailed": ("⚠️ 알림을 보냈지만 도착하지 않았어요", "무슨 일: Sentinel이 알림을 보냈지만 Mattermost에 도착하지 않았어요.\n어떤 영향: 운영 알림 일부를 놓칠 수 있어요.\n담당자: Infra · 김세민\n지금 할 일: Mattermost 연결을 확인해 주세요."),
            "PinLogSentinelDeadLetterAdded": ("⚠️ 못 보낸 알림이 보관함에 들어갔어요", "무슨 일: 보내지 못한 알림이 실패 알림 보관함에 들어갔어요.\n어떤 영향: 운영 알림 일부를 놓칠 수 있어요.\n담당자: Infra · 김세민\n지금 할 일: 실패 알림 보관함을 확인해 주세요."),
            "PinLogSentinelInputRejectedOrBusy": ("⚠️ 알림 전달 로봇이 너무 바빠요", "무슨 일: Sentinel이 너무 바빠서 새 알림을 바로 받지 못했어요.\n어떤 영향: 새 알림이 늦게 오거나 안 올 수 있어요.\n담당자: Infra · 김세민\n지금 할 일: Sentinel 사용량을 확인해 주세요."),
            "PinLogSentinelURLLinkCanary": ("✅ 비상 알림 길 테스트에 성공했어요", "무슨 일: 테스트 알림이 Mattermost까지 잘 도착했어요.\n어떤 영향: 실제 서비스 장애는 없어요.\n담당자: Infra · 김세민\n지금 할 일: 제목을 눌러 Grafana 화면이 열리는지만 확인해 주세요."),
            "UnexpectedInternalName": ("⚠️ 알림 전달 기능에 문제가 생겼어요", "무슨 일: 알림 전달 기능에서 알 수 없는 문제가 생겼어요.\n어떤 영향: 운영 알림 일부를 놓칠 수 있어요.\n담당자: Infra · 김세민\n지금 할 일: Sentinel 상태를 확인해 주세요."),
        }
        resolved = ("✅ 알림 전달 기능이 다시 정상이에요", "무슨 일: 앞에서 발견한 문제가 끝났어요.\n어떤 영향: 알림을 다시 정상적으로 받을 수 있어요.\n담당자: Infra · 김세민\n지금 할 일: 추가로 할 일은 없어요.")
        for alertname, firing_expected in firing.items():
            for status, expected in (("firing", firing_expected), ("resolved", resolved)):
                severity = "critical" if alertname == "PinLogSentinelDown" else "warning"
                title = _render_fallback(slack["title"], status=status, alertname=alertname, severity=severity)
                text = _render_fallback(slack["text"], status=status, alertname=alertname, severity=severity)
                with self.subTest(alertname=alertname, status=status):
                    self.assertEqual(title, expected[0])
                    mention = "@channel\n" if status == "firing" and severity == "critical" else ""
                    self.assertEqual(text, mention + expected[1])
                    self.assertEqual(text.count("담당자: Infra · 김세민"), 1)
                    self.assertNotIn("@김세민", text)
                    for label in ("무슨 일:", "어떤 영향:", "지금 할 일:"):
                        self.assertIn(label, text)
                    for token in ("firing", "resolved", ".Annotations", alertname):
                        self.assertNotIn(token, title + text)

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
        self.assertIn("node 전체", doc)
        self.assertIn("외부 HTTPS 모니터", doc)
        for example in (
            "🚨 알림 전달 로봇이 멈췄어요",
            "⚠️ 알림을 보냈지만 도착하지 않았어요",
            "⚠️ 못 보낸 알림이 보관함에 들어갔어요",
            "⚠️ 알림 전달 로봇이 너무 바빠요",
            "✅ 비상 알림 길 테스트에 성공했어요",
            "✅ 알림 전달 기능이 다시 정상이에요",
            "무슨 일: Sentinel이 3분 동안 대답하지 않았어요.",
            "어떤 영향: 서비스에 문제가 생겨도 Mattermost 알림이 안 올 수 있어요.",
            "담당자: Infra · 김세민",
            "지금 할 일: Sentinel 상태를 확인해 주세요.",
        ):
            self.assertIn(example, doc)


if __name__ == "__main__":
    unittest.main()
