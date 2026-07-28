from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bootstrap" / "03-install-argocd.sh"
RUNTIME_DOC = ROOT / "docs" / "container-runtime.md"


class RuntimeOperationsContractTest(unittest.TestCase):
    def test_bootstrap_bounds_high_volume_controller_logs(self):
        script = INSTALLER.read_text(encoding="utf-8")
        for setting in (
            '--set configs.params."controller\\.log\\.level"=warn',
            '--set configs.params."reposerver\\.log\\.level"=warn',
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, script)

    def test_cutover_runbook_matches_verified_runtime_boundary(self):
        document = RUNTIME_DOC.read_text(encoding="utf-8")
        for contract in (
            "systemctl stop k3s.service",
            "Kubernetes 관리 Docker container",
            "k3s-killall.sh",
            "containerd-pre-migration",
            "10-docker-runtime.conf",
            "systemctl start --no-block k3s.service",
            "k3s crictl ps",
            "desired=ready",
            "pg_isready",
            "VM reboot는 필요하지 않다",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, document)

    def test_runtime_runbook_has_data_safety_capacity_and_rollback_boundaries(self):
        document = RUNTIME_DOC.read_text(encoding="utf-8")
        for contract in (
            "PostgreSQL fresh backup",
            "pg_restore --list",
            "state.db",
            "server token",
            "atomic rename",
            "rollback",
            "5분 capacity gate",
            "CPU PSI avg60",
            "Docker package",
            "반복 restart, VM reboot, PVC 삭제로 우회하지 않는다",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, document)


if __name__ == "__main__":
    unittest.main()
