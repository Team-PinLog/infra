# AI shared database 복구 runbook

정본 계약은 Backend와 AI가 동일 PostgreSQL 인스턴스의 같은 database `pinlog`를 사용하고,
`core`/`ai` schema로 경계를 나누는 것이다. 기존 AI role/user와 password를 유지하고 AI에는
`ai` DML만 부여하며 core 접근 거부를 검증한다.

## 변경 경계

- `postgres-ai-shared-db-v1` GitOps Job은 기존 `pinlog_ai_dev` LOGIN role의 속성과 무소속을
  fail-closed로 검사하고 `pinlog.ai` 권한만 부여한다.
- SQL은 단일 transaction이며 ConfigMap은 immutable이다. 수정 재실행이 필요하면 기존 완료
  Job을 덮어쓰지 않고 새 SQL은 v2 ConfigMap/Job 이름으로 별도 PR을 만든다.
- password를 만들거나 바꾸지 않는다. 기존 `pinlog_dev` database와 `ai-db-credentials`를 삭제하지 않는다.
- DB 데이터 수정·backfill·down migration은 하지 않는다.
- Backend Flyway의 `V100__ai_tables.sql`, `V101__ai_indexes.sql` 존재와 성공 row를 별도 검증한다.
- 기존 AI bootstrap의 27 presets artifact와 완료 Job은 보존한다.

## DATABASE_URL ciphertext-only handoff

현재 SealedSecret ciphertext에서 URL의 database 부분만 안전하게 복호화·재봉인할 수 없으므로
Infra가 암호문을 추측하거나 live Secret 값을 추출하지 않는다. credential owner는 기존
`pinlog_ai_dev` 사용자/password는 그대로 둔 채 `DATABASE_URL`의 database만 `pinlog`로 바꾸고,
승인된 Sealed Secrets 공개키로 `pinlog-dev/ai-db-credentials`의 strict one-key
ciphertext-only artifact를 생성한다. 값이나 URL은 PR, Jira, 로그에 쓰지 않는다.

owner가 전달할 비민감 증거:

1. 대상 Secret/namespace와 key 이름 (`ai-db-credentials`, `pinlog-dev`, `DATABASE_URL`)
2. 기존 user/password 유지 및 database component만 변경했다는 확인
3. sealing certificate fingerprint, source SHA/run ID, encryptedData key 이름
4. ciphertext-only Infra PR URL

Infra는 artifact가 기존 파일만 갱신하고 Secret 삭제가 없음을 검증한 뒤 merge한다. 그 후 Argo
`secrets-dev` sync, AI rollout, sanitized DB identity, `/health`와 `/ready`를 확인한다.

## 검증

- AI sanitized identity의 host/port/database가 Backend와 같고 user는 별도인지 확인한다.
- `ai` schema USAGE/DML 허용, CREATE 거부, `core` schema/table 권한 거부를 catalog와 실제
  read-only transaction probe로 확인한다.
- Flyway history에 V100/V101 성공이 있고 bootstrap count가 27인지 읽기 전용으로 확인한다.
- 신규 synthetic context는 표준 Backend 경로로 생성해 최초 PENDING과 후속 state 전이를 검증한다.
  인증 credential handoff가 별도 blocker이면 데이터 생성 없이 health/readiness와 기존 row의
  상태 분포만 확인한다.

## rollback

권한 bootstrap은 데이터 비파괴이므로 장애 시 먼저 AI Deployment를 이전 Secret revision으로
되돌리는 GitOps revert를 적용한다. 필요하면 후속 승인 SQL로 `pinlog_ai_dev`의 `ai` grant만
회수한다. 기존 별도 DB/Secret은 보존하며 삭제·backfill·down migration을 하지 않는다.
Secret rollback은 직전 ciphertext GitOps revert이며 password 회전은 credential owner만 수행한다.
