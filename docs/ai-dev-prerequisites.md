# AI dev Infra 선행조건 운영 runbook

이 절차는 GitOps activation 전에 운영자가 수행할 준비/검증 계약이다. 이 저장소는
비밀값, SealedSecret ciphertext, Backend migration 본문을 만들지 않는다. 현재
`application.enabled: false`, `bootstrap.enabled: false`, `deployment.enabled: false`이며
이 문서는 어떤 gate도 열지 않는다. 모든 명령은 승인된 변경창에서 운영자가 직접
실행하며, 이 변경에서는 live 실행하지 않는다.

## 0. 승인 입력과 fail-closed 경계

시작 전 다음을 승인 증거와 함께 확보한다.

- `pinlog_dev` 전용 role 식별자와 credential owner
- Back 저장소의 승인된 migration 디렉터리 및 V1/V100/V101 commit SHA
- AI 팀이 승인한 profile → model/dimension/distance JSON 계약
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
read -r -p 'Approved pinlog_dev role: ' DEV_ROLE
test -n "$DEV_ROLE"

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

validator는 unique V1, V100, V101이 모두 있을 때만 `V1 → V100 → V101` 순서를 반환한다.
그 다음 credential owner가 제공한 일회성 실행 환경에서 Back의 canonical Flyway command를
`pinlog_dev` 전용 role로 실행한다. 정확한 Back command/image와 migration checksum은
Back 팀 승인 입력이며 Infra가 추측하지 않는다. Flyway 성공 전 AI Application과 bootstrap,
Deployment gate는 열지 않는다.

실행 후 값 자체를 출력하지 않는 DB 검증을 수행한다.

```sql
SELECT extname FROM pg_extension WHERE extname = 'vector';
SELECT version, success, installed_rank
FROM flyway_schema_history
WHERE version IN ('1', '100', '101')
ORDER BY installed_rank;
```

결과는 정확히 세 version이 모두 `success=true`이고 installed rank가
`V1 → V100 → V101`이어야 한다. 누락, duplicate, failed row 또는 순서 불일치면 중단한다.

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
말고 activation을 중단한다.

## 4. application/bootstrap/deployment gate

확정된 bootstrap command는 `python -m app.bootstrap.load_presets`다. 그러나
`bootstrap.version` 규칙은 미확정이므로 빈 문자열을 유지한다. chart는 version 없이
bootstrap을 enable하면 render를 실패시킨다.

활성화 순서는 별도 승인 PR에서만 다음과 같다.

1. DB와 Back Flyway 검증 후 `application.enabled` gate 검토
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
