import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
TUNER = ROOT / "bootstrap" / "tune-metrics-server.sh"
UNIT = ROOT / "bootstrap" / "pinlog-metrics-server-tuning.service"
TIMER = ROOT / "bootstrap" / "pinlog-metrics-server-tuning.timer"
INSTALLER = ROOT / "bootstrap" / "install-metrics-server-tuning.sh"
CONTRACT = ROOT / "docs" / "metrics-server.md"
README = ROOT / "README.md"


class MetricsServerTuningTest(unittest.TestCase):
    def test_operational_contract_documents_cause_validation_and_rollback(self):
        text = CONTRACT.read_text()
        self.assertIn("docs/metrics-server.md", README.read_text())
        for required in (
            "Docker·cri-dockerd",
            "--metric-resolution=60s",
            "--kubelet-request-timeout=30s",
            "kubectl top node",
            "systemctl disable --now pinlog-metrics-server-tuning.timer",
            "systemctl disable --now pinlog-metrics-server-tuning.service",
            "재부팅이나 k3s 재시작은 필요하지 않는다",
        ):
            self.assertIn(required, text)

    def test_systemd_timer_retries_failures_and_repairs_late_drift(self):
        unit = UNIT.read_text()
        timer = TIMER.read_text()
        installer = INSTALLER.read_text()
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=15s", unit)
        self.assertIn("TimeoutStartSec=360", unit)
        self.assertNotIn("Requires=k3s.service", unit)
        self.assertIn(
            "ExecCondition=/usr/bin/systemctl is-active --quiet k3s.service", unit
        )
        self.assertIn("OnBootSec=2min", timer)
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn(
            "systemctl enable --now pinlog-metrics-server-tuning.timer", installer
        )

    def test_systemd_unit_reapplies_tuning_after_k3s_restart(self):
        unit = UNIT.read_text()
        installer = INSTALLER.read_text()
        self.assertIn("After=k3s.service", unit)
        self.assertIn("PartOf=k3s.service", unit)
        self.assertIn("WantedBy=k3s.service", unit)
        self.assertIn(
            "ExecStart=/usr/local/sbin/pinlog-tune-metrics-server.sh", unit
        )
        self.assertIn("install -m 0755", installer)
        self.assertIn("systemctl enable pinlog-metrics-server-tuning.service", installer)
        self.assertIn("systemctl restart pinlog-metrics-server-tuning.service", installer)

    def test_tuner_updates_only_latency_flags_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            state = tmp / "state.json"
            calls = tmp / "calls.json"
            fake_kubectl = tmp / "kubectl"
            state.write_text(
                json.dumps(
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "metrics-server",
                                            "args": [
                                                "--cert-dir=/tmp",
                                                "--metric-resolution=15s",
                                                "--kubelet-request-timeout=10s",
                                                "--secure-port=10250",
                                            ],
                                        }
                                    ]
                                }
                            }
                        },
                    }
                )
            )
            calls.write_text(json.dumps({"patches": 0, "reverted": False}))
            fake_kubectl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    from pathlib import Path
                    import sys

                    args = sys.argv[1:]
                    state_path = Path(os.environ["FAKE_STATE"])
                    calls_path = Path(os.environ["FAKE_CALLS"])
                    if "get" in args and "deployment" in args:
                        calls = json.loads(calls_path.read_text())
                        if (
                            os.environ.get("FAKE_REVERT_AFTER_PATCH") == "1"
                            and calls["patches"] > 0
                            and not calls["reverted"]
                        ):
                            state = json.loads(state_path.read_text())
                            state["spec"]["template"]["spec"]["containers"][0]["args"] = [
                                "--cert-dir=/tmp",
                                "--metric-resolution=15s",
                                "--kubelet-request-timeout=10s",
                                "--secure-port=10250",
                            ]
                            state_path.write_text(json.dumps(state))
                            calls["reverted"] = True
                            calls_path.write_text(json.dumps(calls))
                        print(state_path.read_text())
                    elif "patch" in args and "deployment" in args:
                        patch_path = Path(args[args.index("--patch-file") + 1])
                        patch = json.loads(patch_path.read_text())
                        state = json.loads(state_path.read_text())
                        state["spec"]["template"]["spec"]["containers"][0]["args"] = patch["spec"]["template"]["spec"]["containers"][0]["args"]
                        state_path.write_text(json.dumps(state))
                        calls = json.loads(calls_path.read_text())
                        calls["patches"] += 1
                        calls_path.write_text(json.dumps(calls))
                    elif "rollout" in args and "status" in args:
                        pass
                    else:
                        print(f"unexpected kubectl args: {args}", file=sys.stderr)
                        raise SystemExit(2)
                    """
                )
            )
            fake_kubectl.chmod(0o755)
            env = {
                **os.environ,
                "KUBECTL": str(fake_kubectl),
                "FAKE_STATE": str(state),
                "FAKE_CALLS": str(calls),
                "SLEEP_SECONDS": "0",
                "VERIFY_SLEEP_SECONDS": "0",
            }

            subprocess.run(["bash", str(TUNER)], check=True, env=env)
            result = json.loads(state.read_text())
            args = result["spec"]["template"]["spec"]["containers"][0]["args"]
            self.assertEqual(args.count("--metric-resolution=60s"), 1)
            self.assertEqual(args.count("--kubelet-request-timeout=30s"), 1)
            self.assertIn("--cert-dir=/tmp", args)
            self.assertIn("--secure-port=10250", args)
            self.assertEqual(json.loads(calls.read_text())["patches"], 1)

            subprocess.run(["bash", str(TUNER)], check=True, env=env)
            self.assertEqual(json.loads(calls.read_text())["patches"], 1)

            drifted = json.loads(state.read_text())
            drifted["spec"]["template"]["spec"]["containers"][0]["args"] = [
                "--cert-dir=/tmp",
                "--metric-resolution=15s",
                "--kubelet-request-timeout=10s",
                "--secure-port=10250",
            ]
            state.write_text(json.dumps(drifted))
            calls.write_text(json.dumps({"patches": 0, "reverted": False}))
            drift_env = {**env, "FAKE_REVERT_AFTER_PATCH": "1"}
            drift_result = subprocess.run(["bash", str(TUNER)], env=drift_env)
            self.assertNotEqual(drift_result.returncode, 0)
            self.assertTrue(json.loads(calls.read_text())["reverted"])


if __name__ == "__main__":
    unittest.main()
