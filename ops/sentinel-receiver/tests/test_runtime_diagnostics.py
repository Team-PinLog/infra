import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class RuntimeAdapterTests(unittest.TestCase):
    def config(self):
        return {
            "prometheus_url": "http://10.43.1.20:9090",
            "loki_url": "http://10.43.1.21:3100",
        }

    def test_allowlisted_template_becomes_fixed_bounded_query_range_request(self):
        from runtime_diagnostics import DiagnosticQueryAdapter
        calls = []
        payload = json.dumps({"status":"success","data":{"result":[{"values":[[1,"0.12"]]}]}}).encode()
        def opener(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(payload)
        adapter = DiagnosticQueryAdapter(self.config(), opener=opener)
        result = adapter("prometheus", "backend_http_5xx_ratio", 100, 5000, 999, 30)
        self.assertEqual(result["value"], 0.12)
        request, timeout = calls[0]
        self.assertEqual(timeout, 3)
        self.assertEqual(request.method, "GET")
        self.assertTrue(request.full_url.startswith("http://10.43.1.20:9090/api/v1/query_range?"))
        self.assertIn("start=100", request.full_url)
        self.assertIn("end=1300", request.full_url)
        self.assertNotIn("backend_http_5xx_ratio", request.full_url)

    def test_prometheus_uses_last_finite_sample_and_median_of_prior_samples(self):
        from runtime_diagnostics import DiagnosticQueryAdapter
        values = [[1, "0.01"], [2, "NaN"], [3, "0.03"], [4, "+Inf"], [5, "0.02"], [6, "0.50"]]
        payload = json.dumps({"status": "success", "data": {"result": [{"values": values}]}}).encode()
        adapter = DiagnosticQueryAdapter(self.config(), opener=lambda *_: FakeResponse(payload))

        self.assertEqual(
            adapter("prometheus", "infra_ready_ratio", 1, 1200, 100, 3),
            {"value": 0.5, "baseline": 0.02},
        )

    def test_prometheus_does_not_invent_baseline_for_empty_nonfinite_or_short_series(self):
        from runtime_diagnostics import DiagnosticQueryAdapter
        for values, expected in (
            ([], {}),
            ([[1, "NaN"], [2, "-Inf"]], {}),
            ([[1, "0.4"]], {"value": 0.4}),
            ([[1, "0.1"], [2, "0.2"], [3, "0.3"]], {"value": 0.3}),
        ):
            payload = json.dumps({"status": "success", "data": {"result": [{"values": values}]}}).encode()
            adapter = DiagnosticQueryAdapter(self.config(), opener=lambda *_args, payload=payload: FakeResponse(payload))
            with self.subTest(values=values):
                self.assertEqual(adapter("prometheus", "infra_ready_ratio", 1, 1200, 100, 3), expected)

    def test_unknown_template_source_or_unsafe_endpoint_is_rejected(self):
        from runtime_diagnostics import DiagnosticQueryAdapter, ConfigurationError
        adapter = DiagnosticQueryAdapter(self.config(), opener=lambda *_: self.fail("network called"))
        for args in (("prometheus", "raw user query"), ("unknown", "backend_http_5xx_ratio")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                adapter(args[0], args[1], 1, 2, 1, 1)
        for url in ("http://monitoring.svc:9090", "http://10.43.1.20:80", "http://127.0.0.1:9090", "https://user@10.43.1.20:9090"):
            config = self.config(); config["prometheus_url"] = url
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                DiagnosticQueryAdapter(config)

    def test_loki_output_and_http_response_are_capped_and_redirects_rejected(self):
        from runtime_diagnostics import DiagnosticQueryAdapter, ResponseTooLarge, NoRedirectHandler
        oversized = b"x" * (64 * 1024 + 1)
        adapter = DiagnosticQueryAdapter(self.config(), opener=lambda *_: FakeResponse(oversized))
        with self.assertRaises(ResponseTooLarge):
            adapter("loki", "backend_error_logs", 1, 2, 100, 3)
        handler = NoRedirectHandler()
        with self.assertRaises(HTTPError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://evil")

    def test_loki_returns_only_bounded_redacted_records(self):
        from runtime_diagnostics import DiagnosticQueryAdapter
        secret = "sk-live-SENTINEL-secret-123456"
        streams = [{"values":[[str(i), f"Authorization: Bearer {secret} ERROR " + "x" * 250]]} for i in range(100)]
        payload = json.dumps({"status":"success","data":{"result":streams}}).encode()
        adapter = DiagnosticQueryAdapter(self.config(), opener=lambda *_: FakeResponse(payload))
        output = adapter("loki", "backend_error_logs", 1, 2, 100, 3)
        self.assertLessEqual(len(output), 100)
        self.assertTrue(all(set(record) == {"timestamp", "line", "source"} for record in output))
        self.assertTrue(all(len(record["line"].encode()) <= 2048 for record in output))
        self.assertNotIn(secret, repr(output))


class RuntimeWiringAndInstallTests(unittest.TestCase):
    def test_receiver_builds_adapter_from_systemd_credential(self):
        import tempfile
        from receiver import build_diagnostic_query
        from runtime_diagnostics import DiagnosticQueryAdapter
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "diagnostics_config").write_text(json.dumps({
                "prometheus_url":"http://10.43.1.20:9090",
                "loki_url":"http://10.43.1.21:3100"}), encoding="utf-8")
            self.assertIsInstance(build_diagnostic_query(Path(directory)), DiagnosticQueryAdapter)

    def test_installer_snapshots_service_cluster_ips_without_runtime_kubeconfig(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        unit = (ROOT / "pinlog-sentinel-receiver.service").read_text(encoding="utf-8")
        self.assertIn("runtime_diagnostics.py", installer)
        self.assertIn("diagnostics_config", installer)
        self.assertIn("clusterIP", installer)
        self.assertIn("LoadCredential=diagnostics_config:/etc/pinlog-sentinel/diagnostics.json", unit)
        self.assertNotIn("KUBECONFIG", unit)


if __name__ == "__main__":
    unittest.main()
