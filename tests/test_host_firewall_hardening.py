import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "host-firewall" / "harden-management-ports.sh"


class HostFirewallHardeningTest(unittest.TestCase):
    def test_preserves_consumers_before_blocking_external_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            log = temp / "ufw.log"
            fake_ufw = bin_dir / "ufw"
            fake_ufw.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    printf '%s\\n' "$*" >> "$UFW_TEST_LOG"
                    if [[ $* == "status numbered" ]]; then
                      printf '%s\\n' '[ 1] 8989/tcp ALLOW IN Anywhere'
                    elif [[ $* == status* ]]; then
                      printf '%s\\n' 'Status: active'
                    fi
                    """
                )
            )
            fake_ufw.chmod(0o755)
            for command in ("iptables-save", "ip6tables-save"):
                executable = bin_dir / command
                executable.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '*filter' 'COMMIT'\n")
                executable.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{bin_dir}:{env['PATH']}",
                    "PINLOG_FIREWALL_BACKUP_ROOT": str(temp / "backups"),
                    "PINLOG_FIREWALL_TEST_MODE": "1",
                    "UFW_TEST_LOG": str(log),
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            calls = log.read_text().splitlines()
            mutations = [call for call in calls if not call.startswith("--dry-run") and not call.startswith("status")]
            tail_allow = "allow in on tailscale0 to any port 8989 proto tcp comment Gerrit web via Tailscale"
            sentinel_allow = "allow in on cni0 from 10.42.0.0/16 to any port 9765 proto tcp comment Sentinel from k3s pods"
            delete_public = "--force delete allow 8989/tcp"
            self.assertLess(mutations.index(tail_allow), mutations.index(delete_public))
            self.assertLess(mutations.index(sentinel_allow), mutations.index(delete_public))

            for port in (8988, 8989, 29418, 9765, 9100, 10250, 6443):
                self.assertTrue(
                    any(
                        call.startswith(f"deny in on enX0 to any port {port} proto tcp")
                        for call in mutations
                    ),
                    f"missing enX0 deny for {port}",
                )

            backup_dirs = list((temp / "backups").iterdir())
            self.assertEqual(len(backup_dirs), 1)
            self.assertTrue((backup_dirs[0] / "rollback.sh").exists())
            self.assertTrue((backup_dirs[0] / "ufw-status.before").exists())


if __name__ == "__main__":
    unittest.main()
