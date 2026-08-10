# AI dev serving scaffold 운영 계약

이 문서는 dev-only AI serving의 승인된 운영 경계를 기록한다. 현재
`apps/dev/ai/values.yaml`은 `application.enabled: true`, `deployment.enabled: true`,
`bootstrap.enabled: true`이며 ApplicationSet이 Argo CD Application `ai-dev`를 생성하고
singleton Deployment, ClusterIP Service와 versioned PreSync bootstrap Job을 관리한다.

## 확정된 경계

- private image: `ghcr.io/team-pinlog/ai`; pull Secret reference: `ghcr-ai-pull`
- dev only, external ingress disabled, internal ClusterIP `8000`
- CPU-only singleton; request `100m/384Mi`, limit `500m/768Mi`, HPA disabled
- startup/liveness `/health`, readiness `/ready`; `terminationGracePeriodSeconds: 180`
- prod 전 metrics endpoint/ServiceMonitor 계약 보강
- runtime은 `ai-owner-secrets` 7 keys와 `ai-db-credentials`의 `DATABASE_URL` 1 key를
  순서대로 `envFrom`하며, 중복 key를 허용하지 않는다.
- 기존 PostgreSQL instance에 별도 `pinlog_dev` database와 role `pinlog_ai_dev` 사용;
  credential owner는 `김세민`
- Backend Flyway가 schema를 먼저 적용하고 AI versioned/idempotent preset bootstrap,
  그 다음 AI Deployment 순서
- Back migration 선행 계약은 `V1 → V2 → V3 → V100 → V101 → V102`; 상세 운영 절차는
  [AI dev Infra 선행조건](ai-dev-prerequisites.md)을 따른다.
- destination-side NetworkPolicy는 승인된 dev `ai`/`back` selector의 PostgreSQL TCP/5432만 허용
- 검증 순서: AI standalone smoke 후 dev Backend E2E
- image automation은 Infra PR만 만들고 trusted `workflow_run`이 필수 checks, exact PR head,
  source HEAD, provenance와 GHCR digest를 재검증한 뒤 승인 변수에 따라 squash merge한다.

## 완료된 activation 준비

- AI publish run `30428472911`의 provenance로 full commit SHA
  `299f6a6435f4f4c92cad59fa8eca4bacdf1e597e`와 private GHCR manifest digest를 검증해
  `sha256:a02cb48b84b1cb474d4cdaa7c9aa6a2e99f1162b16d324b396fcfcfcf0dae101`
  immutable image candidate를 반영했다.
- Backend source commit `cc7753c6a32e6fe12bee694b4ca8004c8a8a4cbc`, image digest
  `sha256:57e2845efd62e7ba5c857ff39d1d4d59974908c06c0885fcf6aa50870626a8a3`를 pin했다.
  full six migration 파일의 SHA-256은 이 source checkout에서 산출·보관해 checksum evidence로 쓴다.
- isolated-proven Flyway one-shot은 image 기존 entrypoint에
  `--spring.main.web-application-type=none`, `--spring.main.banner-mode=off`를 추가하며
  관측 결과는 `65.024s`, `exit 0`이다.
- `ghcr-ai-pull`은 namespace/name/key가 controller 인증서에 묶인 SealedSecret 암호문으로
  GitOps 관리한다. 평문 또는 복호화 출력은 Git·PR·CI 증거에 남기지 않는다.
- 준비 및 단계별 activation 검증을 완료해 세 workload gate를 모두 활성화했다.

## 공용 chart bootstrap 계약

`bootstrap.enabled=true`에는 DNS-label `bootstrap.version`과 non-empty
`bootstrap.command`가 모두 필요하다. Job은 Argo CD `PreSync` hook이고
`BeforeHookCreation`으로 이전 hook을 교체하므로 image 변경에도 immutable Job update로
막히지 않는다. 따라서 앱 bootstrap은 재실행 안전한 versioned/idempotent 동작이어야
한다. Job은 Deployment와 같은 immutable image, resource/security context,
`imagePullSecrets`, `env`/`envFrom`을 쓴다. 실패한 PreSync Job은 Deployment sync를
차단한다.

AI command는 `python -m app.bootstrap.load_presets`로 확정됐다. 27 presets의 full SHA-256
`204824bd37e6e1f056f1636ec1bb86d2585994a8cdbfd99bb188096cfca04034`에서 파생한 승인
version은 `preset-204824bd37e6`이다. 활성화는 application → bootstrap PreSync →
deployment gate 순서로 완료됐고 현재 `bootstrap.enabled: true`다.

## runtime secret과 updater source 계약

