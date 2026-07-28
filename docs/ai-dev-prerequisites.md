# AI dev Infra 선행조건 운영 runbook

이 절차는 GitOps activation 전에 운영자가 수행할 준비/검증 계약이다. 이 저장소는
비밀값, SealedSecret ciphertext, Backend migration 본문을 만들지 않는다. 현재
`application.enabled: false`, `bootstrap.enabled: false`, `deployment.enabled: false`이며
이 문서는 어떤 gate도 열지 않는다. 모든 명령은 승인된 변경창에서 운영자가 직접
실행하며, 이 변경에서는 live 실행하지 않는다.

## 0. 승인 입력과 fail-closed 경계

시작 전 다음을 승인 증거와 함께 확보한다.

- `pinlog_dev` 전용 DB role `pinlog_ai_dev`; credential owner `김세민`
- Back source commit `cc7753c6a32e6fe12bee694b4ca8004c8a8a4cbc`와 image digest
  `sha256:57e2845efd62e7ba5c857ff39d1d4d59974908c06c0885fcf6aa50870626a8a3`
- 승인 profile: `text-embedding-3-small` / `1536` / `cosine` /
  `openai-text-embedding-3-small-1536-cosine-v1`
- `ai-runtime-secrets`의 실제 암호문과 credential rotation/회수 책임자

누락 시 중단한다. DB password를 shell argument, SQL, Git, 로그에 넣지 않는다.
`ops/ai-dev-prerequisites/bootstrap-pinlog-dev.sql`은 role을 `NOLOGIN`으로 만들며 password를
취급하지 않는다. credential owner가 승인된 별도 채널에서 LOGIN/password와
`DATABASE_URL`을 함께 설정하기 전에는 앱 연결이 불가능한 것이 정상이다.

## 1. DB/role/pgvector 준비

먼저 fresh backup과 restore 가능성, PostgreSQL Ready, 현재 DB/role 충돌 부재를
읽기 전용으로 확인한다. 기존 pgvector 전환 절차는
`docs/postgres-pgvector-migration.md`를 따른다. 승인된 role은 비밀값이 아니지만 임의로
발명하지 않는다.

```bash
set -euo pipefail
DEV_ROLE=pinlog_ai_dev
test "$DEV_ROLE" = pinlog_ai_dev

git diff --exit-code -- ops/ai-dev-prerequisites/bootstrap-pinlog-dev.sql
kubectl -n pinlog-prod exec -i statefulset/postgres -- \
  psql -X -v ON_ERROR_STOP=1 -U pinlog -d postgres -v dev_role="$DEV_ROLE" \
  < ops/ai-dev-prerequisites/bootstrap-pinlog-dev.sql
```

SQL은 identifier를 제한하고 `%I`로 quote하며 다음을 idempotent하게 수행한다.

1. 승인 role이 없으면 `NOLOGIN`, non-superuser로 생성
2. `pinlog_dev`가 없으면 해당 role owner로 생성; 다른 owner의 동명 DB면 실패
3. PUBLIC connect/create를 회수하고 전용 role에 DB/schema 최소 grant
4. `pinlog_dev`에서 `CREATE EXTENSION IF NOT EXISTS vector`

SQL 성공 후 credential owner가 승인된 비밀 관리 절차로 role LOGIN/password를 설정한다.
이 저장소에는 그 명령을 두지 않는다. plaintext password나 credential 값을 검증 증거로
남기지 않는다.

## 2. Back Flyway 선행순서

Back artifact와 migration 파일을 이 저장소가 복제하지 않는다. 승인된 Back checkout에서
다음 offline 검증을 먼저 실행한다.

```bash
python3 tools/validate_ai_dev_prerequisites.py flyway-files \
  /absolute/path/to/approved/back/src/main/resources/db/migration
```

validator는 승인된 Backend commit의 `exact pinned source set`인 다음 여섯 versioned SQL
파일만 각각 하나씩 있고 다른 `V*__*.sql`이 없을 때만 numeric order를 반환한다:
`V1__create_schemas.sql`, `V2__member.sql`, `V3__core_domain.sql`,
`V100__ai_tables.sql`, `V101__ai_indexes.sql`, `V102__feed_event.sql` — 즉
`V1 → V2 → V3 → V100 → V101 → V102`다. 승인된 Back source commit checkout에서
파일별 SHA-256을 산출해 source pin과 함께 실행 증거에 보관하고, 위 image digest와 대조한다.

canonical isolated-proven Flyway one-shot은 승인 Backend image의 기존 entrypoint에
`--spring.main.web-application-type=none`와 `--spring.main.banner-mode=off`를 추가한 실행이며,
관측 결과는 `65.024s`, `exit 0`이다. 이를 `pinlog_ai_dev` role로 재현 검증한다. Flyway
성공 전 AI Application과 bootstrap, Deployment gate는 열지 않는다.

실행 후 값 자체를 출력하지 않는 DB 검증을 수행한다.

```sql
SELECT extname FROM pg_extension WHERE extname = 'vector';
SELECT version, success, installed_rank
FROM flyway_schema_history
WHERE version IS NOT NULL
ORDER BY installed_rank;
```

