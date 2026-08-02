from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap/03-install-argocd.sh"
RUNBOOK = ROOT / "docs/argocd-access-runbook.md"


class ArgoCdSafeBootstrapTest(unittest.TestCase):
    def test_bootstrap_never_reads_or_prints_initial_admin_credential(self):
        script = BOOTSTRAP.read_text(encoding="utf-8")
        forbidden = (
            "argocd-initial-admin-secret",
            ".data.password",
            "base64 -d",
            "초기 admin 비밀번호",
        )
        for fragment in forbidden:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, script)

    def test_bootstrap_output_is_safe_by_default_and_preserves_current_auth_mode(self):
        script = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('--set configs.cm."admin\\.enabled"=true', script)
        self.assertIn("--set dex.enabled=false", script)
        self.assertIn("민감정보를 출력하지 않습니다", script)
        completion = script.split("cat <<'EOF'", 1)[1]
        self.assertNotRegex(completion, re.compile(r"kubectl.*secret", re.IGNORECASE))
        self.assertNotIn("Secret 이름", completion)


class ArgoCdAccessRunbookContractTest(unittest.TestCase):
    def test_runbook_has_required_value_free_operational_sections(self):
        self.assertTrue(RUNBOOK.is_file())
        runbook = RUNBOOK.read_text(encoding="utf-8")
        required_sections = (
            "## 현재 상태와 변경 금지선",
            "## Account, RBAC, SSO 선택지",
            "## Credential owner와 storage",
            "## Rotation",
            "## Break-glass recovery",
            "## Rollback",
            "## Maintenance",
            "## UI/CLI 재검증",
            "## 전체 Application health 검증",
            "## argocd-server 내부 ingress NetworkPolicy 설계안",
            "## NetworkPolicy acceptance criteria",
        )
        for section in required_sections:
            with self.subTest(section=section):
                self.assertIn(section, runbook)

        for required in (
            "admin.enabled=true",
            "SSO 없음",
            "승인 전 적용 금지",
            "Tailscale",
            "port-forward",
            "manifest를 추가하거나 변경하지 않는다",
            "값을 출력하지",
        ):
            with self.subTest(required=required):
                self.assertIn(required, runbook)

        forbidden = (
            "argocd-initial-admin-secret",
            "jsonpath='{.data.password}'",
            "kubectl get secret",
        )
        for fragment in forbidden:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, runbook)


if __name__ == "__main__":
    unittest.main()
