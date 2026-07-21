# Git/CI 거버넌스

PinLog `infra` 저장소의 변경·검증·병합·공급망 보안 규칙을 정의한다.
이 문서는 운영 정책의 기준이며, 실제 GitHub 설정과 workflow가 다르면 설정을 먼저
확인하고 문서를 함께 수정한다.

**검증 기준일**: 2026-07-21

---

## 1. 현재 적용 범위

### 검증된 구현

`Team-PinLog/infra`에는 다음 정책이 실제 적용되어 있다.

- 사람과 AI 모두 **main 직접 push 금지**
- protected `main` 변경은 **기능 브랜치**와 Pull Request를 통해서만 반영
- 관리자에게도 branch protection 적용
- 필수 checks: `pr-policy`, `guardrails`, `helm`
- 최신 `main` 기준 strict status checks
- 승인 리뷰 0 — 단일 운영자 구조에서 형식적 self-approval은 요구하지 않음
- 미해결 PR **대화 해결** 필수
- force push와 protected branch 삭제 금지
- GitHub 설정상 merge commit·squash·rebase를 허용하지만 PinLog 자동화·운영 절차는
  squash merge 사용
- merge 후 기능 브랜치 자동 삭제
- GitHub Actions 외부 의존성은 **full commit SHA**로 고정
- secret scanning, push protection, Dependabot security updates 활성화

### 운영 convention — GitHub가 강제하지 않음

다음은 현재 자동 검증되는 control이 아니라 팀 운영 표준이다.

- 기능 브랜치 이름에 Jira 키 포함
- 사람이 수동 merge할 때도 squash 방식 사용

`pr-policy`는 PR 본문의 Jira 키를 검사하지만 branch 이름은 검사하지 않는다.
convention을 강제 control로 바꾸려면 별도 검사와 mutation test를 추가한 뒤 이 문서의
“검증된 구현” 목록으로 이동한다.

### 아직 구현되지 않은 범위

`back`과 `front` 저장소는 현재 커밋과 CI workflow가 없다. 따라서 다음은 목표
배포 계약이며 아직 운영 E2E가 아니다.

- 서비스 코드 PR → 서비스 CI → GHCR 불변 이미지 생성
- 이미지 tag 변경용 infra 기능 브랜치 생성
- infra PR 생성 → 필수 checks → merge
- Argo CD sync → 런타임 검증

서비스 CI가 `infra/main`에 직접 commit하는 방식은 금지한다. 자동화할 때도 bot은
기능 브랜치와 PR을 사용해야 한다.

---

## 2. 변경 흐름

```text
Jira 작업
  → 기능 브랜치
  → RED / GREEN / 회귀 검증
  → Pull Request
  → pr-policy + guardrails + helm
  → squash merge (PinLog 운영 표준)
  → Argo CD가 main 감지
  → 배포 및 런타임 검증
```

### 브랜치 이름 convention

추적성을 위해 Jira 키를 포함한다. 현재는 운영 convention이며 CI가 branch 이름
자체를 검사하지는 않는다.

```text
feat/S15P11A705-123-short-description
fix/S15P11A705-123-short-description
docs/S15P11A705-123-short-description
```

### 일반 변경 절차

```bash
git fetch origin main
git switch main
git merge --ff-only origin/main
git switch -c docs/S15P11A705-123-short-description

# 변경과 테스트
git add <파일>
git commit -m "docs: 변경 내용"
git push -u origin HEAD

gh pr create --base main --head "$(git branch --show-current)"
```

`main`에서 작업을 시작했더라도 commit 전에 기능 브랜치로 이동한다. 보호 규칙을
우회하거나 관리자 권한으로 main에 밀어 넣지 않는다.

---

## 3. PR 계약

일반 PR 본문에는 다음 항목이 필요하다.

- Jira 키와 링크 또는 작업 설명
- 실행한 테스트 명령
- RED: 변경 전 실패와 non-zero exit
- GREEN: 목표 테스트 성공과 exit 0
- Regression: 전체 회귀 성공과 exit 0

PR 본문 정책은 `pull_request_target`에서 기본 브랜치의 신뢰된
`tools/validate_pr_body.py`를 실행한다. PR 브랜치의 검사기나 PR-controlled shell을
실행하지 않는다. 본문 수정 시에도 `pr-policy`를 다시 실행한다.

이 증거는 실행 사실에 대한 attestation이다. 실제 merge 권한은 GitHub Actions의
필수 checks가 결정한다.

---

## 4. 필수 checks

| Check | 역할 |
|---|---|
| `pr-policy` | Jira 키와 TDD 증거, 신뢰된 PR 정책 검사 |
| `guardrails` | workflow 권한, Action SHA, Dependabot 경계, 저장소 규칙 검사 |
| `helm` | Helm lint/render와 Kubernetes manifest 검증 |

모든 check는 최신 `main`과 합쳐진 PR head에서 성공해야 한다. check 이름을
변경하면 branch protection의 required context도 함께 변경하고 API로 readback한다.

CI 성공 후 auto-merge를 예약할 때는 PR head 변경 경쟁을 막기 위해 SHA를
바인딩한다.

```bash
HEAD_SHA=$(gh pr view <번호> --json headRefOid --jq .headRefOid)
gh pr merge <번호> --auto --squash --delete-branch \
  --match-head-commit "$HEAD_SHA"
```

---

## 5. 공급망 보안

### 외부 Actions

