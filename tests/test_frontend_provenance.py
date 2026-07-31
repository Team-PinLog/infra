import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "validate_frontend_provenance.py"
SHA = "a" * 40
FP = "b" * 20
DIGEST = "sha256:" + "c" * 64
TAG = f"{SHA}-cfg-{FP}-run-123-a2"


def valid_provenance():
    return {
        "schema_version": 1,
        "source_repository": "Team-PinLog/front",
        "source_sha": SHA,
        "source_ref": "dev",
        "image_repository": "ghcr.io/team-pinlog/front",
        "image_tag": TAG,
        "image_digest": DIGEST,
        "config_fingerprint": FP,
        "workflow_run_id": 123,
        "workflow_run_attempt": 2,
    }


class ProvenanceContract(unittest.TestCase):
    def validate(self, data, run_id="123", run_attempt="2", source_sha=SHA, registry_digest=DIGEST):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "provenance.json"
            path.write_text(json.dumps(data))
            return subprocess.run([sys.executable, str(TOOL), "validate", str(path), "--run-id", run_id,
                "--run-attempt", run_attempt, "--source-sha", source_sha, "--registry-digest", registry_digest], capture_output=True, text=True)

    def test_accepts_exact_run_bound_composite_provenance(self):
        result = self.validate(valid_provenance())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [TAG, DIGEST, SHA])

    def test_rejects_non_exact_integer_schema_versions(self):
        for schema_version in (True, False, 1.0, "1"):
            value = valid_provenance()
            value["schema_version"] = schema_version
            with self.subTest(schema_version=schema_version):
                self.assertNotEqual(self.validate(value).returncode, 0)

    def test_rejects_boolean_workflow_run_numbers(self):
        for key in ("workflow_run_id", "workflow_run_attempt"):
            value = valid_provenance()
            value[key] = True
            with self.subTest(key=key):
                self.assertNotEqual(self.validate(value).returncode, 0)

    def test_rejects_malformed_untrusted_or_mismatched_provenance(self):
        mutations = []
        extra = valid_provenance(); extra["caller_tag"] = TAG; mutations.append(extra)
        wrong_repo = valid_provenance(); wrong_repo["source_repository"] = "evil/front"; mutations.append(wrong_repo)
        wrong_ref = valid_provenance(); wrong_ref["source_ref"] = "main"; mutations.append(wrong_ref)
        wrong_run = valid_provenance(); wrong_run["workflow_run_id"] = 124; mutations.append(wrong_run)
        wrong_tag = valid_provenance(); wrong_tag["image_tag"] = f"{SHA}-cfg-{'d'*20}"; mutations.append(wrong_tag)
        bad_type = valid_provenance(); bad_type["workflow_run_id"] = "123"; mutations.append(bad_type)
        wrong_attempt = valid_provenance(); wrong_attempt["workflow_run_attempt"] = 3; mutations.append(wrong_attempt)
        for data in mutations:
            with self.subTest(data=data): self.assertNotEqual(self.validate(data).returncode, 0)
        self.assertNotEqual(self.validate(valid_provenance(), source_sha="d"*40).returncode, 0)
        self.assertNotEqual(self.validate(valid_provenance(), registry_digest="sha256:"+"d"*64).returncode, 0)

    def test_selects_latest_trusted_success_for_head_from_push_or_manual_only(self):
        runs = {"workflow_runs": [
            {"id": 9, "head_sha": SHA, "head_branch": "dev", "event": "pull_request", "status": "completed", "conclusion": "success", "path": ".github/workflows/ci.yml", "repository": {"full_name": "Team-PinLog/front"}},
            {"id": 10, "head_sha": SHA, "head_branch": "dev", "event": "push", "status": "completed", "conclusion": "success", "path": ".github/workflows/ci.yml", "repository": {"full_name": "Team-PinLog/front"}},
            {"id": 11, "head_sha": SHA, "head_branch": "dev", "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "path": ".github/workflows/ci.yml", "repository": {"full_name": "Team-PinLog/front"}},
        ]}
        result = subprocess.run([sys.executable, str(TOOL), "select-run", "--source-sha", SHA], input=json.dumps(runs), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "11")

    def test_selection_rejects_stale_wrong_event_and_untrusted_workflow(self):
        base = {"id": 1, "head_sha": SHA, "head_branch": "dev", "event": "push", "status": "completed", "conclusion": "success", "path": ".github/workflows/ci.yml", "repository": {"full_name": "Team-PinLog/front"}}
        for change in ({"head_sha": "d"*40}, {"event": "pull_request"}, {"path": ".github/workflows/evil.yml"}, {"conclusion": "failure"}):
            run = dict(base); run.update(change)
            result = subprocess.run([sys.executable, str(TOOL), "select-run", "--source-sha", SHA], input=json.dumps({"workflow_runs": [run]}), capture_output=True, text=True)
            with self.subTest(change=change): self.assertNotEqual(result.returncode, 0)

if __name__ == "__main__": unittest.main()
