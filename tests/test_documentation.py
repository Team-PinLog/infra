from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DocumentationConsistencyTest(unittest.TestCase):
    def test_operational_documents_exist_and_are_indexed(self):
        expected = {
            "docs/git-governance.md": "Git/CI 거버넌스",
            "docs/alerting.md": "운영 알림",
        }
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for relative_path, title in expected.items():
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())
                self.assertIn(f"[{title}]({relative_path})", readme)

    def test_stale_direct_push_and_disabled_alertmanager_guidance_is_removed(self):
        documents = {
            "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
            "docs/onboarding.md": (ROOT / "docs/onboarding.md").read_text(
                encoding="utf-8"
            ),
            "docs/architecture.md": (ROOT / "docs/architecture.md").read_text(
                encoding="utf-8"
            ),
            "docs/monitoring.md": (ROOT / "docs/monitoring.md").read_text(
                encoding="utf-8"
            ),
            "docs/backend-conventions.md": (
                ROOT / "docs/backend-conventions.md"
            ).read_text(encoding="utf-8"),
            "examples/README.md": (ROOT / "examples/README.md").read_text(
                encoding="utf-8"
            ),
            "examples/hello-service/README.md": (
                ROOT / "examples/hello-service/README.md"
            ).read_text(encoding="utf-8"),
        }
        stale_fragments = {
            "README.md": [
                "CI가 infra 저장소의 values.yaml tag를 yq로 갱신 후 커밋",
                "CI가 `infra`에 커밋",
            ],
            "docs/onboarding.md": ["2. main 브랜치에 푸시"],
            "docs/architecture.md": [
                "### 2.6 GitOps: CI가 태그를 커밋 (Image Updater 미사용)"
            ],
            "docs/monitoring.md": ["### Alertmanager 비활성"],
            "docs/backend-conventions.md": [
                "`main` 브랜치에 푸시하면 자동 배포됩니다."
            ],
            "examples/README.md": [
                "git add . && git commit",
                "`INFRA_REPO_TOKEN`",
            ],
            "examples/hello-service/README.md": [
                "푸시 한 번이 사람 손 없이 배포까지 도달",
                "`INFRA_REPO_TOKEN`",
            ],
        }

        for path, fragments in stale_fragments.items():
            for fragment in fragments:
                with self.subTest(path=path, fragment=fragment):
                    self.assertNotIn(fragment, documents[path])

    def test_git_governance_documents_enforced_controls(self):
        governance = (ROOT / "docs/git-governance.md").read_text(encoding="utf-8")
        for required in (
            "main 직접 push 금지",
            "기능 브랜치",
            "pr-policy",
            "guardrails",
            "helm",
            "승인 리뷰 0",
            "대화 해결",
            "full commit SHA",
            "dependabot[bot]",
            "monitor-state",
        ):
            with self.subTest(required=required):
                self.assertIn(required, governance)

    def test_alerting_document_separates_in_cluster_and_off_node_paths(self):
        alerting = (ROOT / "docs/alerting.md").read_text(encoding="utf-8")
        for required in (
            "Prometheus → Alertmanager → PinLog Sentinel Receiver → Mattermost",
            "Critical FIRING",
            "1시간",
            "Warning FIRING",
            "6시간",
            "RESOLVED",
            "GitHub-hosted runner",
            "단일 노드 전체 장애",
            "Mattermost 직접 전송",
            "의도적 예외",
        ):
            with self.subTest(required=required):
                self.assertIn(required, alerting)

    def test_relative_markdown_links_resolve(self):
        markdown_files = sorted(
            path for path in ROOT.rglob("*.md") if ".git" not in path.parts
        )
        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+\.md(?:#[^)]*)?)\)")

        broken: list[str] = []
        for source in markdown_files:
            text = source.read_text(encoding="utf-8")
            for raw_target in link_pattern.findall(text):
                target = raw_target.split("#", 1)[0]
                if "://" in target:
                    continue
                resolved = (source.parent / target).resolve()
                if not resolved.is_file():
                    broken.append(f"{source.relative_to(ROOT)} -> {raw_target}")

        self.assertEqual([], broken)


if __name__ == "__main__":
    unittest.main()
