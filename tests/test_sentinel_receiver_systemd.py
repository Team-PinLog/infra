from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "ops" / "sentinel-receiver" / "pinlog-sentinel-receiver.service"


class SentinelReceiverSystemdContractTest(unittest.TestCase):
    def test_receiver_is_ordered_after_but_not_lifecycle_bound_to_k3s(self):
        unit = UNIT.read_text(encoding="utf-8")
        self.assertIn("After=network-online.target k3s.service", unit)
        self.assertIn("Wants=network-online.target", unit)

        unit_section = unit.split("[Unit]", 1)[1].split("[Service]", 1)[0]
        for lifecycle_directive in ("Requires", "BindsTo", "PartOf"):
            with self.subTest(lifecycle_directive=lifecycle_directive):
                self.assertNotRegex(
                    unit_section,
                    rf"(?m)^\\s*{lifecycle_directive}\\s*=.*\\bk3s\\.service\\b",
                )

        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=multi-user.target", unit)


if __name__ == "__main__":
    unittest.main()
