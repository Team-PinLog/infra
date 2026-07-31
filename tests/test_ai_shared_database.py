import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
JOB = ROOT / "platform" / "postgres" / "ai-shared-database-bootstrap.yaml"
CONFIGMAP = ROOT / "platform" / "postgres" / "ai-shared-database-bootstrap-configmap.yaml"
RUNBOOK = ROOT / "docs" / "ai-shared-database-recovery.md"


class AiSharedDatabaseContractTest(unittest.TestCase):
    def test_sql_preserves_role_and_old_database_but_grants_only_ai_schema(self):
        configmap = yaml.safe_load(CONFIGMAP.read_text(encoding="utf-8"))
        sql = configmap["data"]["bootstrap.sql"]
        required = (
            "pinlog_ai_dev",
            "current_database() = 'pinlog'",
            "rolcanlogin = true",
            "REVOKE ALL ON SCHEMA core FROM pinlog_ai_dev",
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA core FROM pinlog_ai_dev",
            "GRANT USAGE ON SCHEMA ai TO pinlog_ai_dev",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ai TO pinlog_ai_dev",
            "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA ai TO pinlog_ai_dev",
            "ALTER DEFAULT PRIVILEGES FOR ROLE pinlog IN SCHEMA ai",
            "has_schema_privilege('pinlog_ai_dev', 'core', 'USAGE') = false",
            "BEGIN;",
            "COMMIT;",
            "REVOKE TEMPORARY ON DATABASE pinlog FROM PUBLIC",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            "NOT has_database_privilege('pinlog_ai_dev', 'pinlog', 'TEMPORARY')",
            "NOT has_schema_privilege('pinlog_ai_dev', 'public', 'USAGE')",
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA ai FROM pinlog_ai_dev",
            "'TRUNCATE,REFERENCES,TRIGGER'",
        )
        for token in required:
            self.assertIn(token, sql)
        for forbidden in (
            "DROP DATABASE",
            "DROP ROLE",
            "ALTER ROLE pinlog_ai_dev PASSWORD",
            "CREATE DATABASE",
            "pinlog_dev OWNER",
        ):
            self.assertNotIn(forbidden, sql)

    def test_gitops_job_uses_admin_secret_without_exposing_ai_credentials(self):
        job = yaml.safe_load(JOB.read_text(encoding="utf-8"))
        self.assertEqual(job["kind"], "Job")
        self.assertEqual(job["metadata"]["namespace"], "pinlog-prod")
        self.assertEqual(job["metadata"]["name"], "postgres-ai-shared-db-v1")
        self.assertEqual(job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "1")
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["restartPolicy"], "Never")
        container = pod["containers"][0]
        self.assertEqual(container["env"][0]["valueFrom"]["secretKeyRef"], {"name": "postgres-credentials", "key": "password"})
        self.assertEqual(container["volumeMounts"][0]["mountPath"], "/bootstrap")
        self.assertEqual(pod["volumes"][0]["configMap"]["name"], "postgres-ai-shared-db-v1")
        rendered = JOB.read_text(encoding="utf-8")
        self.assertNotIn("ai-db-credentials", rendered)
        self.assertNotIn("DATABASE_URL", rendered)
        self.assertTrue(yaml.safe_load(CONFIGMAP.read_text(encoding="utf-8"))["immutable"])
        self.assertIn("새 SQL은 v2", RUNBOOK.read_text(encoding="utf-8"))

    def test_runbook_requires_ciphertext_owner_handoff_and_non_destructive_rollback(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for token in (
            "같은 database `pinlog`",
            "기존 AI role/user와 password를 유지",
            "DATABASE_URL",
            "ciphertext-only",
            "기존 `pinlog_dev` database와 `ai-db-credentials`를 삭제하지 않는다",
            "core 접근 거부",
            "V100__ai_tables.sql",
            "V101__ai_indexes.sql",
            "27 presets",
            "GitOps revert",
        ):
            self.assertIn(token, runbook)


if __name__ == "__main__":
    unittest.main()
