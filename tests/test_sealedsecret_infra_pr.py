import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / ".github/actions/sealedsecret-infra-pr/sealedsecret_infra_pr.py"
ACTION_PATH = ROOT / ".github/actions/sealedsecret-infra-pr/action.yml"
POLICY_DIR = ROOT / "policy/sealedsecrets"


def load_module():
    spec = importlib.util.spec_from_file_location("sealedsecret_infra_pr", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CanonicalPolicyTest(unittest.TestCase):
    def test_only_approved_service_environment_policies_exist(self):
        self.assertEqual(
            ["ai-dev.yaml", "back-prod.yaml"],
            sorted(path.name for path in POLICY_DIR.glob("*.yaml")),
        )
        ai = yaml.safe_load((POLICY_DIR / "ai-dev.yaml").read_text())
        back = yaml.safe_load((POLICY_DIR / "back-prod.yaml").read_text())
        self.assertEqual(
            ["GMS_API_KEY", "GMS_BASE_URL", "INTERNAL_SHARED_SECRET"],
            ai["ownerSecretKeys"],
        )
        self.assertEqual(
            ["GOOGLE_CLIENT_SECRET", "KAKAO_CLIENT_SECRET", "NAVER_CLIENT_SECRET"],
            back["ownerSecretKeys"],
        )
        for policy in (ai, back):
            self.assertEqual("strict", policy["scope"])
            self.assertEqual("Opaque", policy["secretType"])
            self.assertEqual("0.38.4", policy["kubeseal"]["version"])
            self.assertRegex(policy["kubeseal"]["archiveSha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                [policy["targetPath"], policy["provenancePath"], policy["rolloutPath"]],
                policy["allowedChangedPaths"],
            )
            self.assertEqual(
                f"automation/secrets-{policy['service']}-{policy['environment']}",
                policy["targetBranch"],
            )

    def test_policy_rejects_cross_service_environment_name_path_key_and_scope(self):
        module = load_module()
        base = yaml.safe_load((POLICY_DIR / "ai-dev.yaml").read_text())
        mutations = {
            "service": "back",
            "environment": "prod",
            "secretName": "ai-db-credentials",
            "targetPath": "secrets/prod/ai-owner-secrets.sealedsecret.yaml",
            "ownerSecretKeys": ["DATABASE_URL"],
            "scope": "cluster-wide",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = dict(base)
                changed[field] = value
                with self.assertRaises(ValueError):
                    module.validate_policy(changed, "ai-dev")


class ArtifactContractTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.policy = self.module.load_policy(POLICY_DIR / "ai-dev.yaml", "ai-dev")
        self.provenance = {
            "sourceRepository": "Team-PinLog/ai",
            "sourceSha": "a" * 40,
            "sourceRef": "refs/heads/main",
            "sourceWorkflow": ".github/workflows/ai-ci.yml",
            "sourceRunId": "123",
            "policyDigest": self.module.policy_digest(self.policy),
            "actionSha": "b" * 40,
            "certSha256": self.policy["certificate"]["sha256"],
            "certFingerprint": self.policy["certificate"]["x509FingerprintSha256"],
            "service": "ai",
            "environment": "dev",
            "githubEnvironment": "pinlog-secrets-dev",
            "revision": "a" * 40,
        }

    def manifest(self):
        return self.module.build_manifest(
            self.policy,
            {key: "Ag" + "x" * 120 for key in self.policy["ownerSecretKeys"]},
            self.provenance,
        )

    def test_ciphertext_only_manifest_and_provenance_are_exact(self):
        manifest = self.manifest()
        self.module.validate_artifact(manifest, self.provenance, self.policy)
        self.assertEqual("SealedSecret", manifest["kind"])
        def all_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from all_keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from all_keys(child)

        self.assertTrue({"data", "stringData"}.isdisjoint(all_keys(manifest)))
        self.assertEqual(
            self.provenance["revision"],
            manifest["spec"]["template"]["metadata"]["annotations"]["secrets.pinlog.io/revision"],
        )

    def test_plaintext_secret_and_wrong_provenance_are_rejected(self):
        manifest = self.manifest()
        for mutation in (
            lambda value: value.update(kind="Secret"),
            lambda value: value.__setitem__("data", {"GMS_API_KEY": "plain"}),
            lambda value: value.__setitem__("stringData", {"GMS_API_KEY": "plain"}),
        ):
            changed = json.loads(json.dumps(manifest))
            mutation(changed)
            with self.assertRaises(ValueError):
                self.module.validate_artifact(changed, self.provenance, self.policy)
        for field in ("sourceRepository", "sourceSha", "sourceRef", "sourceWorkflow", "sourceRunId", "policyDigest", "actionSha", "certSha256", "certFingerprint", "githubEnvironment"):
            changed = dict(self.provenance)
            changed[field] = "wrong"
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.validate_artifact(manifest, changed, self.policy)


class ActionBoundaryTest(unittest.TestCase):
    def test_action_has_narrow_inputs_and_no_dispatch_or_cluster_mutation(self):
        action = yaml.load(ACTION_PATH.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual("composite", action["runs"]["using"])
        self.assertEqual({"policy", "revision"}, set(action["inputs"]))
        text = ACTION_PATH.read_text() + MODULE_PATH.read_text()
        for forbidden in ("repository_dispatch", "kubeconfig", "kubectl", "--from-literal", "set -x"):
            self.assertNotIn(forbidden, text)
        cert = ROOT / ".github/pinlog/sealed-secrets/pinlog-dev-cert.pem"
        self.assertNotIn("PRIVATE KEY", cert.read_text())
        self.assertIn("PINLOG_INFRA_SECRET_PR_TOKEN", text)
        self.assertIn("--force-with-lease=refs/heads/", text)
        self.assertIn("actions/runs", text)

    def test_safe_output_rejects_newline_injection(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            module.write_github_output(output, "branch", "safe-value")
            module.write_github_output(output, "pull_request_url", "https://github.com/Team-PinLog/infra/pull/1")
            self.assertEqual(
                "branch=safe-value\npull_request_url=https://github.com/Team-PinLog/infra/pull/1\n",
                output.read_text(),
            )
            for value in ("bad\nname", "bad\rname", "bad%0Aname"):
                with self.assertRaises(ValueError):
                    module.write_github_output(output, "branch", value)
            for name in ("pull-request-url", "bad\nname", "bad=name"):
                with self.assertRaises(ValueError):
                    module.write_github_output(output, name, "safe-value")

    def test_action_output_contract_uses_safe_snake_case_name(self):
        action = yaml.load(ACTION_PATH.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual({"branch", "pull_request_url"}, set(action["outputs"]))
        self.assertEqual(
            "${{ steps.direct-pr.outputs.pull_request_url }}",
            action["outputs"]["pull_request_url"]["value"],
        )

    def test_workflow_environment_and_immutable_action_are_runtime_bound(self):
        module = load_module()
        policy = module.load_policy(POLICY_DIR / "ai-dev.yaml", "ai-dev")
        action_sha = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "source"
            workflow = workspace / policy["sourceWorkflow"]
            workflow.parent.mkdir(parents=True)
            workflow.write_text(yaml.safe_dump({
                "jobs": {
                    "publish": {
                        "environment": {"name": policy["githubEnvironment"]},
                        "steps": [{
                            "uses": f"Team-PinLog/infra/.github/actions/sealedsecret-infra-pr@{action_sha}",
                        }],
                    },
                },
            }))
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=workspace, check=True)
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "workflow"], cwd=workspace, check=True)
            source_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, text=True,
                capture_output=True,
            ).stdout.strip()
            context = {
                "repository": policy["sourceRepository"],
                "sha": source_sha,
                "workflow": policy["sourceWorkflow"],
                "action_sha": action_sha,
            }
            module.verify_workflow_binding(workspace, "publish", context, policy)

    def test_workflow_binding_rejects_environment_and_provenance_bypasses(self):
        module = load_module()
        policy = module.load_policy(POLICY_DIR / "ai-dev.yaml", "ai-dev")
        action_sha = "b" * 40

        def rejected(workflow_data, *, context_updates=None, symlink=False):
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "source"
                workflow = workspace / policy["sourceWorkflow"]
                workflow.parent.mkdir(parents=True)
                target = Path(directory) / "workflow.yml" if symlink else workflow
                target.write_text(yaml.safe_dump(workflow_data))
                if symlink:
                    workflow.symlink_to(target)
                subprocess.run(["git", "init", "-q", str(workspace)], check=True)
                subprocess.run(["git", "config", "user.name", "test"], cwd=workspace, check=True)
                subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=workspace, check=True)
                subprocess.run(["git", "add", "."], cwd=workspace, check=True)
                subprocess.run(["git", "commit", "-qm", "workflow"], cwd=workspace, check=True)
                source_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, text=True,
                    capture_output=True,
                ).stdout.strip()
                context = {
                    "repository": policy["sourceRepository"],
                    "sha": source_sha,
                    "workflow": policy["sourceWorkflow"],
                    "action_sha": action_sha,
                }
                context.update(context_updates or {})
                with self.assertRaises(ValueError):
                    module.verify_workflow_binding(workspace, "publish", context, policy)

        immutable_step = {
            "uses": f"Team-PinLog/infra/.github/actions/sealedsecret-infra-pr@{action_sha}",
        }
        for environment in (None, "wrong", "${{ inputs.environment }}"):
            job = {"steps": [immutable_step]}
            if environment is not None:
                job["environment"] = environment
            rejected({"jobs": {"publish": job}})
        rejected({"jobs": {"publish": {
            "environment": policy["githubEnvironment"],
            "steps": [{"uses": "Team-PinLog/infra/.github/actions/sealedsecret-infra-pr@main"}],
        }}})
        rejected({"jobs": {"publish": {
            "environment": policy["githubEnvironment"], "steps": [immutable_step],
        }}}, context_updates={"repository": "attacker/repo"})
        rejected({"jobs": {"publish": {
            "environment": policy["githubEnvironment"], "steps": [immutable_step],
        }}}, context_updates={"workflow": "../workflow.yml"})
        rejected({"jobs": {"publish": {
            "environment": policy["githubEnvironment"], "steps": [immutable_step],
        }}}, symlink=True)

    def test_rollout_path_receives_exact_pod_annotation_idempotently(self):
        module = load_module()
        revision = "c" * 40
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text("replicaCount: 1\n")
            module.patch_rollout(values, revision)
            first = yaml.safe_load(values.read_text())
            self.assertEqual(revision, first["podAnnotations"]["secrets.pinlog.io/revision"])
            original = values.read_text()
            module.patch_rollout(values, revision)
            self.assertEqual(original, values.read_text())

    def test_secret_values_are_stdin_only_and_services_use_isolated_branches(self):
        module = load_module()
        argv, stdin = module.kubeseal_invocation(
            "/tmp/kubeseal", "/tmp/cert.pem", "pinlog-dev", "ai-owner-secrets", "synthetic-value"
        )
        self.assertNotIn("synthetic-value", argv)
        self.assertEqual("synthetic-value", stdin)
        ai = module.load_policy(POLICY_DIR / "ai-dev.yaml", "ai-dev")
        back = module.load_policy(POLICY_DIR / "back-prod.yaml", "back-prod")
        self.assertNotEqual(ai["targetBranch"], back["targetBranch"])

    def test_fixed_remote_branch_refresh_fetches_then_force_leases_twice(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, seed, checkout = root / "origin.git", root / "seed", root / "work"

            def git(*args, cwd=None):
                return subprocess.run(
                    ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
                ).stdout.strip()

            git("init", "--bare", str(origin))
            git("init", str(seed))
            git("config", "user.name", "test", cwd=seed)
            git("config", "user.email", "test@example.invalid", cwd=seed)
            (seed / "base").write_text("base\n")
            git("add", "base", cwd=seed)
            git("commit", "-m", "base", cwd=seed)
            git("branch", "-M", "main", cwd=seed)
            git("remote", "add", "origin", str(origin), cwd=seed)
            git("push", "origin", "main", cwd=seed)
            git("clone", str(origin), str(checkout))
            git("switch", "main", cwd=checkout)
            git("config", "user.name", "test", cwd=checkout)
            git("config", "user.email", "test@example.invalid", cwd=checkout)
            branch = "automation/secrets-ai-dev"
            previous = ""
            remote_shas = []
            for iteration in ("first", "second"):
                previous = module.fetch_remote_branch(checkout, branch, dict(os.environ))
                git("switch", "-C", branch, "origin/main", cwd=checkout)
                (checkout / "refresh").write_text(iteration + "\n")
                git("add", "refresh", cwd=checkout)
                git("commit", "-m", iteration, cwd=checkout)
                module.push_with_lease(checkout, branch, previous, dict(os.environ))
                remote_shas.append(git("rev-parse", f"refs/remotes/origin/{branch}", cwd=checkout))
            self.assertNotEqual(remote_shas[0], remote_shas[1])

    def test_run_api_readback_contract_is_fail_closed(self):
        module = load_module()
        expected = {
            "repository": "Team-PinLog/ai",
            "sha": "a" * 40,
            "ref": "refs/heads/main",
            "workflow": ".github/workflows/ai-ci.yml",
            "run_id": "123",
        }
        valid = {
            "head_sha": expected["sha"],
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": expected["workflow"],
            "repository": {"full_name": expected["repository"]},
            "status": "in_progress",
        }
        module.validate_run_readback(valid, expected)
        for field in ("head_sha", "head_branch", "path", "event"):
            changed = json.loads(json.dumps(valid))
            changed[field] = "wrong"
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.validate_run_readback(changed, expected)

    def test_oidc_claims_bind_the_actual_github_environment(self):
        module = load_module()
        policy = module.load_policy(POLICY_DIR / "ai-dev.yaml", "ai-dev")
        context = {
            "repository": "Team-PinLog/ai", "sha": "a" * 40,
            "ref": "refs/heads/main", "workflow": ".github/workflows/ai-ci.yml",
            "run_id": "123", "action_sha": "b" * 40,
        }
        claims = {
            "aud": "pinlog-sealedsecret-infra-pr",
            "sub": "repo:Team-PinLog/ai:environment:pinlog-secrets-dev",
            "repository": "Team-PinLog/ai", "sha": "a" * 40,
            "ref": "refs/heads/main",
            "workflow_ref": "Team-PinLog/ai/.github/workflows/ai-ci.yml@refs/heads/main",
            "run_id": "123",
        }
        module.validate_oidc_claims(claims, policy, context)
        for field in claims:
            changed = dict(claims)
            changed[field] = "wrong"
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.validate_oidc_claims(changed, policy, context)


if __name__ == "__main__":
    unittest.main()
