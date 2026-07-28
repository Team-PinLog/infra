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
APPROVED_FLYWAY_FILES = (
    "V1__create_schemas.sql",
    "V2__member.sql",
    "V3__core_domain.sql",
    "V100__ai_tables.sql",
    "V101__ai_indexes.sql",
    "V102__feed_event.sql",
)


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
    def _write_migrations(self, root, names=APPROVED_FLYWAY_FILES):
        for name in names:
            (root / name).write_text("-- migration\n", encoding="utf-8")

    def _assert_wrong_filename_rejected(self, approved_name, wrong_name):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_migrations(
                root,
                tuple(wrong_name if name == approved_name else name for name in APPROVED_FLYWAY_FILES),
            )
            with self.assertRaisesRegex(ValueError, "exact"):
                validate_flyway_migrations(root)

    def test_flyway_returns_exact_approved_files_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_migrations(root)
            self.assertEqual(validate_flyway_migrations(root), list(APPROVED_FLYWAY_FILES))

    def test_flyway_rejects_duplicate_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_migrations(root)
            (root / "V100__duplicate.sql").write_text("-- duplicate\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                validate_flyway_migrations(root)

    def test_flyway_rejects_wrong_v1_filename(self):
        self._assert_wrong_filename_rejected("V1__create_schemas.sql", "V1__wrong.sql")

    def test_flyway_rejects_wrong_v2_filename(self):
        self._assert_wrong_filename_rejected("V2__member.sql", "V2__wrong.sql")

    def test_flyway_rejects_wrong_v3_filename(self):
        self._assert_wrong_filename_rejected("V3__core_domain.sql", "V3__wrong.sql")

    def test_flyway_rejects_wrong_v100_filename(self):
        self._assert_wrong_filename_rejected("V100__ai_tables.sql", "V100__wrong.sql")

    def test_flyway_rejects_wrong_v101_filename(self):
        self._assert_wrong_filename_rejected("V101__ai_indexes.sql", "V101__wrong.sql")

    def test_flyway_rejects_wrong_v102_filename(self):
        self._assert_wrong_filename_rejected("V102__feed_event.sql", "V102__wrong.sql")

    def test_flyway_rejects_extra_versioned_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_migrations(root)
            (root / "V4__unapproved.sql").write_text("-- migration\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exact"):
                validate_flyway_migrations(root)

    def test_flyway_rejects_malformed_versioned_sql_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_migrations(root)
            (root / "V4_unapproved.sql").write_text("-- migration\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "malformed"):
                validate_flyway_migrations(root)

    def test_flyway_allows_unrelated_non_versioned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_migrations(root)
            (root / "README.md").write_text("notes\n", encoding="utf-8")
            (root / "seed.sql").write_text("-- unrelated\n", encoding="utf-8")
            self.assertEqual(validate_flyway_migrations(root), list(APPROVED_FLYWAY_FILES))

    def test_flyway_rejects_each_missing_required_version(self):
        for omitted in APPROVED_FLYWAY_FILES:
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in APPROVED_FLYWAY_FILES:
                    if name != omitted:
                        (root / name).write_text("-- migration\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "missing"):
                    validate_flyway_migrations(root)

    def test_runbook_queries_all_versioned_flyway_rows_and_pins_exact_source_set(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("WHERE version IS NOT NULL", runbook)
        self.assertNotIn("WHERE version IN", runbook)
        self.assertIn("exact pinned source set", runbook)
        self.assertIn(
            ",".join(APPROVED_FLYWAY_FILES),
            runbook.replace("`", "").replace("\n", "").replace(" ", ""),
        )

    def test_runbook_and_sql_are_idempotent_fail_closed_and_non_destructive(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        sql = SQL.read_text(encoding="utf-8")
        for required in (
            "pinlog_dev",
            "pinlog_ai_dev",
            "김세민",
            "NOLOGIN",
            "CREATE EXTENSION IF NOT EXISTS vector",
            "V1 → V2 → V3 → V100 → V101 → V102",
            "cc7753c6a32e6fe12bee694b4ca8004c8a8a4cbc",
            "sha256:57e2845efd62e7ba5c857ff39d1d4d59974908c06c0885fcf6aa50870626a8a3",
            "--spring.main.web-application-type=none",
            "--spring.main.banner-mode=off",
            "65.024s",
            "exit 0",
            "text-embedding-3-small",
            "1536",
            "cosine",
            "openai-text-embedding-3-small-1536-cosine-v1",
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