이 쿼리는 allowlist로 숨기지 않고 모든 versioned Flyway row를 반환해야 한다. 결과가
정확히 여섯 row이고 version 순서가 `1, 2, 3, 100, 101, 102`, 모든 row가
`success=true`, installed rank 순서가 `V1 → V2 → V3 → V100 → V101 → V102`여야 한다.
누락, extra, duplicate, failed row 또는 순서 불일치면 중단한다.

## 3. ai-runtime-secrets key/profile preflight

필수 key는 다음 8개이며 실제 값은 문서/CI에 두지 않는다.

- `DATABASE_URL`
- `GMS_API_KEY`
- `GMS_BASE_URL`
- `PINLOG_EMBEDDING_MODEL`
- `PINLOG_EMBEDDING_DIMENSION`
- `PINLOG_EMBEDDING_DISTANCE`
- `PINLOG_EMBEDDING_PROFILE`
- `INTERNAL_SHARED_SECRET`

SealedSecret 생성은 credential owner의 별도 GitOps PR이다. ciphertext가 없으므로 이
변경은 `secrets/dev/ai-runtime-secrets.sealedsecret.yaml`을 만들지 않는다. controller가
Secret을 만든 뒤 key 이름만 제한된 파일로 받아 다음을 실행한다.

```bash
python3 tools/validate_ai_dev_prerequisites.py secret-keys /secure/path/runtime-key-names
```

exact 8-key schema가 아니면 실패한다. 값 preflight는 mode 0600 ephemeral env 파일과
AI 팀이 승인한 JSON profile contract를 사용한다. validator는 값을 출력하지 않는다.

```bash
umask 077
python3 tools/validate_ai_dev_prerequisites.py profile \
  /secure/ephemeral/ai-runtime.env /secure/approved/profile-contract.json
rm -f /secure/ephemeral/ai-runtime.env
```

`PINLOG_EMBEDDING_PROFILE`이 승인 목록에 없거나 model/dimension/distance 중 하나라도
exact match하지 않으면 실패한다. 승인 profile contract가 없으면 이 단계를 생략하지
말고 activation을 중단한다. 승인 exact tuple은 `text-embedding-3-small` / `1536` /
`cosine` / `openai-text-embedding-3-small-1536-cosine-v1`이다. `GMS_BASE_URL`은 runtime
계약에 포함할 수 있지만 GMS key나 값은 이 문서와 저장소에 기록하지 않는다.

## 4. application/bootstrap/deployment gate

확정된 bootstrap command는 `python -m app.bootstrap.load_presets`다. 승인 version은
`preset-204824bd37e6`이며 27 presets의 full preset SHA-256
`204824bd37e6e1f056f1636ec1bb86d2585994a8cdbfd99bb188096cfca04034`에서 파생됐다.
계약을 기록하되 `bootstrap.enabled: false`를 유지한다.

활성화 순서는 별도 승인 PR에서만 다음과 같다.

1. fresh backup/restore 가능성, runtime Secret provisioning, DB와 Back Flyway 성공 검증 후
   `application.enabled` gate 검토
2. 승인된 `bootstrap.version`과 고정 command로 bootstrap PreSync Job 완료
3. Job 성공 증거 후 `deployment.enabled` gate 검토 및 rollout

즉 application → bootstrap PreSync → deployment gate다. 현재 세 gate는 모두 false다.

## 5. updater Settings/source 계약

실제 GitHub Settings는 이 절차에서 변경하지 않는다. required names와 값은 다음이다.

- Variable `AI_SOURCE_BRANCH=main`
- Variable `AI_SOURCE_WORKFLOW=ai-ci.yml`
- Variable `AI_PROVENANCE_ARTIFACT=ai-image-provenance`
- Variable `AI_IMAGE_AUTOMATION_APPROVED=false`
- Variable `AI_INFRA_JIRA_KEY` (승인된 실제 Jira key 필요)
- Variable `PINLOG_AI_IMAGE_UPDATER_USERNAME`
- Secret `PINLOG_AI_SOURCE_READER_TOKEN`
- Secret `PINLOG_AI_INFRA_PR_TOKEN`

workflow는 source 세 값이 exact match하지 않으면 fail closed한다. credential scopes와 값은
별도 승인이며 출력하지 않는다. `AI_IMAGE_AUTOMATION_APPROVED`와 application/bootstrap/
deployment gates는 계속 false다.

## 6. rollback

배포/NetworkPolicy/image 변경은 새 live mutation이 아니라 검증된 GitOps revert PR로
되돌린다. destructive DB rollback 금지이며 down migration이나 수동 데이터 삭제를 하지
않는다. Flyway/extension 변경은 forward-only 보상 migration을 Back/DB owner와 승인한다.

bootstrap 데이터는 단순 image revert로 복원되지 않는다. bad preset은 새 version의
idempotent 보상 bootstrap 또는 승인된 forward data repair가 필요하다. 먼저
`deployment.enabled: false`로 차단하는 GitOps revert를 검토하고 DB 증거를 보존한다.