모든 `uses:`는 tag가 아니라 40자리 **full commit SHA**를 사용한다. 사람이 읽을 수
있도록 뒤에 원래 release tag를 주석으로 남긴다.

```yaml
- uses: actions/checkout@<40자리_SHA> # v7.0.1
```

다운로드한 실행 파일은 upstream checksum 파일로 검증한 뒤 실행한다. 단순 HTTPS
다운로드만으로 신뢰하지 않는다.

### 최소 권한

workflow별 top-level `permissions`는 테스트에서 정확히 고정한다.

| Workflow | 권한 |
|---|---|
| `validate.yaml` | `contents: read` |
| `pr-policy.yaml` | `contents: read` |
| `external-https-monitor.yaml` | `contents: write` |
| `dependabot-auto-merge.yaml` | `contents: write`, `pull-requests: write` |

새 workflow를 추가하거나 권한을 넓히면 `tests/test_repository_guardrails.py`의 명시적
기대값과 보안 근거도 함께 변경해야 한다.

### Secret

- 저장소는 public이다. token, webhook, kubeconfig, 평문 Secret을 commit하지 않는다.
- Kubernetes secret은 SealedSecret으로 관리한다.
- GitHub secret 값은 workflow 출력·PR 본문·Jira·문서에 남기지 않는다.
- push protection 차단을 우회하지 않는다.

---

## 6. Dependabot 자동 병합

Dependabot PR은 정확한 작성자 `dependabot[bot]`에 한해 사람용 Jira/TDD 본문을
면제한다. 다른 bot 이름이나 표시 이름은 면제하지 않는다.

쓰기 권한이 있는 auto-merge workflow는 다음 신뢰 경계를 지킨다.

1. 읽기 전용 `validate` workflow가 PR head를 검사한다.
2. 기본 브랜치의 `workflow_run`이 성공 결과를 받는다.
3. GitHub API로 PR을 다시 조회해 작성자가 `dependabot[bot]`인지 확인한다.
4. PR 코드를 checkout하거나 실행하지 않는다.
5. 검증된 `head_sha`를 `--match-head-commit`에 바인딩한다.
6. required checks 뒤에서 squash auto-merge를 예약한다.

이 구조는 Dependabot `pull_request` 실행의 read-only token을 억지로 확장하지 않고,
쓰기 권한 job이 candidate code를 실행하지 않게 한다.

---

## 7. 자동화 상태 브랜치

외부 HTTPS 모니터의 전이 상태는 protected `main`이 아니라 `monitor-state` 브랜치의
`.github/monitoring/external_https_state.json`에 저장한다.

- 상태가 의미 있게 변할 때만 commit
- `main` 직접 push 예외로 사용하지 않음
- 서비스/배포 구성의 진실 원천은 계속 `main`
- 상태 파일에는 credential을 저장하지 않음

---

## 8. 서비스 배포 계약

현재 서비스 저장소 CI는 미구현이다. 구현 전까지 infra image tag 변경은 운영자가
기능 브랜치에서 수행하고 PR로 반영한다.

향후 서비스 CI 자동화도 다음 조건을 만족해야 한다.

- 서비스 PR·CI에서 이미지를 빌드하고 `sha-<서비스 커밋>` 불변 tag 사용
- 최소 권한 token으로 infra 기능 브랜치만 push
- infra PR에 서비스 Jira 키, 이미지 SHA, 검증 증거 기록
- `infra/main` 직접 push 금지
- 필수 checks 성공 후 merge
- Argo CD sync와 실제 endpoint/health 검증을 완료 증거로 남김

자동화 identity와 credential 설계가 확정되기 전에는 동작하는 것처럼 문서화하지
않는다.

---

## 9. 롤백

배포 rollback도 protected main을 우회하지 않는다.

```bash
git fetch origin main
git switch main
git merge --ff-only origin/main
git switch -c revert/S15P11A705-123-bad-change
git revert <merge_commit>
git push -u origin HEAD
# PR 생성 → 필수 checks → merge
```

긴급 상황에서도 `kubectl rollout undo`나 main 강제 push를 영구 해결책으로 쓰지
않는다. Git의 원하는 상태를 고쳐 Argo CD와 실제 상태를 다시 일치시킨다.

---

## 10. 점검과 트러블슈팅

```bash
# 열린 PR과 checks
gh pr list --state open
gh pr checks <번호>

# 최근 workflow
gh run list --limit 10

# branch protection readback
gh api repos/Team-PinLog/infra/branches/main/protection

# workflow 목록
gh workflow list --all

# 로컬 회귀
python3 -m unittest discover -s tests -v
```

### PR이 BLOCKED일 때

1. `gh pr checks <번호>`로 실패 check를 확인한다.
2. PR 본문을 수정했다면 `pr-policy` 재실행을 기다린다.
3. branch가 최신 main보다 뒤처졌다면 main을 merge/rebase하고 checks를 다시 받는다.
4. 미해결 review conversation이 있는지 확인한다.
5. check를 우회하지 말고 실패 원인을 수정한다.

### Dependabot PR이 merge되지 않을 때

1. `validate`, `pr-policy` 결과를 확인한다.
2. `dependabot-auto-merge`의 `workflow_run` 실행을 확인한다.
3. PR head가 검증 후 변경됐는지 확인한다.
4. Action update가 full SHA 고정 규칙을 유지하는지 확인한다.

---

## 관련 문서

- [아키텍처](architecture.md)
- [운영 런북](runbook.md)
- [운영 알림](alerting.md)
- [저장소 README](../README.md)
