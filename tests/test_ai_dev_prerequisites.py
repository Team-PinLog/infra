import tempfile
import unittest
from pathlib import Path
import subprocess

from tools.validate_ai_dev_prerequisites import (
    REQUIRED_RUNTIME_KEYS,
    validate_flyway_migrations,
    validate_profile_contract,
    validate_secret_keys,
)


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "ai-dev-prerequisites.md"
SQL = ROOT / "ops" / "ai-dev-prerequisites" / "bootstrap-pinlog-dev.sql"


class RuntimeSecretContractTest(unittest.TestCase):
    def test_required_key_schema_is_exact_and_contains_no_values(self):
        self.assertEqual(
            REQUIRED_RUNTIME_KEYS,
            {
                "DATABASE_URL",
                "GMS_API_KEY",
                "GMS_BASE_URL",
                "PINLOG_EMBEDDING_MODEL",
                "PINLOG_EMBEDDING_DIMENSION",
                "PINLOG_EMBEDDING_DISTANCE",
                "PINLOG_EMBEDDING_PROFILE",
                "INTERNAL_SHARED_SECRET",
            },
        )
        self.assertEqual(validate_secret_keys(REQUIRED_RUNTIME_KEYS), [])
        self.assertTrue(validate_secret_keys(REQUIRED_RUNTIME_KEYS - {"DATABASE_URL"}))
        self.assertTrue(validate_secret_keys(REQUIRED_RUNTIME_KEYS | {"UNAPPROVED"}))

    def test_secret_key_cli_never_echoes_a_malformed_value(self):
        with tempfile.TemporaryDirectory() as directory:
            keys = Path(directory) / "keys"
            keys.write_text("GMS_API_KEY=do-not-echo-this-value\n", encoding="utf-8")
            result = subprocess.run(
                [
                    "python3",
                    str(ROOT / "tools" / "validate_ai_dev_prerequisites.py"),
                    "secret-keys",
                    str(keys),
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("do-not-echo-this-value", result.stdout + result.stderr)

    def test_profile_must_resolve_to_an_approved_exact_tuple(self):
        runtime = {
            "PINLOG_EMBEDDING_PROFILE": "approved-profile",
            "PINLOG_EMBEDDING_MODEL": "approved-model",
            "PINLOG_EMBEDDING_DIMENSION": "1536",
            "PINLOG_EMBEDDING_DISTANCE": "cosine",
        }
        approved = {
            "approved-profile": {
                "model": "approved-model",
                "dimension": "1536",
                "distance": "cosine",
            }
        }
        self.assertEqual(validate_profile_contract(runtime, approved), [])
        for field, value in (
            ("PINLOG_EMBEDDING_MODEL", "other"),
            ("PINLOG_EMBEDDING_DIMENSION", "768"),
            ("PINLOG_EMBEDDING_DISTANCE", "l2"),
            ("PINLOG_EMBEDDING_PROFILE", "unknown"),
        ):
            with self.subTest(field=field):
                candidate = dict(runtime)
                candidate[field] = value
                self.assertTrue(validate_profile_contract(candidate, approved))


class DatabaseAndFlywayContractTest(unittest.TestCase):
    def test_flyway_requires_unique_v1_v100_v101_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("V1__base.sql", "V100__vector.sql", "V101__ai.sql"):
                (root / name).write_text("-- migration\n", encoding="utf-8")
            self.assertEqual(
                validate_flyway_migrations(root),
                ["V1__base.sql", "V100__vector.sql", "V101__ai.sql"],
            )
            (root / "V100__duplicate.sql").write_text("-- duplicate\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                validate_flyway_migrations(root)

    def test_runbook_and_sql_are_idempotent_fail_closed_and_non_destructive(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        sql = SQL.read_text(encoding="utf-8")
        for required in (
            "pinlog_dev",
            "NOLOGIN",
            "CREATE EXTENSION IF NOT EXISTS vector",
            "V1 → V100 → V101",
            "flyway_schema_history",
            "ai-runtime-secrets",
            "GitOps revert",
            "destructive DB rollback 금지",
            "bootstrap 데이터는 단순 image revert로 복원되지 않는다",
        ):
            self.assertIn(required, runbook + sql)
        for token in ("DROP DATABASE", "DROP ROLE", "DROP EXTENSION", "down migration"):
            self.assertNotIn(token, sql)
        self.assertNotIn("PASSWORD '", sql)
        self.assertNotIn("ELSE 1 / 0", sql)
        self.assertIn("NOBYPASSRLS", sql)
        self.assertIn("rolbypassrls = false", sql)
        for fail_closed_guard in (
            "rolsuper = false",
            "rolcreatedb = false",
            "rolcreaterole = false",
            "rolreplication = false",
            "pg_auth_members",
            "aclexplode",
            "REVOKE ALL ON DATABASE pinlog_dev FROM PUBLIC",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        ):
            self.assertIn(fail_closed_guard, sql)


if __name__ == "__main__":
    unittest.main()
