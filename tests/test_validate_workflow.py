from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yaml"


class ValidateWorkflowTopologyTest(unittest.TestCase):
    def test_validation_is_split_into_stable_required_checks_and_parallel_nodes(self):
        workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        jobs = workflow["jobs"]
        self.assertEqual(
            set(jobs),
            {
                "guardrails",
                "helm-lint",
                "monitoring-render",
                "service-render",
                "platform-schema",
                "helm",
                "ci-gate",
            },
        )

        self.assertIn("python -m unittest discover -s tests -v", str(jobs["guardrails"]))
        self.assertEqual(
            set(jobs["helm"]["needs"]),
            {"helm-lint", "monitoring-render", "service-render", "platform-schema"},
        )
        self.assertIn("always()", jobs["helm"]["if"])
        self.assertEqual(set(jobs["ci-gate"]["needs"]), {"guardrails", "helm"})
        self.assertIn("always()", jobs["ci-gate"]["if"])

    def test_tdd_and_render_nodes_run_on_github_hosted_runners_with_timeouts(self):
        workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for name, job in workflow["jobs"].items():
            with self.subTest(job=name):
                self.assertEqual(job["runs-on"], "ubuntu-latest")
                self.assertIn("timeout-minutes", job)
        self.assertNotIn("self-hosted", WORKFLOW.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
