import os
from pathlib import Path
import subprocess
import sys
import unittest

from tools.validate_pr_body import validate_pr_body


ROOT = Path(__file__).resolve().parents[1]


class PullRequestPolicyTest(unittest.TestCase):
    def test_rejects_pull_request_without_jira_key(self):
        errors = validate_pr_body(
            "## TDD Evidence\nRED: failing test\nGREEN: passing test\nRegression: full suite"
        )
        self.assertIn("Jira key is required", errors)

    def test_rejects_pull_request_without_tdd_evidence(self):
        errors = validate_pr_body("Jira: S15P11A705-10")
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_unchanged_template_comments_as_evidence(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: <!-- 먼저 실패한 테스트 -->\n"
            "GREEN: <!-- 통과한 테스트 -->\n"
            "Regression: <!-- 전체 회귀 검증 -->"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_single_character_evidence(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\nRED: x\nGREEN: y\nRegression: z"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_evidence_labels_inside_fenced_code(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n```text\n"
            "RED: a realistic failing test\n"
            "GREEN: a realistic passing test\n"
            "Regression: the full suite passed\n```"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_evidence_labels_inside_unclosed_fence(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n```text\n"
            "RED: expected failure reproduced\n"
            "GREEN: implementation passed\n"
            "Regression: full suite passed"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_long_but_meaningless_evidence(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: abcdefghijklmnop\n"
            "GREEN: qrstuvwxyzabcd\n"
            "Regression: efghijklmnop"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_repeated_keywords_without_commands_or_exit_codes(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: failurefailure\n"
            "GREEN: successsuccess\n"
            "Regression: passpass"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_negated_claims_without_execution_results(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: no failure exists\n"
            "GREEN: no test passed\n"
            "Regression: suite not run"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_non_test_commands_with_fabricated_exit_codes(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: `echo failure` 실패 확인, exit 1\n"
            "GREEN: `echo success` 통과 확인, exit 0\n"
            "Regression: `echo suite passed` 전체 통과, exit 0"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_tool_commands_that_do_not_run_validation(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: `helm repo add example https://example.invalid` 실패 확인, exit 1\n"
            "GREEN: `pnpm install` 통과 확인, exit 0\n"
            "Regression: `yarn add example` 전체 통과, exit 0"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_informational_and_fake_test_subcommands(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: `pytest --collect-only` 실패 확인, exit 1\n"
            "GREEN: `npm test-fake` 통과 확인, exit 0\n"
            "Regression: `actionlint --version` 전체 통과, exit 0"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_rejects_structured_but_negated_execution_claims(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: `python3 -m unittest tests.test_guardrail` no failure, exit 1\n"
            "GREEN: `python3 -m unittest tests.test_guardrail` no test passed, exit 0\n"
            "Regression: `python3 -m unittest discover -s tests` suite not run, exit 0"
        )
        self.assertIn("RED evidence is required", errors)
        self.assertIn("GREEN evidence is required", errors)
        self.assertIn("Regression evidence is required", errors)

    def test_accepts_meaningful_jira_and_tdd_evidence(self):
        errors = validate_pr_body(
            "Jira: S15P11A705-10\n"
            "RED: `python3 -m unittest tests.test_guardrail` 실패 확인, exit 1\n"
            "GREEN: `python3 -m unittest tests.test_guardrail` 통과, exit 0\n"
            "Regression: `python3 -m unittest discover -s tests` 전체 통과, exit 0"
        )
        self.assertEqual([], errors)

    def test_dependabot_pr_is_exempt_from_human_tdd_template(self):
        self.assertEqual([], validate_pr_body("", author="dependabot[bot]"))
        self.assertIn("Jira key is required", validate_pr_body("", author="developer"))

    def test_cli_exits_nonzero_for_invalid_pull_request_body(self):
        env = os.environ.copy()
        env["PR_BODY"] = "Jira: S15P11A705-10"
        result = subprocess.run(
            [sys.executable, "tools/validate_pr_body.py"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("RED evidence is required", result.stdout)


if __name__ == "__main__":
    unittest.main()
