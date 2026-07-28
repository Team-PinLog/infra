from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bootstrap" / "03-install-argocd.sh"
RUNTIME_DOC = ROOT / "docs" / "container-runtime.md"


class ArgoCdRuntimeLogContractTest(unittest.TestCase):
    def test_bootstrap_bounds_high_volume_controller_logs(self):
        script = INSTALLER.read_text(encoding="utf-8")
        for setting in (
            '--set configs.params."controller\\.log\\.level"=warn',
            '--set configs.params."reposerver\\.log\\.level"=warn',
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, script)

    def test_runtime_runbook_has_maintenance_and_rollback_boundaries(self):
        document = RUNTIME_DOC.read_text(encoding="utf-8")
        for contract in (
            "ReopenContainerLog",
            '"max-size": "8m"',
            '"max-file": "3"',
            "fresh PostgreSQL backup",
            "dockerd --validate",
            "systemctl restart docker",
            "rollback",
            "containerLogMaxSize=10Mi",
            "5분",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, document)


if __name__ == "__main__":
    unittest.main()
