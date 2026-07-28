from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "Backend": ROOT / ".github" / "workflows" / "backend-image-update.yaml",
    "Frontend": ROOT / ".github" / "workflows" / "frontend-image-update.yaml",
    "AI": ROOT / ".github" / "workflows" / "ai-image-update.yaml",
}
EXPECTED_JOBS = ["detect-source", "verify-source-ci", "verify-image", "create-infra-pr"]


class ServiceImageUpdateTopologyTest(unittest.TestCase):
    def load(self, path: Path):
        return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_each_service_exposes_the_same_four_stage_promotion_dag(self):
        for service, path in WORKFLOWS.items():
            with self.subTest(service=service):
                workflow = self.load(path)
                jobs = workflow["jobs"]
                self.assertEqual(list(jobs), EXPECTED_JOBS)
                self.assertNotIn("update", jobs)
                self.assertEqual(jobs["detect-source"]["name"], f"{service} · 1 Detect source")
                self.assertEqual(
                    jobs["verify-source-ci"]["name"],
                    f"{service} · 2 Verify source CI",
                )
                self.assertEqual(
                    jobs["verify-image"]["name"],
                    f"{service} · 3 Verify immutable image",
                )
                self.assertEqual(
                    jobs["create-infra-pr"]["name"],
                    f"{service} · 4 Create Infra PR",
                )
                self.assertNotIn("needs", jobs["detect-source"])
                self.assertEqual(jobs["verify-source-ci"]["needs"], "detect-source")
                self.assertIn("verify-source-ci", jobs["verify-image"]["needs"])
                self.assertIn(
                    "needs.verify-source-ci.outputs.candidate == 'true'",
                    jobs["verify-image"]["if"],
                )
                self.assertIn("verify-image", jobs["create-infra-pr"]["needs"])
                create_condition = jobs["create-infra-pr"]["if"]
                self.assertIn("always()", create_condition)
                self.assertIn("needs.detect-source.result != 'skipped'", create_condition)
                self.assertIn("source_sha", jobs["detect-source"]["outputs"])
                self.assertIn("candidate", jobs["verify-source-ci"]["outputs"])
                self.assertIn("run_id", jobs["verify-source-ci"]["outputs"])
                self.assertIn("digest", jobs["verify-image"]["outputs"])

    def test_nodes_do_real_work_and_only_the_last_node_can_mutate_infra(self):
        for service, path in WORKFLOWS.items():
            with self.subTest(service=service):
                jobs = self.load(path)["jobs"]
                detect = yaml.safe_dump(jobs["detect-source"])
                source_ci = yaml.safe_dump(jobs["verify-source-ci"])
                image = yaml.safe_dump(jobs["verify-image"])
                infra_pr = yaml.safe_dump(jobs["create-infra-pr"])
                self.assertIn("git/ref/heads", detect)
                self.assertIn("actions/workflows", source_ci)
                self.assertIn("imagetools inspect", image)
                self.assertNotIn("create-pull-request", detect + source_ci + image)
                self.assertIn("peter-evans/create-pull-request", infra_pr)
                self.assertIn("python -m unittest discover -s tests -v", infra_pr)
                self.assertIn("needs.verify-image.result != 'success'", infra_pr)
                pr_list_steps = [
                    step
                    for step in jobs["create-infra-pr"]["steps"]
                    if "gh pr list" in step.get("run", "")
                ]
                self.assertTrue(pr_list_steps)
                for step in pr_list_steps:
                    self.assertIn("--base main --head", step["run"])
                failure_cleanup = next(
                    step
                    for step in jobs["create-infra-pr"]["steps"]
                    if step.get("name")
                    == "Remove a stale updater PR after a failed update attempt"
                )
                self.assertIn(
                    "steps.create-pr.outcome != 'success'",
                    failure_cleanup["if"],
                )


if __name__ == "__main__":
    unittest.main()
