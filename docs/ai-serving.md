# AI dev serving scaffold 운영 계약

이 문서는 최초 dev-only AI serving의 승인된 경계와 아직 승인되지 않아 배포를
차단한 항목을 구분한다. 현재 scaffold는 `apps/dev/ai/values.yaml`의
`application.enabled: false`, `deployment.enabled: false`,
`bootstrap.enabled: false`로 chart workload 리소스를 하나도 렌더링하지 않는다.
ApplicationSet은 `apps/dev/ai` 디렉터리를 발견해 Argo CD Application `ai-dev` 1개를
생성할 수 있으며, 이는 승인된 예외다. 해당 Application의 Helm render 결과는 gate가
닫힌 동안 0개다. 기존 서비스는 chart 기본 `application.enabled: true`를 상속한다.

## 확정된 경계

- private image: `ghcr.io/team-pinlog/ai`; pull Secret reference: `ghcr-ai-pull`
- dev only, external ingress disabled, internal ClusterIP `8000`
- CPU-only singleton; request `100m/384Mi`, limit `500m/768Mi`, HPA disabled
- startup/readiness/liveness `/health`; `terminationGracePeriodSeconds: 180`
- prod 전 실제 readiness 의미 분리와 metrics endpoint/ServiceMonitor 계약 보강
- runtime secret은 `ai-runtime-secrets` SealedSecret reference만 사용
- 기존 PostgreSQL instance에 별도 `pinlog_dev` database와 role `pinlog_ai_dev` 사용;
  credential owner는 `김세민`
- Backend Flyway가 schema를 먼저 적용하고 AI versioned/idempotent preset bootstrap,
  그 다음 AI Deployment 순서
- Back migration 선행 계약은 `V1 → V2 → V3 → V100 → V101 → V102`; 상세 운영 절차는
  [AI dev Infra 선행조건](ai-dev-prerequisites.md)을 따른다.
- destination-side NetworkPolicy는 승인된 dev `ai`/`back` selector의 PostgreSQL TCP/5432만 허용
- 검증 순서: AI standalone smoke 후 dev Backend E2E
- image automation은 Infra PR만 만들며 초기 수동 merge. live 변경과 merge는 하지 않음

## 완료된 activation 준비

- AI publish run `30330670901`의 provenance로 full commit SHA
  `1ed55b817197de73e63618a3a61696da7e14b5bc`와 private GHCR manifest digest를 검증해
  `sha256:e3e27a2475eef99287d7eca013acd9bc69969a37bfc3771a03aec80d95eed592`
  immutable image candidate를 반영했다.
- Backend source commit `cc7753c6a32e6fe12bee694b4ca8004c8a8a4cbc`, image digest
  `sha256:57e2845efd62e7ba5c857ff39d1d4d59974908c06c0885fcf6aa50870626a8a3`를 pin했다.
  full six migration 파일의 SHA-256은 이 source checkout에서 산출·보관해 checksum evidence로 쓴다.
- isolated-proven Flyway one-shot은 image 기존 entrypoint에
  `--spring.main.web-application-type=none`, `--spring.main.banner-mode=off`를 추가하며
  관측 결과는 `65.024s`, `exit 0`이다.
- `ghcr-ai-pull`은 namespace/name/key가 controller 인증서에 묶인 SealedSecret 암호문으로
  GitOps 관리한다. 평문 또는 복호화 출력은 Git·PR·CI 증거에 남기지 않는다.
- 위 준비가 완료돼도 `application.enabled: false`, `deployment.enabled: false`,
  `bootstrap.enabled: false`를 유지하며 workload를 생성하지 않는다.

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
version은 `preset-204824bd37e6`이다. 값을 고정하되 `bootstrap.enabled: false`를 유지한다.
활성화는 application → bootstrap PreSync → deployment gate 순서이며 현재 세 gate 모두 false다.

## runtime secret과 updater source 계약

`ai-runtime-secrets` required key schema와 embedding profile preflight는
[AI dev Infra 선행조건](ai-dev-prerequisites.md)에 고정한다. profile은 승인된
model/dimension/distance tuple과 exact match해야 하며 실제 값/ciphertext는 이 변경에 없다.
승인 tuple은 `text-embedding-3-small` / `1536` / `cosine` /
`openai-text-embedding-3-small-1536-cosine-v1`이다. GMS base URL contract는 허용하지만
key는 기록하지 않는다.

updater required source settings는 `AI_SOURCE_BRANCH=main`,
`AI_SOURCE_WORKFLOW=ai-ci.yml`, `AI_PROVENANCE_ARTIFACT=ai-image-provenance`다.
credential names는 기존 계약을 유지하고 실제 GitHub Settings/Secrets는 변경하지 않는다.

## 명시적 미완료 gate

다음 값은 팀 답변이나 승인된 artifact 없이 추측하지 않는다. 모두 닫히기 전
`application.enabled: false`, `deployment.enabled: false`,
`bootstrap.enabled: false`를 유지한다.

1. image updater 활성 승인: `AI_IMAGE_AUTOMATION_APPROVED=true`
2. updater PR용 실제 Jira key: `AI_INFRA_JIRA_KEY`
3. source Actions/GHCR read-only와 Infra PR write를 분리한 repo-scoped credentials 및
   registry username: `PINLOG_AI_SOURCE_READER_TOKEN`, `PINLOG_AI_INFRA_PR_TOKEN`,
   `PINLOG_AI_IMAGE_UPDATER_USERNAME`
4. namespace `pinlog-dev`의 실제 runtime 암호문 `ai-runtime-secrets` SealedSecret
5. GMS endpoint/key, DB password, shared secret의 실제 credential 값
6. fresh backup/restore 가능성과 `pinlog_ai_dev` credential의 별도 live provisioning 증거
7. Backend Flyway full six-version checksum과 one-shot 재현 성공 증거
8. Backend Flyway 완료를 AI sync보다 선행시키는 cross-Application 신호/운영 절차
9. 이미지의 non-root UID는 검증된 `10001`을 사용하며 `/health` 호환성은 activation 전에 재확인

## activation 검증 순서

1. source workflow와 GHCR provenance를 확인하고 automation PR로 SHA/digest만 갱신한다.
   이 단계에서도 workload는 disabled다.
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

- bad image: 이전 SHA/digest로 Infra revert PR을 만들고 필수 checks 후 수동 merge한다.
- bad preset: 새 bootstrap version에 보상/idempotent 동작을 제공하거나 Deployment를
  `deployment.enabled: false`로 되돌리는 PR을 사용한다. DB 수동 파괴나 live rollback은
  하지 않는다.
- health/rollout 실패: activation PR을 revert해 workload를 다시 차단한다. Secret 값,
  Kubernetes live object, Argo CD를 직접 mutation하지 않는다.
- rollback은 GitOps revert를 사용하며 destructive DB rollback은 하지 않는다.
  bootstrap 데이터는 단순 image revert만으로 복원되지 않는다. 새 version의 idempotent 보상
  또는 승인된 forward data repair가 필요하다.