`ai-owner-secrets` 7-key schema와 `ai-db-credentials` 1-key schema의 합집합 및 embedding profile preflight는
[AI dev Infra 선행조건](ai-dev-prerequisites.md)에 고정한다. profile은 승인된
model/dimension/distance tuple과 exact match해야 한다. 이 변경에는 owner/DB ciphertext만 있고
실제 credential 평문은 없다.
승인 tuple은 `text-embedding-3-small` / `1536` / `cosine` /
`openai-text-embedding-3-small-1536-cosine-v1`이다. AI API base URL contract는 허용하지만
credential key 이름과 값은 기록하지 않는다.

updater required source settings는 `AI_SOURCE_BRANCH=main`,
`AI_SOURCE_WORKFLOW=ai-ci.yml`, `AI_PROVENANCE_ARTIFACT=ai-image-provenance`다.
기존 repository Secret `PINLOG_IMAGE_UPDATER_TOKEN`과 variable
`PINLOG_IMAGE_UPDATER_USERNAME`을 사용한다. status-only workflow probe에서 AI source와
provenance artifact read, private AI GHCR read, Infra draft PR create/close가 모두 성공했다.
별도 Secret 값 복제는 하지 않는다.

## 활성화된 automation 계약

1. image updater 승인: `AI_IMAGE_AUTOMATION_APPROVED=true`
2. image PR auto-merge 승인: `AI_IMAGE_AUTO_MERGE_APPROVED=true`
3. updater Jira key: `AI_INFRA_JIRA_KEY`에 Jira에서 추적한 자동화 작업 키를 설정
4. credential과 registry username: `PINLOG_IMAGE_UPDATER_TOKEN`,
   `PINLOG_IMAGE_UPDATER_USERNAME`
5. workflow의 repository 권한은 `contents: read`로 유지하고 PR merge 권한은 trusted
   auto-merge workflow의 `github.token`에만 선언한다.
6. 다음 runtime/DB 항목은 activation 당시 검증된 계약이며 credential 값을 기록하지 않는다.

운영 전제:

1. namespace `pinlog-dev`의 strict owner 암호문 `ai-owner-secrets` SealedSecret
   (`Team-PinLog/ai` owner workflow run `30431247125`, artifact
   `ai-owner-secrets-sealed`, 7 keys)
2. strict `DATABASE_URL` 1-key `ai-db-credentials`; 두 manifest 모두 ciphertext-only이며
   AI API endpoint/credential, DB password, shared secret의 실제 평문은 Infra 저장소에 없음
3. fresh backup/restore 가능성과 `pinlog_ai_dev` credential의 별도 live provisioning 증거
4. Backend Flyway full six-version checksum과 one-shot 재현 성공 증거
5. Backend Flyway 완료를 AI sync보다 선행시키는 cross-Application 신호/운영 절차
6. 이미지의 non-root UID는 검증된 `10001`을 사용하며 `/health` 호환성은 activation 전에 재확인

## activation 검증 순서

1. source workflow와 GHCR provenance를 확인하고 automation PR로 SHA/digest와 image
   provenance annotation만 갱신한다. workload gate는 활성 상태를 유지한다.
2. 승인된 담당자가 별도 절차로 SealedSecret을 만들고 GitOps PR에서 metadata/name/key
   contract만 검토한다. 평문이나 복호화 출력은 증거에 남기지 않는다.
3. fresh backup/restore 가능성, runtime Secret 존재, `pinlog_dev`/`pinlog_ai_dev` 준비와
   Backend Flyway 성공을 확인한다. live DB나 runtime Secret이 현재 존재한다고 간주하지 않는다.
4. `application.enabled: true`를 승인된 activation PR에서 열고, 확정된
   command/version으로 bootstrap을 켜 Helm render/kubeconform 및 PreSync Job 완료를
   확인한다.
5. Deployment를 켜고 rollout, startup/readiness/liveness, 종료 180초 경계를 확인한다.
6. 내부 port-forward로 AI standalone smoke를 통과시킨 뒤 dev Backend E2E를 수행한다.
7. prod 승격 전에 readiness 실패 의미, metrics, alert, 부하/메모리, 모델 cache/storage,
   GPU node scheduling 계약을 별도 승인한다.

## rollback

- bad image: 이전 SHA/digest로 Infra revert PR을 만들고 필수 checks 후 merge한다.
- bad preset: 새 bootstrap version에 보상/idempotent 동작을 제공하거나 Deployment를
  `deployment.enabled: false`로 되돌리는 PR을 사용한다. DB 수동 파괴나 live rollback은
  하지 않는다.
- health/rollout 실패: activation PR을 revert해 workload를 다시 차단한다. Secret 값,
  Kubernetes live object, Argo CD를 직접 mutation하지 않는다.
- rollback은 GitOps revert를 사용하며 destructive DB rollback은 하지 않는다.
  bootstrap 데이터는 단순 image revert만으로 복원되지 않는다. 새 version의 idempotent 보상
  또는 승인된 forward data repair가 필요하다.
