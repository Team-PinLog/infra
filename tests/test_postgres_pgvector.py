from copy import deepcopy
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
STATEFULSET = ROOT / "platform" / "postgres" / "statefulset.yaml"
BACKUP = ROOT / "platform" / "postgres" / "backup-cronjob.yaml"
RUNBOOK = ROOT / "docs" / "postgres-pgvector-migration.md"
README = ROOT / "README.md"
ROLLBACK_STATEFULSET = ROOT / "docs" / "rollback" / "postgres-before-pgvector.yaml"
EXPECTED_IMAGE = (
    "pgvector/pgvector:0.8.5-pg16@"
    "sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
)
ROLLBACK_IMAGE = (
    "postgres:16-alpine@"
    "sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)


def documents(path: Path) -> list[dict]:
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def env_item(container: dict, name: str) -> dict:
    return next(item for item in container["env"] if item["name"] == name)


def assert_preserved_runtime_contract(statefulset: dict) -> None:
    spec = statefulset["spec"]
    container = spec["template"]["spec"]["containers"][0]

    assert spec["serviceName"] == "postgres"
    assert container["volumeMounts"] == [
        {"name": "data", "mountPath": "/var/lib/postgresql/data"}
    ]
    assert env_item(container, "POSTGRES_PASSWORD")["valueFrom"]["secretKeyRef"] == {
        "name": "postgres-credentials",
        "key": "password",
    }
    assert container["resources"] == {
        "requests": {"memory": "512Mi", "cpu": "250m"},
        "limits": {"memory": "1Gi", "cpu": "1"},
    }
    expected_probe_command = ["pg_isready", "-U", "pinlog"]
    assert container["livenessProbe"] == {
        "exec": {"command": expected_probe_command},
        "initialDelaySeconds": 30,
        "periodSeconds": 20,
    }
    assert container["readinessProbe"] == {
        "exec": {"command": expected_probe_command},
        "initialDelaySeconds": 10,
        "periodSeconds": 10,
    }


class PostgresPgvectorContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.statefulset = documents(STATEFULSET)[0]
        cls.container = cls.statefulset["spec"]["template"]["spec"]["containers"][0]

    def test_postgres_uses_immutable_pgvector_postgres16_image(self):
        self.assertEqual(EXPECTED_IMAGE, self.container["image"])

    def test_statefulset_explicitly_retains_existing_data_claim(self):
        self.assertEqual(
            {"whenDeleted": "Retain", "whenScaled": "Retain"},
            self.statefulset["spec"]["persistentVolumeClaimRetentionPolicy"],
        )
        claim = self.statefulset["spec"]["volumeClaimTemplates"][0]
        self.assertEqual("data", claim["metadata"]["name"])
        self.assertEqual("local-path-retain", claim["spec"]["storageClassName"])
        self.assertEqual("20Gi", claim["spec"]["resources"]["requests"]["storage"])

    def test_existing_database_layout_and_backup_client_contract_are_preserved(self):
        assert_preserved_runtime_contract(self.statefulset)

        env = {item["name"]: item for item in self.container["env"]}
        self.assertEqual("/var/lib/postgresql/data/pgdata", env["PGDATA"]["value"])
        self.assertEqual("pinlog", env["POSTGRES_DB"]["value"])
        self.assertEqual("pinlog", env["POSTGRES_USER"]["value"])

        cronjob = next(document for document in documents(BACKUP) if document["kind"] == "CronJob")
        backup_container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual("postgres:16-alpine", backup_container["image"])

    def test_preserved_runtime_contract_rejects_dangerous_mutations(self):
        mutations = {
            "serviceName": lambda spec, container: spec.__setitem__("serviceName", "wrong"),
            "mountPath": lambda spec, container: container["volumeMounts"][0].__setitem__("mountPath", "/wrong"),
            "secretName": lambda spec, container: env_item(container, "POSTGRES_PASSWORD")["valueFrom"]["secretKeyRef"].__setitem__("name", "wrong"),
            "secretKey": lambda spec, container: env_item(container, "POSTGRES_PASSWORD")["valueFrom"]["secretKeyRef"].__setitem__("key", "wrong"),
            "resources": lambda spec, container: container["resources"]["requests"].__setitem__("memory", "1Mi"),
            "liveness": lambda spec, container: container["livenessProbe"].__setitem__("periodSeconds", 1),
            "readiness": lambda spec, container: container["readinessProbe"]["exec"].__setitem__("command", ["true"]),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                candidate = deepcopy(self.statefulset)
                spec = candidate["spec"]
                container = spec["template"]["spec"]["containers"][0]
                mutate(spec, container)
                with self.assertRaises(AssertionError):
                    assert_preserved_runtime_contract(candidate)

    def test_migration_runbook_defines_approval_verification_and_phase_safe_rollback(self):
        self.assertTrue(RUNBOOK.is_file())
        runbook = RUNBOOK.read_text()
        required = [
            "S15P11A705-46",
            "승인 경계",
            "pg_restore --list",
            "pg_available_extensions",
            "CREATE EXTENSION IF NOT EXISTS vector",
            "rollout status statefulset/postgres --timeout=180s",
            "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
            "확장 생성 후",
            "stock PostgreSQL 이미지로 rollback하지 않는다",
            "TOC 엔트리 0개",
            "persistentVolumeReclaimPolicy",
            "deployment redis",
        ]
        for text in required:
            with self.subTest(required=text):
                self.assertIn(text, runbook)
        self.assertIn("[PostgreSQL pgvector 전환](docs/postgres-pgvector-migration.md)", README.read_text())

    def test_runbook_updates_preserved_extension_before_version_equality_verification(self):
        runbook = RUNBOOK.read_text()
        inspect_index = runbook.find("pg_available_extensions")
        update_index = runbook.find("ALTER EXTENSION vector UPDATE;")
        verify_index = runbook.rfind("pg_available_extensions")

        self.assertGreaterEqual(inspect_index, 0)
        self.assertGreater(update_index, inspect_index)
        self.assertGreater(verify_index, update_index)
        self.assertIn("installed_version = default_version", runbook)

    def test_runbook_requires_approval_before_extension_catalog_update(self):
        approval_boundary = RUNBOOK.read_text().split("## 1. 전환 직전 읽기 전용 점검", 1)[0]
        self.assertIn("ALTER EXTENSION vector UPDATE", approval_boundary)

    def test_runbook_has_explicit_empty_database_bootstrap_gate(self):
        runbook = RUNBOOK.read_text()
        required = (
            "초기 빈 DB 부트스트랩 예외",
            "사용자 테이블 0개",
            "사용자 시퀀스 0개",
            "사용자 뷰 0개",
            "사용자 승인",
            "TOC 0",
        )
        for text in required:
            with self.subTest(required=text):
                self.assertIn(text, runbook)

        self.assertIn("c.relkind IN ('r','p')", runbook)
        self.assertIn("n.nspname NOT IN ('pg_catalog','information_schema')", runbook)

    def test_runbook_links_the_fresh_backup_without_a_placeholder(self):
        runbook = RUNBOOK.read_text()
        self.assertNotIn("<검증할_dump_절대경로>", runbook)
        required = (
            "set -euo pipefail",
            "artifact_container=",
            "backup_host_path=",
            'test ! -L "$dump"',
            "job_started_epoch=",
            "dump_mtime_epoch=",
            "pg_restore --list",
        )
        for text in required:
            with self.subTest(required=text):
                self.assertIn(text, runbook)

    def test_static_rollback_manifest_changes_only_the_image(self):
        self.assertTrue(ROLLBACK_STATEFULSET.is_file())
        rollback = documents(ROLLBACK_STATEFULSET)[0]
        expected = deepcopy(self.statefulset)

        rollback_containers = rollback["spec"]["template"]["spec"]["containers"]
        postgres = [item for item in rollback_containers if item["name"] == "postgres"]
        self.assertEqual(1, len(postgres))
        self.assertEqual(ROLLBACK_IMAGE, postgres[0]["image"])

        expected_containers = expected["spec"]["template"]["spec"]["containers"]
        expected_postgres = [item for item in expected_containers if item["name"] == "postgres"]
        self.assertEqual(1, len(expected_postgres))
        expected_postgres[0]["image"] = ROLLBACK_IMAGE
        self.assertEqual(expected, rollback)

        runbook = RUNBOOK.read_text()
        self.assertIn("docs/rollback/postgres-before-pgvector.yaml", runbook)
        self.assertIn(ROLLBACK_IMAGE, runbook)


if __name__ == "__main__":
    unittest.main(verbosity=2)
