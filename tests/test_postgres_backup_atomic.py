from pathlib import Path
import os
import subprocess
import tempfile
import textwrap
import time
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "platform" / "postgres" / "backup-cronjob.yaml"


def backup_contract() -> tuple[dict, str]:
    cronjob = next(
        document
        for document in yaml.safe_load_all(MANIFEST.read_text())
        if document and document.get("kind") == "CronJob"
    )
    script = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    return cronjob, script


class BackupFixture:
    def __init__(self, mode: str = "success"):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.backup = self.root / "backup"
        self.bin = self.root / "bin"
        self.backup.mkdir()
        self.bin.mkdir()
        self.env = os.environ.copy()
        self.env.update({"BACKUP_DIR": str(self.backup), "PATH": f"{self.bin}:{self.env['PATH']}", "DUMP_MODE": mode})
        self._stub(
            "pg_dump",
            """
            #!/bin/sh
            set -eu
            out=
            while [ "$#" -gt 0 ]; do
              if [ "$1" = -f ]; then out=$2; shift 2; else shift; fi
            done
            case "${DUMP_MODE:-success}" in
              success) printf 'valid custom archive\n' > "$out" ;;
              nonzero) printf 'incomplete\n' > "$out"; exit 23 ;;
              signal) printf 'incomplete\n' > "$out"; kill -TERM "$PPID"; exit 143 ;;
            esac
            """,
        )
        self._stub(
            "pg_restore",
            """
            #!/bin/sh
            set -eu
            [ "$1" = --list ]
            [ "${RESTORE_MODE:-success}" = success ]
            grep -q '^valid custom archive$' "$2"
            if [ -n "${RACE_FINAL:-}" ]; then
              printf 'competitor archive\n' > "${RACE_FINAL}"
            fi
            """,
        )

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(textwrap.dedent(body).lstrip())
        path.chmod(0o755)

    def run(self, restore_mode: str = "success") -> subprocess.CompletedProcess[str]:
        _, script = backup_contract()
        env = dict(self.env, RESTORE_MODE=restore_mode)
        return subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=10)

    def close(self) -> None:
        self.temporary.cleanup()


class AtomicPostgresBackupTest(unittest.TestCase):
    def test_manifest_preserves_schedule_storage_secret_and_forbids_overlap(self):
        cronjob, script = backup_contract()
        self.assertEqual("0 19 * * *", cronjob["spec"]["schedule"])
        self.assertEqual("Forbid", cronjob["spec"]["concurrencyPolicy"])
        container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual("postgres:16-alpine", container["image"])
        self.assertEqual(
            {"name": "postgres-credentials", "key": "password"},
            container["env"][0]["valueFrom"]["secretKeyRef"],
        )
        pvc = next(document for document in yaml.safe_load_all(MANIFEST.read_text()) if document and document.get("kind") == "PersistentVolumeClaim")
        self.assertEqual("10Gi", pvc["spec"]["resources"]["requests"]["storage"])
        self.assertIn("set -euo pipefail", script)
        self.assertIn("trap ", script)
        self.assertIn('exec 9<"${BACKUP_DIR}"', script)
        self.assertIn("flock -n 9", script)
        self.assertNotIn("LOCK_DIR", script)

    def test_success_validates_partial_then_atomically_publishes_final_and_latest(self):
        fixture = BackupFixture()
        self.addCleanup(fixture.close)
        result = fixture.run()
        self.assertEqual(0, result.returncode, result.stderr)
        finals = list(fixture.backup.glob("pinlog-????????-??????.dump"))
        self.assertEqual(1, len(finals))
        self.assertEqual("valid custom archive\n", finals[0].read_text())
        self.assertFalse(any(fixture.backup.glob("*.partial*")))
        latest = fixture.backup / "latest.dump"
        self.assertTrue(latest.is_symlink())
        self.assertEqual(finals[0].resolve(), latest.resolve())

    def test_dump_nonzero_or_signal_removes_partial_and_returns_nonzero(self):
        for mode in ("nonzero", "signal"):
            with self.subTest(mode=mode):
                fixture = BackupFixture(mode)
                self.addCleanup(fixture.close)
                result = fixture.run()
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], list(fixture.backup.iterdir()))

    def test_validation_failure_removes_unpublished_artifacts_and_preserves_valid_latest(self):
        fixture = BackupFixture()
        self.addCleanup(fixture.close)
        old = fixture.backup / "pinlog-20000101-000000.dump"
        old.write_text("previous valid archive\n")
        (fixture.backup / "latest.dump").symlink_to(old.name)
        result = fixture.run(restore_mode="fail")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([old], list(fixture.backup.glob("pinlog-????????-??????.dump")))
        self.assertEqual(old.resolve(), (fixture.backup / "latest.dump").resolve())
        self.assertFalse(any(fixture.backup.glob("*.partial*")))
        self.assertFalse(any(fixture.backup.glob(".latest.dump.*")))

    def test_collision_fails_without_overwriting_valid_final(self):
        fixture = BackupFixture()
        self.addCleanup(fixture.close)
        _, script = backup_contract()
        fixed_date = fixture.bin / "date"
        fixed_date.write_text("#!/bin/sh\nprintf '20260101-010101\\n'\n")
        fixed_date.chmod(0o755)
        existing = fixture.backup / "pinlog-20260101-010101.dump"
        existing.write_text("do not overwrite\n")
        result = subprocess.run(["bash", "-c", script], env=fixture.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("do not overwrite\n", existing.read_text())
        self.assertFalse(any(fixture.backup.glob("*.partial*")))

    def test_publish_race_fails_without_overwriting_competing_final(self):
        fixture = BackupFixture()
        self.addCleanup(fixture.close)
        _, script = backup_contract()
        fixed_date = fixture.bin / "date"
        fixed_date.write_text("#!/bin/sh\nprintf '20260101-010101\\n'\n")
        fixed_date.chmod(0o755)
        final = fixture.backup / "pinlog-20260101-010101.dump"
        env = dict(fixture.env, RESTORE_MODE="success", RACE_FINAL=str(final))
        result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("competitor archive\n", final.read_text())
        self.assertFalse(any(fixture.backup.glob("*.partial*")))
        self.assertFalse((fixture.backup / "latest.dump").exists())

    def test_kernel_lock_blocks_overlap_and_releases_without_persistent_artifact(self):
        import fcntl

        fixture = BackupFixture()
        self.addCleanup(fixture.close)
        descriptor = os.open(fixture.backup, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = fixture.run()
            self.assertNotEqual(0, blocked.returncode)
            self.assertIn("다른 백업 실행", blocked.stderr)
            self.assertEqual([], list(fixture.backup.iterdir()))
        finally:
            os.close(descriptor)
        successful = fixture.run()
        self.assertEqual(0, successful.returncode, successful.stderr)

    def test_retention_only_deletes_old_validated_final_archive_names(self):
        fixture = BackupFixture()
        self.addCleanup(fixture.close)
        old_final = fixture.backup / "pinlog-20000101-000000.dump"
        old_partial = fixture.backup / ".pinlog-20000101-000000.dump.partial.keep"
        failed_artifact = fixture.backup / "pinlog-failed.dump"
        non_digit_artifact = fixture.backup / "pinlog-AAAAAAAA-BBBBBB.dump"
        for path in (old_final, old_partial, failed_artifact, non_digit_artifact):
            path.write_text(path.name)
            os.utime(path, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        result = fixture.run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(old_final.exists())
        self.assertTrue(old_partial.exists())
        self.assertTrue(failed_artifact.exists())
        self.assertTrue(non_digit_artifact.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
