import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from receiver import (
    AlertProcessor,
    DeliveryStore,
    HermesRunner,
    PayloadError,
    build_dedupe_key,
    derive_status_and_severity,
    enforce_message_policy,
    extract_hermes_message,
    is_client_allowed,
    parse_allowed_cidrs,
    validate_payload,
)


class PayloadValidationTests(unittest.TestCase):
    def test_validate_payload_accepts_alertmanager_payload(self):
        payload = {
            "version": "4",
            "status": "firing",
            "groupKey": "{}:{alertname=\"NodeDown\"}",
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": "abc123",
                    "labels": {"severity": "critical"},
                }
            ],
        }

        self.assertIs(validate_payload(payload, raw_size=256), payload)

    def test_validate_payload_rejects_empty_alerts(self):
        with self.assertRaisesRegex(PayloadError, "alerts"):
            validate_payload({"version": "4", "alerts": []}, raw_size=32)

    def test_validate_payload_rejects_oversized_body(self):
        with self.assertRaisesRegex(PayloadError, "large"):
            validate_payload({"alerts": [{}]}, raw_size=262_145)


class MessagePolicyTests(unittest.TestCase):
    def test_critical_firing_gets_marker_mention_and_final_summary(self):
        message = (
            ":red_circle: **[CRITICAL][PROD][MONITORING] 노드 응답 없음**\n\n"
            "- **상태:** FIRING\n\n"
            "---\n**한 줄 요약:** 노드 장애가 진행 중이므로 즉시 확인하세요.\n"
        )

        result = enforce_message_policy(message, status="firing", severity="critical")

        self.assertTrue(result.startswith("🤖 **[자동 알림 · SENTINEL]**\n"))
        self.assertIn("@channel", result)
        self.assertTrue(result.endswith("**한 줄 요약:** 노드 장애가 진행 중이므로 즉시 확인하세요."))

    def test_warning_removes_broad_mention(self):
        message = (
            "🤖 **[자동 알림 · SENTINEL]**\n"
            "@channel @here @all @everyone @attacker :warning: **[WARNING][PROD][MONITORING] 메모리 압력**\n"
            "[악성 링크](https://evil.example/x) https://evil.example/raw\n"
            "---\n**한 줄 요약:** @victim 메모리 사용량을 확인하세요."
        )

        result = enforce_message_policy(message, status="firing", severity="warning")

        self.assertNotIn("@channel", result)
        self.assertNotIn("@here", result)
        self.assertNotIn("@all", result)
        self.assertNotIn("@everyone", result)
        self.assertNotIn("@attacker", result)
        self.assertNotIn("@victim", result)
        self.assertNotIn("https://", result)
        self.assertIn("악성 링크", result)

    def test_resolved_removes_broad_mention(self):
        message = (
            "@channel\n:large_green_circle: **[RESOLVED][PROD][MONITORING] 복구**\n"
            "---\n**한 줄 요약:** 서비스가 복구되어 추가 조치는 없습니다."
        )

        result = enforce_message_policy(message, status="resolved", severity="critical")

        self.assertNotIn("@channel", result)

    def test_missing_final_summary_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "summary"):
            enforce_message_policy("경고 본문", status="firing", severity="warning")


class NetworkBoundaryTests(unittest.TestCase):
    def test_allowed_cidrs_accept_loopback_and_pods_only(self):
        networks = parse_allowed_cidrs("127.0.0.0/8,10.42.0.0/16")
        self.assertTrue(is_client_allowed("127.0.0.1", networks))
        self.assertTrue(is_client_allowed("10.42.3.7", networks))
        self.assertFalse(is_client_allowed("172.26.14.10", networks))
        self.assertFalse(is_client_allowed("203.0.113.9", networks))

    def test_invalid_cidr_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_allowed_cidrs("not-a-cidr")


class DedupeKeyTests(unittest.TestCase):
    def test_key_is_stable_when_alert_order_changes(self):
        first = {
            "status": "firing",
            "groupKey": "group-a",
            "alerts": [{"fingerprint": "b"}, {"fingerprint": "a"}],
        }
        second = {
            "status": "firing",
            "groupKey": "group-a",
            "alerts": [{"fingerprint": "a"}, {"fingerprint": "b"}],
        }

        self.assertEqual(build_dedupe_key(first), build_dedupe_key(second))


