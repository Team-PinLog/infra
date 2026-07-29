#!/usr/bin/env python3
"""Fail-closed SealedSecret generation and direct Infra Draft PR update."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
POLICY_DIR = ROOT / "policy" / "sealedsecrets"
SHA = re.compile(r"^[0-9a-f]{40}$")
CIPHERTEXT = re.compile(r"^Ag[A-Za-z0-9+/=]{100,}$")
FINGERPRINT = "4A:C5:18:6C:57:94:76:F6:AA:E5:1C:C4:53:9D:4F:11:BB:C1:8F:3E:DF:8B:81:B8:1F:5E:A9:68:1C:5B:C6:A2"
CERT_SHA = "603347e4b3a9ece6d7a6a4245ad4b9d1d899610134f71a6f3cf5db6b78e78d06"
KUBESEAL_SHA = "ab5ae808b0efcb167a825b6cf7f3a7c0034bd99a6301d78db2012da651a8c0b9"
FIELDS = {
    "policyVersion", "service", "environment", "sourceRepository", "sourceWorkflow",
    "trustedRef", "githubEnvironment", "targetRepository", "targetBase", "targetBranch",
    "targetPath", "provenancePath", "rolloutPath", "namespace", "secretName",
    "secretType", "scope", "ownerSecretKeys", "certificate", "kubeseal",
    "allowedChangedPaths",
}
CANONICAL = {
    "ai-dev": {
        "service": "ai", "environment": "dev", "sourceRepository": "Team-PinLog/ai",
        "sourceWorkflow": ".github/workflows/ai-ci.yml", "trustedRef": "refs/heads/main",
        "githubEnvironment": "pinlog-secrets-dev", "targetBranch": "automation/secrets-ai-dev",
        "targetPath": "secrets/dev/ai-owner-secrets.sealedsecret.yaml",
        "provenancePath": ".github/pinlog/provenance/ai-dev.json",
        "rolloutPath": "apps/dev/ai/values.yaml", "namespace": "pinlog-dev",
        "secretName": "ai-owner-secrets",
        "ownerSecretKeys": ["GMS_API_KEY", "GMS_BASE_URL", "INTERNAL_SHARED_SECRET"],
    },
    "back-prod": {
        "service": "back", "environment": "prod", "sourceRepository": "Team-PinLog/back",
        "sourceWorkflow": ".github/workflows/backend-ci.yml", "trustedRef": "refs/heads/dev",
        "githubEnvironment": "pinlog-secrets-prod", "targetBranch": "automation/secrets-back-prod",
        "targetPath": "secrets/prod/back-owner-secrets.sealedsecret.yaml",
        "provenancePath": ".github/pinlog/provenance/back-prod.json",
        "rolloutPath": "apps/prod/back/values.yaml", "namespace": "pinlog-prod",
        "secretName": "back-owner-secrets",
        "ownerSecretKeys": ["GOOGLE_CLIENT_SECRET", "KAKAO_CLIENT_SECRET", "NAVER_CLIENT_SECRET"],
    },
}
PROVENANCE_FIELDS = {
    "sourceRepository", "sourceSha", "sourceRef", "sourceWorkflow", "sourceRunId",
    "policyDigest", "actionSha", "certSha256", "certFingerprint", "service",
    "environment", "githubEnvironment", "revision",
}
ANNOTATION_KEYS = {
    "secrets.pinlog.io/source-repository", "secrets.pinlog.io/source-sha",
    "secrets.pinlog.io/source-ref", "secrets.pinlog.io/source-workflow",
    "secrets.pinlog.io/source-run-id", "secrets.pinlog.io/policy-digest",
    "secrets.pinlog.io/action-sha", "secrets.pinlog.io/cert-sha256",
    "secrets.pinlog.io/cert-fingerprint", "secrets.pinlog.io/github-environment",
    "secrets.pinlog.io/revision",
}
OWNER_SECRET_KEYS = frozenset(
    key for contract in CANONICAL.values() for key in contract["ownerSecretKeys"]
)
PULL_REQUEST_URL = re.compile(
    r"https://github\.com/Team-PinLog/infra/pull/[1-9][0-9]*"
)


def mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def child_environment(source: Any = os.environ) -> dict[str, str]:
    return {key: value for key, value in source.items() if key not in OWNER_SECRET_KEYS}


def reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("symlink path components are forbidden")


def github_environment_name(value: Any, expected: str) -> str:
    if isinstance(value, str):
        name = value
    elif isinstance(value, dict) and set(value) in ({"name"}, {"name", "url"}):
        name = value.get("name")
        if "url" in value and not isinstance(value["url"], str):
            raise ValueError("GitHub Environment URL must be a string")
    else:
        raise ValueError("GitHub Environment shape is invalid")
    if name != expected:
        raise ValueError("executing job GitHub Environment mismatch")
    return expected


def validate_pull_request_url(value: str) -> str:
    if not isinstance(value, str) or not PULL_REQUEST_URL.fullmatch(value):
        raise ValueError("Infra pull request URL is invalid")
    return value


def validate_policy(policy: dict[str, Any], name: str) -> dict[str, Any]:
    if name not in CANONICAL or set(policy) != FIELDS:
        raise ValueError("policy identity or fields are not exact")
    expected = CANONICAL[name]
    for key, value in expected.items():
        if policy.get(key) != value:
            raise ValueError(f"canonical policy mismatch: {key}")
    if policy["policyVersion"] != "v1" or policy["targetRepository"] != "Team-PinLog/infra" or policy["targetBase"] != "main":
        raise ValueError("target contract mismatch")
    if policy["secretType"] != "Opaque" or policy["scope"] != "strict":
        raise ValueError("Secret type/scope mismatch")
    certificate = mapping(policy["certificate"], "certificate")
    if certificate != {
        "path": ".github/pinlog/sealed-secrets/pinlog-dev-cert.pem",
        "sha256": CERT_SHA, "x509FingerprintSha256": FINGERPRINT,
        "minValiditySeconds": 2592000,
    }:
        raise ValueError("certificate contract mismatch")
    kubeseal = mapping(policy["kubeseal"], "kubeseal")
    if kubeseal != {"version": "0.38.4", "archiveSha256": KUBESEAL_SHA}:
        raise ValueError("kubeseal contract mismatch")
    allowed = [policy["targetPath"], policy["provenancePath"], policy["rolloutPath"]]
    if policy["allowedChangedPaths"] != allowed:
        raise ValueError("changed path contract mismatch")
    for path in allowed:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("unsafe policy path")
    return policy


def load_policy(path: Path, name: str) -> dict[str, Any]:
    return validate_policy(mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "policy"), name)


def policy_digest(policy: dict[str, Any]) -> str:
    raw = yaml.safe_dump(policy, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def provenance_annotations(provenance: dict[str, str]) -> dict[str, str]:
    return {
        "secrets.pinlog.io/source-repository": provenance["sourceRepository"],
        "secrets.pinlog.io/source-sha": provenance["sourceSha"],
        "secrets.pinlog.io/source-ref": provenance["sourceRef"],
        "secrets.pinlog.io/source-workflow": provenance["sourceWorkflow"],
        "secrets.pinlog.io/source-run-id": provenance["sourceRunId"],
        "secrets.pinlog.io/policy-digest": provenance["policyDigest"],
        "secrets.pinlog.io/action-sha": provenance["actionSha"],
        "secrets.pinlog.io/cert-sha256": provenance["certSha256"],
        "secrets.pinlog.io/cert-fingerprint": provenance["certFingerprint"],
        "secrets.pinlog.io/github-environment": provenance["githubEnvironment"],
        "secrets.pinlog.io/revision": provenance["revision"],
    }


def build_manifest(policy: dict[str, Any], encrypted: dict[str, str], provenance: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "bitnami.com/v1alpha1", "kind": "SealedSecret",
        "metadata": {"name": policy["secretName"], "namespace": policy["namespace"], "annotations": provenance_annotations(provenance)},
        "spec": {"encryptedData": encrypted, "template": {
            "metadata": {"name": policy["secretName"], "namespace": policy["namespace"],
                         "annotations": {"secrets.pinlog.io/revision": provenance["revision"]}},
            "type": policy["secretType"],
        }},
    }


def validate_artifact(manifest: dict[str, Any], provenance: dict[str, str], policy: dict[str, Any]) -> None:
    if set(provenance) != PROVENANCE_FIELDS:
        raise ValueError("provenance fields are not exact")
    fixed = {
        "sourceRepository": policy["sourceRepository"], "sourceRef": policy["trustedRef"],
        "sourceWorkflow": policy["sourceWorkflow"], "policyDigest": policy_digest(policy),
        "certSha256": policy["certificate"]["sha256"],
        "certFingerprint": policy["certificate"]["x509FingerprintSha256"],
        "service": policy["service"], "environment": policy["environment"],
        "githubEnvironment": policy["githubEnvironment"],
    }
    if any(provenance.get(key) != value for key, value in fixed.items()):
        raise ValueError("provenance policy binding mismatch")
    if not SHA.fullmatch(str(provenance["sourceSha"])) or not SHA.fullmatch(str(provenance["actionSha"])):
        raise ValueError("source/action SHA is invalid")
    if not str(provenance["sourceRunId"]).isdigit() or provenance["revision"] != provenance["sourceSha"]:
        raise ValueError("run/revision binding is invalid")
    if set(manifest) != {"apiVersion", "kind", "metadata", "spec"} or manifest["apiVersion"] != "bitnami.com/v1alpha1" or manifest["kind"] != "SealedSecret":
        raise ValueError("manifest GVK is invalid")
    raw = json.dumps(manifest)
    if '"data"' in raw or '"stringData"' in raw or "PRIVATE KEY" in raw:
        raise ValueError("plaintext material is forbidden")
    metadata, spec = mapping(manifest["metadata"], "metadata"), mapping(manifest["spec"], "spec")
    template = mapping(spec.get("template"), "template")
    template_metadata = mapping(template.get("metadata"), "template metadata")
    encrypted = mapping(spec.get("encryptedData"), "encryptedData")
    if set(metadata) != {"name", "namespace", "annotations"} or set(spec) != {"encryptedData", "template"} or set(template) != {"metadata", "type"}:
        raise ValueError("manifest structure is not exact")
    if metadata["name"] != policy["secretName"] or metadata["namespace"] != policy["namespace"] or template["type"] != policy["secretType"]:
        raise ValueError("target binding mismatch")
    if template_metadata != {"name": policy["secretName"], "namespace": policy["namespace"], "annotations": {"secrets.pinlog.io/revision": provenance["revision"]}}:
        raise ValueError("template/rollout binding mismatch")
    if list(encrypted) != policy["ownerSecretKeys"] or any(not isinstance(value, str) or not CIPHERTEXT.fullmatch(value) for value in encrypted.values()):
        raise ValueError("ciphertext keys or values are invalid")
    actual_annotations = mapping(metadata["annotations"], "annotations")
    if set(actual_annotations) != ANNOTATION_KEYS or actual_annotations != provenance_annotations(provenance):
        raise ValueError("provenance annotations mismatch")


def kubeseal_invocation(binary: str, cert: str, namespace: str, name: str, value: str) -> tuple[list[str], str]:
    argv = [binary, "--raw", "--scope", "strict", "--namespace", namespace, "--name", name, "--cert", cert]
    return argv, value


def write_github_output(path: Path, name: str, value: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or any(token in value.lower() for token in ("\n", "\r", "%0a", "%0d")):
        raise ValueError("unsafe GitHub output")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def validate_run_readback(run: dict[str, Any], expected: dict[str, str]) -> None:
    repository = mapping(run.get("repository"), "run repository").get("full_name")
    branch = expected["ref"].removeprefix("refs/heads/")
    if repository != expected["repository"] or run.get("head_sha") != expected["sha"] or run.get("head_branch") != branch:
        raise ValueError("run source binding mismatch")
    if run.get("path") != expected["workflow"] or run.get("event") not in {"push", "workflow_dispatch"}:
        raise ValueError("run workflow/event mismatch")
    if run.get("status") not in {"queued", "in_progress", "completed"}:
        raise ValueError("run status invalid")
    if run.get("status") == "completed" and run.get("conclusion") != "success":
        raise ValueError("completed run was unsuccessful")


def verify_workflow_binding(workspace: Path, job_name: str, context: dict[str, str], policy: dict[str, Any]) -> None:
    if context.get("repository") != policy["sourceRepository"] or context.get("workflow") != policy["sourceWorkflow"]:
        raise ValueError("workflow repository/path binding mismatch")
    action_sha = context.get("action_sha", "")
    if not SHA.fullmatch(action_sha):
        raise ValueError("workflow action reference is not immutable")
    if not workspace.is_absolute() or workspace.is_symlink():
        raise ValueError("source workspace must be a direct absolute checkout")
    reject_symlink_components(workspace)
    resolved_workspace = workspace.resolve(strict=True)
    if resolved_workspace != workspace:
        raise ValueError("source workspace path must not contain symlinks")
    workspace = resolved_workspace
    relative = Path(policy["sourceWorkflow"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe workflow path")
    workflow_path = workspace / relative
    reject_symlink_components(workflow_path)
    if not workflow_path.is_file():
        raise ValueError("workflow file is unavailable or indirect")
    checkout_root = Path(run_checked(
        ["git", "rev-parse", "--show-toplevel"], cwd=workspace, capture_output=True,
    ).stdout.strip()).resolve(strict=True)
    if checkout_root != workspace:
        raise ValueError("source workspace is not the checkout root")
    expected_origin = f"https://github.com/{context['repository']}.git"
    origin = run_checked(
        ["git", "remote", "get-url", "origin"], cwd=workspace, capture_output=True,
    ).stdout.strip()
    if origin != expected_origin:
        raise ValueError("source checkout origin is not canonical")
    head = run_checked(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True).stdout.strip()
    if head != context.get("sha") or not SHA.fullmatch(head):
        raise ValueError("checked-out workflow SHA mismatch")
    committed = run_checked(
        ["git", "show", f"{head}:{relative.as_posix()}"], cwd=workspace, capture_output=True,
    ).stdout
    workflow = mapping(yaml.safe_load(committed), "workflow")
    jobs = mapping(workflow.get("jobs"), "workflow jobs")
    job = mapping(jobs.get(job_name), "executing workflow job")
    github_environment_name(job.get("environment"), policy["githubEnvironment"])
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise ValueError("executing workflow steps are invalid")
    expected_use = f"Team-PinLog/infra/.github/actions/sealedsecret-infra-pr@{action_sha}"
    matches = [step for step in steps if isinstance(step, dict) and step.get("uses") == expected_use]
    if len(matches) != 1:
        raise ValueError("executing workflow does not pin this action SHA exactly once")


def validate_oidc_claims(claims: dict[str, Any], policy: dict[str, Any], context: dict[str, str]) -> None:
    expected = {
        "aud": "pinlog-sealedsecret-infra-pr",
        "sub": f"repo:{policy['sourceRepository']}:environment:{policy['githubEnvironment']}",
        "repository": policy["sourceRepository"],
        "sha": context["sha"],
        "ref": policy["trustedRef"],
        "workflow_ref": f"{policy['sourceRepository']}/{policy['sourceWorkflow']}@{policy['trustedRef']}",
        "run_id": context["run_id"],
    }
    if any(str(claims.get(key, "")) != value for key, value in expected.items()):
        raise ValueError("GitHub OIDC Environment identity mismatch")


def fetch_oidc_claims(request_url: str, request_token: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(request_url)
    if parsed.scheme != "https" or parsed.hostname != "pipelines.actions.githubusercontent.com" or not request_token:
        raise ValueError("trusted GitHub OIDC endpoint is unavailable")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("audience", "pinlog-sealedsecret-infra-pr"))
    url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {request_token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        token = mapping(json.load(response), "OIDC response").get("value")
    if not isinstance(token, str) or token.count(".") != 2:
        raise ValueError("GitHub OIDC response is invalid")
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return mapping(json.loads(base64.urlsafe_b64decode(payload)), "OIDC claims")
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError("GitHub OIDC claims are invalid") from error


def api_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return mapping(json.load(response), "GitHub API response")


def run_checked(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs["env"] = child_environment(kwargs.get("env", os.environ))
    return subprocess.run(argv, check=True, text=True, **kwargs)


def verify_certificate(cert: Path, policy: dict[str, Any]) -> None:
    if hashlib.sha256(cert.read_bytes()).hexdigest() != policy["certificate"]["sha256"]:
        raise ValueError("certificate file digest mismatch")
    fingerprint = run_checked(["openssl", "x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256"], capture_output=True).stdout.strip().split("=", 1)[-1]
    if fingerprint != policy["certificate"]["x509FingerprintSha256"]:
        raise ValueError("certificate fingerprint mismatch")
    run_checked(["openssl", "x509", "-in", str(cert), "-noout", "-checkend", str(policy["certificate"]["minValiditySeconds"])], capture_output=True)


def patch_rollout(path: Path, revision: str) -> None:
    if not SHA.fullmatch(revision):
        raise ValueError("unsafe rollout revision")
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"(?m)^(  secrets\.pinlog\.io/revision:\s*).*$")
    if pattern.search(text):
        text = pattern.sub(rf"\g<1>{revision}", text)
    elif re.search(r"(?m)^podAnnotations:\s*$", text):
        text = re.sub(r"(?m)^podAnnotations:\s*$", f"podAnnotations:\n  secrets.pinlog.io/revision: {revision}", text, count=1)
    else:
        text = text.rstrip() + f"\n\npodAnnotations:\n  secrets.pinlog.io/revision: {revision}\n"
    path.write_text(text, encoding="utf-8")


def create_provenance(policy: dict[str, Any], context: dict[str, str]) -> dict[str, str]:
    return {
        "sourceRepository": context["repository"], "sourceSha": context["sha"],
        "sourceRef": context["ref"], "sourceWorkflow": context["workflow"],
        "sourceRunId": context["run_id"], "policyDigest": policy_digest(policy),
        "actionSha": context["action_sha"], "certSha256": policy["certificate"]["sha256"],
        "certFingerprint": policy["certificate"]["x509FingerprintSha256"],
        "service": policy["service"], "environment": policy["environment"],
        "githubEnvironment": policy["githubEnvironment"],
        "revision": context["sha"],
    }


def seal_values(policy: dict[str, Any], binary: str, cert: Path) -> dict[str, str]:
    encrypted: dict[str, str] = {}
    for key in policy["ownerSecretKeys"]:
        value = os.environ.get(key)
        if value is None or not value:
            raise ValueError(f"required owner key missing: {key}")
        argv, stdin = kubeseal_invocation(binary, str(cert), policy["namespace"], policy["secretName"], value)
        result = run_checked(argv, input=stdin, capture_output=True)
        ciphertext = result.stdout.strip()
        if not CIPHERTEXT.fullmatch(ciphertext):
            raise ValueError("kubeseal returned malformed ciphertext")
        encrypted[key] = ciphertext
    return encrypted


def fetch_remote_branch(checkout: Path, branch: str, env: dict[str, str]) -> str:
    remote_ref = f"refs/heads/{branch}"
    result = subprocess.run(
        ["git", "fetch", "origin", f"+{remote_ref}:refs/remotes/origin/{branch}"],
        cwd=checkout, env=child_environment(env), text=True, capture_output=True,
    )
    if result.returncode == 0:
        return run_checked(
            ["git", "rev-parse", f"refs/remotes/origin/{branch}"],
            cwd=checkout, capture_output=True,
        ).stdout.strip()
    if "couldn't find remote ref" in result.stderr:
        return ""
    raise subprocess.CalledProcessError(result.returncode, result.args)


def push_with_lease(checkout: Path, branch: str, old_sha: str, env: dict[str, str]) -> None:
    remote_ref = f"refs/heads/{branch}"
    lease = f"--force-with-lease=refs/heads/{branch}:{old_sha}"
    run_checked(
        ["git", "push", lease, "origin", f"HEAD:{remote_ref}"],
        cwd=checkout, env=env, capture_output=True,
    )


def validate_changed_paths(paths: list[str], policy: dict[str, Any]) -> None:
    if sorted(paths) != sorted(policy["allowedChangedPaths"]):
        raise ValueError("working tree changed paths are not exact")


def checkout_write_path(checkout: Path, relative_value: str) -> Path:
    if not checkout.is_absolute():
        raise ValueError("checkout path must be absolute")
    reject_symlink_components(checkout)
    root = checkout.resolve(strict=True)
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe checkout write path")
    candidate = root / relative
    reject_symlink_components(candidate)
    try:
        candidate.parent.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise ValueError("checkout write path escapes checkout") from error
    return candidate


def update_infra(policy: dict[str, Any], manifest: dict[str, Any], provenance: dict[str, str], token: str) -> tuple[str, str]:
    env = {**child_environment(), "GH_TOKEN": token}
    with tempfile.TemporaryDirectory(prefix="pinlog-infra-pr-") as directory:
        checkout = Path(directory) / "infra"
        run_checked(["gh", "repo", "clone", policy["targetRepository"], str(checkout), "--", "--quiet"], env=env, capture_output=True)
        run_checked(["git", "fetch", "origin", policy["targetBase"]], cwd=checkout, env=env, capture_output=True)
        branch = policy["targetBranch"]
        old_sha = fetch_remote_branch(checkout, branch, env)
        run_checked(["git", "switch", "-C", branch, f"origin/{policy['targetBase']}"], cwd=checkout, capture_output=True)
        manifest_path, provenance_path, rollout_path = (
            checkout_write_path(checkout, policy[key])
            for key in ("targetPath", "provenancePath", "rolloutPath")
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        provenance_path.write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        patch_rollout(rollout_path, provenance["revision"])
        changed = run_checked(["git", "status", "--short"], cwd=checkout, capture_output=True).stdout
        paths = sorted(line[3:] for line in changed.splitlines() if line)
        validate_changed_paths(paths, policy)
        run_checked(["git", "config", "user.name", "pinlog-secret-pr-bot"], cwd=checkout)
        run_checked(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=checkout)
        run_checked(["git", "add", "--", *policy["allowedChangedPaths"]], cwd=checkout)
        run_checked(["git", "commit", "-m", f"chore: refresh {policy['service']} {policy['environment']} owner Secret"], cwd=checkout, capture_output=True)
        push_with_lease(checkout, branch, old_sha, env)
        query = run_checked(["gh", "pr", "list", "--repo", policy["targetRepository"], "--base", policy["targetBase"], "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url // empty"], env=env, capture_output=True).stdout.strip()
        if query:
            pr_url = validate_pull_request_url(query)
        else:
            pr_url = validate_pull_request_url(run_checked(["gh", "pr", "create", "--draft", "--repo", policy["targetRepository"], "--base", policy["targetBase"], "--head", branch, "--title", f"chore: refresh {policy['service']} {policy['environment']} owner Secret", "--body", "Ciphertext-only SealedSecret refresh. Review provenance and rollout annotation before merge."], env=env, capture_output=True).stdout.strip())
        return branch, pr_url


def main() -> int:
    os.umask(0o077)
    policy_name = os.environ["PINLOG_POLICY"]
    if not re.fullmatch(r"(?:ai-dev|back-prod)", policy_name):
        raise ValueError("policy is not allowlisted")
    policy = load_policy(POLICY_DIR / f"{policy_name}.yaml", policy_name)
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    prefix = policy["sourceRepository"] + "/"
    suffix = "@" + policy["trustedRef"]
    if not workflow_ref.startswith(prefix) or not workflow_ref.endswith(suffix):
        raise ValueError("workflow ref binding mismatch")
    context = {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""), "sha": os.environ.get("GITHUB_SHA", ""),
        "ref": os.environ.get("GITHUB_REF", ""), "workflow": workflow_ref[len(prefix):-len(suffix)],
        "run_id": os.environ.get("GITHUB_RUN_ID", ""), "action_sha": os.environ.get("PINLOG_ACTION_SHA", ""),
    }
    if context["repository"] != policy["sourceRepository"] or context["ref"] != policy["trustedRef"] or context["workflow"] != policy["sourceWorkflow"]:
        raise ValueError("caller is not canonical")
    if os.environ.get("PINLOG_REVISION") != context["sha"] or not SHA.fullmatch(context["sha"]) or not SHA.fullmatch(context["action_sha"]) or not context["run_id"].isdigit():
        raise ValueError("immutable source identity is invalid")
    oidc_claims = fetch_oidc_claims(
        os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""),
        os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
    )
    validate_oidc_claims(oidc_claims, policy, context)
    verify_workflow_binding(
        Path(os.environ.get("GITHUB_WORKSPACE", "")),
        os.environ.get("GITHUB_JOB", ""),
        context,
        policy,
    )
    token = os.environ.get("PINLOG_INFRA_SECRET_PR_TOKEN", "")
    if not token:
        raise ValueError("Infra PR token is unavailable")
    run = api_json(f"https://api.github.com/repos/{context['repository']}/actions/runs/{context['run_id']}", token)
    validate_run_readback(run, context)
    cert = ROOT / policy["certificate"]["path"]
    verify_certificate(cert, policy)
    provenance = create_provenance(policy, context)
    manifest = build_manifest(policy, seal_values(policy, os.environ["PINLOG_KUBESEAL"], cert), provenance)
    validate_artifact(manifest, provenance, policy)
    branch, pr_url = update_infra(policy, manifest, provenance, token)
    output = Path(os.environ["GITHUB_OUTPUT"])
    write_github_output(output, "branch", branch)
    write_github_output(output, "pull_request_url", pr_url)
    print(f"prepared ciphertext-only {policy['service']} {policy['environment']} Infra Draft PR")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("sealedsecret Infra PR action failed (details withheld)", file=sys.stderr)
        raise SystemExit(1)
