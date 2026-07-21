from pathlib import Path
import unittest

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


ROOT = Path(__file__).resolve().parents[1]


def _find_unpinned_uses(workflow_text: str) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    root = yaml.compose(workflow_text, Loader=yaml.SafeLoader)

    def walk(node: yaml.Node | None) -> None:
        if isinstance(node, MappingNode):
            for key_node, value_node in node.value:
                if isinstance(key_node, ScalarNode) and key_node.value == "uses":
                    reference = (
                        value_node.value if isinstance(value_node, ScalarNode) else ""
                    )
                    normalized = f"uses: {reference or '<missing>'}"
                    if not reference.startswith("./"):
                        owner, separator, revision = reference.rpartition("@")
                        is_full_sha = (
                            bool(owner)
                            and bool(separator)
                            and len(revision) == 40
                            and all(char in "0123456789abcdefABCDEF" for char in revision)
                        )
                        if not is_full_sha:
                            violations.append(
                                (key_node.start_mark.line + 1, normalized)
                            )
                walk(value_node)
        elif isinstance(node, SequenceNode):
            for child in node.value:
                walk(child)

    walk(root)
    return violations


class RepositoryGuardrailsTest(unittest.TestCase):
    def test_action_pin_scanner_covers_job_level_reusable_workflows(self):
        violations = _find_unpinned_uses(
            "jobs:\n"
            "  reusable:\n"
            "    uses: owner/repository/.github/workflows/ci.yml@v1\n"
            "  inline: { uses: owner/repository/.github/workflows/ci.yml@v2 }\n"
            "  escaped: { \"us\\u0065s\": actions/checkout@v3 }\n"
            "steps:\n"
            "  - { uses: actions/checkout@v4 }\n"
            "  - uses : actions/setup-python@v5\n"
        )
        self.assertEqual(
            [
                (3, "uses: owner/repository/.github/workflows/ci.yml@v1"),
                (4, "uses: owner/repository/.github/workflows/ci.yml@v2"),
                (5, "uses: actions/checkout@v3"),
                (7, "uses: actions/checkout@v4"),
                (8, "uses: actions/setup-python@v5"),
            ],
            violations,
        )

    def test_codeowners_assigns_default_infrastructure_owner(self):
        codeowners = ROOT / ".github" / "CODEOWNERS"
        self.assertTrue(codeowners.is_file(), ".github/CODEOWNERS must exist")
        content = codeowners.read_text(encoding="utf-8")
        self.assertRegex(content, r"(?m)^\*\s+@tpals0409(?:\s|$)")

    def test_external_actions_are_pinned_to_full_commit_sha(self):
        workflow_dir = ROOT / ".github" / "workflows"
        violations = []
        for workflow in sorted(workflow_dir.glob("*.y*ml")):
            content = workflow.read_text(encoding="utf-8")
            for line_number, reference in _find_unpinned_uses(content):
                violations.append(f"{workflow.name}:{line_number}:{reference}")
        self.assertEqual([], violations, "unpinned actions: " + ", ".join(violations))

    def test_workflows_declare_least_privilege_permissions(self):
        expected = {
            "validate.yaml": "contents: read",
            "pr-policy.yaml": "contents: read",
            "external-https-monitor.yaml": "contents: write",
        }
        for filename, required_permission in expected.items():
            content = (ROOT / ".github" / "workflows" / filename).read_text(
                encoding="utf-8"
            )
            self.assertIn("\npermissions:\n", content, f"{filename} lacks permissions")
            self.assertIn(
                f"  {required_permission}\n",
                content,
                f"{filename} must declare {required_permission}",
            )

    def test_external_monitor_writes_state_only_to_dedicated_branch(self):
        workflow = (
            ROOT / ".github" / "workflows" / "external-https-monitor.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn('STATE_BRANCH: "monitor-state"', workflow)
        self.assertIn("path: .monitor-state", workflow)
        self.assertIn('git -C "$STATE_DIR" push origin HEAD:"$STATE_BRANCH"', workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+git push\s*$")

    def test_external_monitor_state_path_is_not_user_controlled(self):
        workflow = (
            ROOT / ".github" / "workflows" / "external-https-monitor.yaml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("github.event.inputs.state_file", workflow)
        self.assertIn(
            'STATE_RELATIVE_FILE: ".github/monitoring/external_https_state.json"',
            workflow,
        )

    def test_dependabot_updates_github_actions_weekly(self):
        config = ROOT / ".github" / "dependabot.yml"
        self.assertTrue(config.is_file(), ".github/dependabot.yml must exist")
        content = config.read_text(encoding="utf-8")
        self.assertIn('package-ecosystem: "github-actions"', content)
        self.assertIn('interval: "weekly"', content)

    def test_validate_workflow_runs_repository_guardrails(self):
        workflow = (ROOT / ".github" / "workflows" / "validate.yaml").read_text(
            encoding="utf-8"
        )
        self.assertRegex(workflow, r"(?m)^  guardrails:\s*$")
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065", workflow)
        self.assertIn("pip install --require-hashes", workflow)
        requirements = (ROOT / ".github" / "requirements-guardrails.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("PyYAML==6.0.3", requirements)
        self.assertIn("--hash=sha256:", requirements)

    def test_downloaded_ci_tools_are_checksum_verified(self):
        workflow = (ROOT / ".github" / "workflows" / "validate.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("| tar -xz", workflow)
        self.assertIn("sha256sum -c", workflow)

    def test_pull_request_template_requests_jira_and_tdd_evidence(self):
        template = ROOT / ".github" / "pull_request_template.md"
        self.assertTrue(template.is_file(), "pull request template must exist")
        content = template.read_text(encoding="utf-8")
        for marker in ("Jira:", "RED:", "GREEN:", "Regression:"):
            self.assertIn(marker, content)

    def test_trusted_workflow_enforces_pull_request_policy(self):
        workflow = (ROOT / ".github" / "workflows" / "pr-policy.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("pull_request_target:", workflow)
        self.assertRegex(workflow, r"(?m)^  pr-policy:\s*$")
        self.assertIn("PR_BODY: ${{ github.event.pull_request.body }}", workflow)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", workflow)
        self.assertIn("python3 tools/validate_pr_body.py", workflow)

        validate_workflow = (
            ROOT / ".github" / "workflows" / "validate.yaml"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(validate_workflow, r"(?m)^  pr-policy:\s*$")

    def test_pull_request_edits_rerun_policy_checks(self):
        workflow = (ROOT / ".github" / "workflows" / "pr-policy.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "pull_request_target:\n    types: [opened, synchronize, reopened, edited]",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
