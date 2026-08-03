import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INOTIFY_SYSCTL = ROOT / "bootstrap/sysctl.d/99-pinlog-inotify.conf"
RUNBOOK = ROOT / "docs/runbook.md"


class HostInotifyContractTests(unittest.TestCase):
    def test_repo_owns_only_the_minimum_inotify_instance_override(self):
        declarations = [
            line.strip()
            for line in INOTIFY_SYSCTL.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        self.assertEqual(declarations, ["fs.inotify.max_user_instances = 512"])
        self.assertNotIn("max_user_watches", INOTIFY_SYSCTL.read_text(encoding="utf-8"))
        self.assertNotIn("fs.file-max", INOTIFY_SYSCTL.read_text(encoding="utf-8"))

    def test_runbook_applies_only_the_merged_owned_key_without_restart(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")

        self.assertIn("bootstrap/sysctl.d/99-pinlog-inotify.conf", runbook)
        self.assertIn("/etc/sysctl.d/99-pinlog-inotify.conf", runbook)
        self.assertIn("cmp --silent", runbook)
        self.assertIn("sysctl -w fs.inotify.max_user_instances=512", runbook)
        self.assertNotIn("sysctl --system", runbook)


if __name__ == "__main__":
    unittest.main()
