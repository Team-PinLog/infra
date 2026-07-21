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


def _dependabot_auto_merge_violations(workflow_text: str) -> list[str]:
    try:
        workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        return ["valid workflow YAML"]
    if not isinstance(workflow, dict):
        return ["valid workflow mapping"]

    violations: list[str] = []
    on_config = workflow.get("on")
    if not isinstance(on_config, dict) or set(on_config) != {"workflow_run"}:
        violations.append("single trusted workflow trigger")
    trigger = (
        on_config.get("workflow_run", {}) if isinstance(on_config, dict) else {}
    )
    if (
        not isinstance(trigger, dict)
        or set(trigger) != {"workflows", "types"}
        or trigger.get("workflows") != ["validate"]
        or trigger.get("types") != ["completed"]
    ):
        violations.append("trusted validate workflow_run trigger")

    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or set(jobs) != {"queue-auto-merge"}:
        violations.append("single trusted auto-merge job")
    job = jobs.get("queue-auto-merge", {}) if isinstance(jobs, dict) else {}
    expected_job_keys = {"if", "runs-on", "env", "steps"}
    if (
        not isinstance(job, dict)
        or set(job) != expected_job_keys
        or job.get("runs-on") != "ubuntu-latest"
    ):
        violations.append("exact auto-merge job schema")
    condition = job.get("if", "")
    expected_condition = " ".join(
        (
            "github.event.workflow_run.event == 'pull_request' &&",
            "github.event.workflow_run.conclusion == 'success' &&",
            "github.event.workflow_run.pull_requests[0].number",
        )
    )
    if " ".join(condition.split()) != expected_condition:
        violations.append("exact auto-merge job condition")
    if "github.event.workflow_run.event == 'pull_request'" not in condition:
        violations.append("pull_request event guard")
    if "github.event.workflow_run.conclusion == 'success'" not in condition:
        violations.append("successful conclusion guard")
    if "github.event.workflow_run.pull_requests[0].number" not in condition:
        violations.append("workflow_run PR number guard")

    expected_env = {
        "GH_TOKEN": "${{ github.token }}",
        "PR_NUMBER": "${{ github.event.workflow_run.pull_requests[0].number }}",
        "HEAD_SHA": "${{ github.event.workflow_run.head_sha }}",
    }
    if job.get("env") != expected_env:
        violations.append("trusted workflow_run environment")

    steps = job.get("steps", [])
    if not isinstance(steps, list):
        steps = []
    expected_step_name = "Queue verified Dependabot pull request for auto-merge"
    if (
        len(steps) != 1
        or not isinstance(steps[0], dict)
        or set(steps[0]) != {"name", "run"}
        or steps[0].get("name") != expected_step_name
    ):
        violations.append("exact trusted auto-merge step")
    run_script = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    actual_script_lines = [
        line.strip() for line in run_script.splitlines() if line.strip()
    ]
    expected_script_lines = [
        "set -euo pipefail",
        'author=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER" --jq \'.user.login\')',
        'if [ "$author" != "dependabot[bot]" ]; then',
        'echo "Skipping non-Dependabot PR #$PR_NUMBER"',
        "exit 0",
        "fi",
        'gh pr merge "$PR_NUMBER" \\',
        '--repo "$GITHUB_REPOSITORY" \\',
        '--match-head-commit "$HEAD_SHA" \\',
        "--auto --squash --delete-branch",
    ]
    if actual_script_lines != expected_script_lines:
        violations.append("exact trusted auto-merge script")
    if not any(
        line.startswith('gh pr merge "$PR_NUMBER"') for line in actual_script_lines
    ):
        violations.append("actual merge command")
    if (
        'author=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER" '
        "--jq '.user.login')"
        not in run_script
    ):
        violations.append("API author lookup")
    if 'if [ "$author" != "dependabot[bot]" ]; then' not in run_script:
        violations.append("exact Dependabot author comparison")
    if '--match-head-commit "$HEAD_SHA"' not in run_script:
        violations.append("validated head SHA binding")
    if "--auto --squash --delete-branch" not in run_script:
        violations.append("required-check auto-merge queue")

    def contains_uses(value: object) -> bool:
        if isinstance(value, dict):
            return "uses" in value or any(contains_uses(child) for child in value.values())
        if isinstance(value, list):
            return any(contains_uses(child) for child in value)
        return False

    def collect_run_scripts(value: object) -> list[str]:
        scripts: list[str] = []
        if isinstance(value, dict):
            run = value.get("run")
            if isinstance(run, str):
                scripts.append(run)
            for child in value.values():
                scripts.extend(collect_run_scripts(child))
        elif isinstance(value, list):
            for child in value:
                scripts.extend(collect_run_scripts(child))
        return scripts

    all_run_scripts = "\n".join(collect_run_scripts(workflow))
    blocked_checkout_commands = ("git checkout", "gh pr checkout", "actions/checkout")
    if contains_uses(workflow) or any(
        command in all_run_scripts for command in blocked_checkout_commands
    ):
        violations.append("checkout or PR-code execution")

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
            "validate.yaml": {"contents": "read"},
            "pr-policy.yaml": {"contents": "read"},
            "external-https-monitor.yaml": {"contents": "write"},
            "dependabot-auto-merge.yaml": {
                "contents": "write",
                "pull-requests": "write",
            },
        }
        workflow_dir = ROOT / ".github" / "workflows"
        actual = {path.name for path in workflow_dir.glob("*.y*ml")}
        self.assertEqual(set(expected), actual)

        for filename, required_permissions in expected.items():
            workflow = yaml.load(
                (workflow_dir / filename).read_text(encoding="utf-8"),
                Loader=yaml.BaseLoader,
            )
            self.assertEqual(
                required_permissions,
                workflow.get("permissions"),
                f"{filename} permissions must remain least privilege",
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
        self.assertRegex(workflow, r"actions/setup-python@[0-9a-f]{40}")
        self.assertIn('python-version: "3.12"', workflow)
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
        self.assertIn("PR_AUTHOR: ${{ github.event.pull_request.user.login }}", workflow)
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

    def test_dependabot_auto_merge_runs_only_after_trusted_ci_success(self):
        workflow = (
            ROOT / ".github" / "workflows" / "dependabot-auto-merge.yaml"
        ).read_text(encoding="utf-8")
        self.assertEqual([], _dependabot_auto_merge_violations(workflow))

    def test_dependabot_auto_merge_structural_guard_rejects_trust_bypasses(self):
        workflow = (
            ROOT / ".github" / "workflows" / "dependabot-auto-merge.yaml"
        ).read_text(encoding="utf-8")
        mutations = {
            "pull_request event guard": workflow.replace(
                "github.event.workflow_run.event == 'pull_request' &&", "true &&"
            ),
            "successful conclusion guard": workflow.replace(
                "github.event.workflow_run.conclusion == 'success' &&", "true &&"
            ),
            "API author lookup": workflow.replace(
                'author=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER" --jq \'.user.login\')',
                'author="dependabot[bot]"',
            ),
            "exact Dependabot author comparison": workflow.replace(
                'if [ "$author" != "dependabot[bot]" ]; then', "if false; then"
            ),
            "checkout or PR-code execution": workflow.replace(
                "    steps:\n",
                "    steps:\n"
                "      - uses: actions/checkout@"
                "3d3c42e5aac5ba805825da76410c181273ba90b1\n",
            ),
            "exact auto-merge job condition": workflow.replace(
                "github.event.workflow_run.pull_requests[0].number",
                "github.event.workflow_run.pull_requests[0].number || true",
                1,
            ),
            "exact trusted auto-merge script": workflow.replace(
                "          if [ \"$author\" != \"dependabot[bot]\" ]; then",
                "          author=\"dependabot[bot]\"\n"
                "          if [ \"$author\" != \"dependabot[bot]\" ]; then",
            ),
            "actual merge command": workflow.replace(
                '          gh pr merge "$PR_NUMBER" \\\n',
                '          printf \'gh pr merge %s\\n\' "$PR_NUMBER" \\\n',
            ),
            "single trusted auto-merge job": workflow.replace(
                "  queue-auto-merge:\n",
                "  untrusted:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@"
                "3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "  queue-auto-merge:\n",
            ),
            "single trusted workflow trigger": workflow.replace(
                "  workflow_run:\n",
                "  workflow_dispatch:\n  workflow_run:\n",
            ),
            "exact auto-merge job schema": workflow.replace(
                "    runs-on: ubuntu-latest\n",
                "    runs-on: ubuntu-latest\n    permissions: write-all\n",
            ),
            "exact trusted auto-merge step": workflow.replace(
                "      - name: Queue verified Dependabot pull request for auto-merge\n",
                "      - name: Queue verified Dependabot pull request for auto-merge\n"
                "        env:\n          PR_NUMBER: 1\n",
            ),
        }
        for expected_violation, mutated_workflow in mutations.items():
            with self.subTest(expected_violation=expected_violation):
                self.assertIn(
                    expected_violation,
                    _dependabot_auto_merge_violations(mutated_workflow),
                )


if __name__ == "__main__":
    unittest.main()