class DeliveryStoreTests(unittest.TestCase):
    def test_dead_letters_are_capped(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(Path(directory) / "state.db", max_dead_letters=3)
            for number in range(10):
                store.record_failure(
                    f"key-{number}", f"hash-{number}", float(number), "hermes", "Error"
                )
            self.assertEqual(store.failure_count(), 3)

    def test_duplicate_is_suppressed_until_cooldown_expires(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(Path(directory) / "state.db", cooldown_seconds=300)

            self.assertTrue(store.should_process("key-1", "hash-1", now=1_000))
            store.record_success("key-1", "hash-1", now=1_010)
            self.assertFalse(store.should_process("key-1", "hash-1", now=1_100))
            self.assertTrue(store.should_process("key-1", "hash-1", now=1_311))

    def test_changed_content_is_processed_during_cooldown(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(Path(directory) / "state.db", cooldown_seconds=300)
            store.record_success("key-1", "hash-1", now=1_000)

            self.assertTrue(store.should_process("key-1", "hash-2", now=1_100))


class DerivationTests(unittest.TestCase):
    def test_critical_wins_over_warning(self):
        payload = {
            "status": "firing",
            "alerts": [
                {"labels": {"severity": "warning"}},
                {"labels": {"severity": "critical"}},
            ],
        }

        self.assertEqual(derive_status_and_severity(payload), ("firing", "critical"))

    def test_resolved_status_is_preserved(self):
        payload = {"status": "resolved", "alerts": [{"labels": {"severity": "critical"}}]}

        self.assertEqual(derive_status_and_severity(payload), ("resolved", "critical"))

    def test_resolved_critical_does_not_escalate_firing_warning(self):
        payload = {
            "status": "firing",
            "alerts": [
                {"status": "resolved", "labels": {"severity": "critical"}},
                {"status": "firing", "labels": {"severity": "warning"}},
            ],
        }
        self.assertEqual(derive_status_and_severity(payload), ("firing", "warning"))

    def test_missing_severity_is_unknown(self):
        payload = {"status": "firing", "alerts": [{"status": "firing", "labels": {}}]}
        self.assertEqual(derive_status_and_severity(payload), ("firing", "unknown"))

    def test_cli_session_metadata_is_removed(self):
        output = "session_id: 20260721_example\n\n🤖 **[자동 알림 · SENTINEL]**\n---\n**한 줄 요약:** 정상입니다.\n"

        self.assertTrue(extract_hermes_message(output).startswith("🤖"))
        self.assertNotIn("session_id", extract_hermes_message(output))


class AlertProcessorTests(unittest.TestCase):
    def test_unknown_severity_is_rejected_before_model(self):
        import tempfile

        payload = {
            "status": "firing",
            "alerts": [{"status": "firing", "labels": {"alertname": "Unknown"}}],
        }
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            processor = AlertProcessor(
                DeliveryStore(Path(directory) / "state.db"),
                lambda prompt: calls.append(prompt),
                lambda message: calls.append(message),
            )
            with self.assertRaises(PayloadError):
                processor.process(payload, now=100.0)
        self.assertEqual(calls, [])

    def test_payload_is_delivered_once(self):
        import tempfile

        payload = {
            "status": "firing",
            "groupKey": "group-a",
            "alerts": [
                {
                    "fingerprint": "fp-a",
                    "labels": {"severity": "critical", "alertname": "NodeDown"},
                }
            ],
        }
        sent = []
        model_message = (
            ":red_circle: **[CRITICAL][PROD][MONITORING] 노드 장애**\n"
            "---\n**한 줄 요약:** 노드를 즉시 확인하세요."
        )
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(Path(directory) / "state.db", cooldown_seconds=300)
            processor = AlertProcessor(
                store=store,
                runner=lambda prompt: model_message,
                sender=sent.append,
            )

            self.assertEqual(processor.process(payload, now=1_000), "delivered")
            self.assertEqual(processor.process(payload, now=1_100), "duplicate")

        self.assertEqual(len(sent), 1)
        self.assertIn("@channel", sent[0])
        self.assertTrue(sent[0].startswith("🤖"))


class MattermostSenderTests(unittest.TestCase):
    def test_webhook_is_loaded_from_credential_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "mattermost_url"
            placeholder_url = "https://mattermost.example" + "/hooks/" + "placeholder"
            credential.write_text(placeholder_url + "\n")
            sender = __import__("receiver").MattermostSender(credential)
            self.assertEqual(sender._load_url(), placeholder_url)


class HermesRunnerTests(unittest.TestCase):
    def test_runner_sends_prompt_on_stdin_with_bounded_timeout(self):
        import subprocess
        from unittest.mock import patch

        prompt = "sensitive alert payload"
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="formatted", stderr="")
        runner = HermesRunner(timeout_seconds=180)

        with patch("receiver.subprocess.run", return_value=completed) as run:
            self.assertEqual(runner(prompt), "formatted")

        args, kwargs = run.call_args
        self.assertNotIn(prompt, args[0])
        self.assertEqual(kwargs["input"], prompt)
        self.assertEqual(kwargs["timeout"], 180)
        self.assertTrue(kwargs["check"])

    def test_runner_timeout_is_bounded(self):
        import subprocess
        from unittest.mock import patch

        runner = HermesRunner(timeout_seconds=1)
        with patch(
            "receiver.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["worker"], timeout=1),
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                runner("payload")


if __name__ == "__main__":
    unittest.main()
