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
- 기존 PostgreSQL instance에 별도 `pinlog_dev` database/user 사용
- Backend Flyway가 schema를 먼저 적용하고 AI versioned/idempotent preset bootstrap,
  그 다음 AI Deployment 순서
- 검증 순서: AI standalone smoke 후 dev Backend E2E
- image automation은 Infra PR만 만들며 초기 수동 merge. live 변경과 merge는 하지 않음

## 완료된 activation 준비

- AI publish run `30330670901`의 provenance로 full commit SHA
  `1ed55b817197de73e63618a3a61696da7e14b5bc`와 private GHCR manifest digest를 검증해
  immutable image candidate를 반영했다.
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

## 명시적 미완료 gate

다음 값은 팀 답변이나 승인된 artifact 없이 추측하지 않는다. 모두 닫히기 전
`application.enabled: false`, `deployment.enabled: false`,
`bootstrap.enabled: false`를 유지한다.

1. `Team-PinLog/ai`의 배포 대상 branch, successful publish workflow filename, 해당
   run이 source SHA와 image digest를 묶어 내는 `provenance.json` artifact 이름:
   repository variables `AI_SOURCE_BRANCH`, `AI_SOURCE_WORKFLOW`,
   `AI_PROVENANCE_ARTIFACT`
2. image updater 활성 승인: `AI_IMAGE_AUTOMATION_APPROVED=true`
3. updater PR용 실제 Jira key: `AI_INFRA_JIRA_KEY`
4. source Actions/GHCR read-only와 Infra PR write를 분리한 repo-scoped credentials 및
   registry username: `PINLOG_AI_SOURCE_READER_TOKEN`, `PINLOG_AI_INFRA_PR_TOKEN`,
   `PINLOG_AI_IMAGE_UPDATER_USERNAME`
5. namespace `pinlog-dev`의 실제 runtime 암호문 `ai-runtime-secrets` SealedSecret
6. GMS endpoint/key, DB password, shared secret의 정확한 앱 환경변수 계약
7. 기존 PostgreSQL instance에 `pinlog_dev` database와 최소권한 전용 user를 누가,
   어떤 승인된 migration으로 생성/회수할지
8. `pinlog-dev` AI/Backend에서 `pinlog-prod` PostgreSQL 5432로 가는 현재
   default-deny ingress를 어떤 podSelector로 열지에 대한 최소 NetworkPolicy 계약
9. Backend Flyway 완료를 AI sync보다 선행시키는 cross-Application 신호/운영 절차
10. AI preset bootstrap의 실제 command와 version 규칙. 이미지의 non-root UID는
    검증된 `10001`을 사용하며 `/health` 호환성은 activation 전에 다시 확인

## activation 검증 순서

1. source workflow와 GHCR provenance를 확인하고 automation PR로 SHA/digest만 갱신한다.
   이 단계에서도 workload는 disabled다.
2. 승인된 담당자가 별도 절차로 SealedSecret을 만들고 GitOps PR에서 metadata/name/key
   contract만 검토한다. 평문이나 복호화 출력은 증거에 남기지 않는다.
3. `pinlog_dev` database/user 생성과 Backend Flyway 성공을 확인한다.
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
