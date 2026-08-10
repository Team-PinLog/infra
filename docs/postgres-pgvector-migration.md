# PostgreSQL pgvector 전환 Runbook

이 작업은 Jira로 추적했다.

## 목적과 영향

`pinlog-prod/postgres`를 PostgreSQL 16 기반 pgvector 이미지로 전환해 Backend Flyway의
`CREATE EXTENSION IF NOT EXISTS vector`와 `VECTOR(1536)` 타입 요구를 충족한다.
PostgreSQL은 단일 replica이므로 이미지 전환 동안 DB 연결이 잠시 중단된다. 작업은
GitOps PR로만 수행하며 `kubectl set image`, `kubectl edit`, 수동 StatefulSet 삭제를
사용하지 않는다.

## 고정 이미지와 기준점

- 목표 이미지:
  `pgvector/pgvector:0.8.5-pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb`
- 전환 전 실제 실행 이미지:
  `postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777`
- PostgreSQL major version은 16으로 유지한다.
- PVC `data-postgres-0`, `PGDATA=/var/lib/postgresql/data/pgdata`,
  `local-path-retain`, 20Gi 계약을 바꾸지 않는다.

위 기존 digest는 2026-07-27 관측값이다. 실행 직전 Pod의 `imageID`를 다시 읽어 작업
기록에 남기고 다르면 새 기준값으로 검토한다.

## 승인 경계

다음은 사용자 승인 전 수행하지 않는다.

1. Infra 기능 브랜치 push와 PR 생성
2. PR merge와 Argo CD sync
3. PostgreSQL StatefulSet 재시작
4. `CREATE EXTENSION IF NOT EXISTS vector` 실행
5. `ALTER EXTENSION vector UPDATE` 실행
6. 백업 복원, DB 삭제, PVC 조작

## 1. 전환 직전 읽기 전용 점검

```bash
kubectl -n argocd get application postgres
kubectl -n pinlog-prod get statefulset postgres
kubectl -n pinlog-prod get pod postgres-0 -o wide
kubectl -n pinlog-prod get pod postgres-0 \
  -o jsonpath='declared={.spec.containers[0].image}{"\n"}imageID={.status.containerStatuses[0].imageID}{"\n"}restarts={.status.containerStatuses[0].restartCount}{"\n"}'
kubectl -n pinlog-prod get pvc data-postgres-0 postgres-backup
for claim in data-postgres-0 postgres-backup; do
  pv="$(kubectl -n pinlog-prod get pvc "$claim" -o jsonpath='{.spec.volumeName}')"
  kubectl get pv "$pv" \
    -o jsonpath='claim={.spec.claimRef.name}{" reclaimPolicy="}{.spec.persistentVolumeReclaimPolicy}{" phase="}{.status.phase}{"\n"}'
done
kubectl -n pinlog-prod get deployment redis \
  -o jsonpath='ready={.status.readyReplicas}/{.status.replicas}{"\n"}'
kubectl -n pinlog-prod exec postgres-0 -- \
  psql -U pinlog -d pinlog -Atqc \
  "select version(); select extname from pg_extension order by 1;"
```

중지 조건:

- Argo CD가 `Synced/Healthy`가 아님
- PostgreSQL 또는 Redis가 Ready가 아님
- PVC가 Bound가 아니거나 reclaim policy가 Retain이 아님
- 최근 OOM, probe timeout, 노드 압박이 있음
- Git 매니페스트와 실제 StatefulSet의 PVC/PGDATA 계약이 다름

### 초기 빈 DB 부트스트랩 예외

Backend와 Flyway를 한 번도 기동하지 않은 초기 환경에서는 archive의 `TOC 0`이 예상될
수 있다. 아래 읽기 전용 SQL로 사용자 객체 수를 먼저 확인한다.

```bash
kubectl -n pinlog-prod exec postgres-0 -- \
  psql -U pinlog -d pinlog -v ON_ERROR_STOP=1 -AtF '|' -c \
  "SELECT 'user_tables', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast';
   SELECT 'user_sequences', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S' AND n.nspname NOT IN ('pg_catalog','information_schema');
   SELECT 'user_views', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('v','m') AND n.nspname NOT IN ('pg_catalog','information_schema');"
```

다음 조건을 모두 만족할 때만 **초기 빈 DB 부트스트랩 예외**를 적용할 수 있다.

- 사용자 테이블 0개
- 사용자 시퀀스 0개
- 사용자 뷰 0개
- 새 archive가 custom format으로 정상 parse되지만 TOC 0임
- PVC·PGDATA·Secret 계약과 PostgreSQL major version 16이 유지됨
- 사용자가 빈 DB 상태와 복원할 사용자 데이터가 없음을 확인하고 전환을 명시적으로 승인함

사용자 객체가 하나라도 있는데 archive가 TOC 0이면 백업 실패로 간주하고 중지한다.
이 예외는 초기 이미지 전환만 허용하며 의미 있는 복원 테스트 완료로 기록하지 않는다.

## 2. 전환 직전 백업과 archive 검증

백업 Job 생성도 클러스터 mutation이므로 유지보수 승인 후 수행한다. 아래 블록은 같은
shell에서 실행하고 어느 명령이든 실패하면 중지한다.

```bash
set -euo pipefail
job="postgres-backup-pgvector-$(date +%Y%m%d%H%M%S)"
kubectl -n pinlog-prod create job --from=cronjob/postgres-backup "$job"
kubectl -n pinlog-prod wait --for=condition=complete "job/$job" --timeout=180s

job_log="$(kubectl -n pinlog-prod logs "job/$job")"
artifact_lines="$(printf '%s\n' "$job_log" | grep -E '^백업 시작: /backup/pinlog-[0-9]{8}-[0-9]{6}\.dump$')"
artifact_count="$(printf '%s\n' "$artifact_lines" | awk 'NF { n++ } END { print n+0 }')"
test "$artifact_count" -eq 1
artifact_container="${artifact_lines#백업 시작: }"
artifact_basename="${artifact_container##*/}"

backup_pv="$(kubectl -n pinlog-prod get pvc postgres-backup -o jsonpath='{.spec.volumeName}')"
backup_host_path="$(kubectl get pv "$backup_pv" -o jsonpath='{.spec.hostPath.path}')"
test -n "$backup_host_path"
dump="${backup_host_path%/}/$artifact_basename"
test ! -L "$dump"
test -f "$dump"
test -s "$dump"

job_started="$(kubectl -n pinlog-prod get job "$job" -o jsonpath='{.status.startTime}')"
job_started_epoch="$(date -d "$job_started" +%s)"
dump_mtime_epoch="$(stat -c %Y -- "$dump")"
dump_size_bytes="$(stat -c %s -- "$dump")"
test "$dump_mtime_epoch" -ge "$job_started_epoch"
test "$dump_size_bytes" -gt 0
kubectl -n pinlog-prod exec -i postgres-0 -- pg_restore --list < "$dump"
printf 'artifact=%s mtime=%s size=%s\n' \
  "$artifact_basename" "$dump_mtime_epoch" "$dump_size_bytes"
```

과거 dump나 수동 placeholder를 선택하지 않는다. Job 로그에서 유일한 생성 경로를 얻고,
backup PVC가 바인딩된 실제 PV hostPath 아래의 같은 basename만 검사한다.

파일 크기만으로 성공 처리하지 않는다. `pg_restore --list`가 성공하고 사용자 스키마
객체가 보여야 의미 있는 복원 증거다. 2026-07-27 기준 최신 archive는 811 bytes이고
**TOC 엔트리 0개**였다. Backend 최초 배포 전이라 사용자 테이블이 없었으므로 archive
형식은 유효하지만 의미 있는 복원 테스트 완료로 간주하지 않는다.

## 3. GitOps 전환

필수 CI와 리뷰를 통과한 Infra PR을 merge한 뒤에만 Argo CD 자동 sync를 허용한다.
전환 시작 시각과 기존 restart count를 기록하고 bounded wait를 사용한다.

```bash
kubectl -n argocd get application postgres
kubectl -n pinlog-prod rollout status statefulset/postgres --timeout=180s
kubectl -n pinlog-prod get pod postgres-0 -o wide
kubectl -n pinlog-prod logs postgres-0 --since=10m
kubectl -n pinlog-prod get events --sort-by=.lastTimestamp
```

180초 안에 Ready가 되지 않으면 같은 wait를 반복하지 않는다. Pod 상태, 이전 로그,
이벤트, PVC mount, image pull 오류를 먼저 조사하고 rollback 단계를 선택한다.

## 4. pgvector 검증

먼저 새 이미지가 제공하는 기본 버전과 기존 PVC의 설치 버전을 확인한다.

```bash
kubectl -n pinlog-prod exec postgres-0 -- \
  psql -U pinlog -d pinlog -Atqc \
  "select name, default_version, installed_version from pg_available_extensions where name='vector';"
```

기존 PVC에서 `installed_version`이 `default_version`보다 낮으면 이미지 교체만으로는
extension catalog가 갱신되지 않는다. fresh backup 검증과 별도 사용자 승인 후 다음
구문을 실행한다. 이 구문은 DB system catalog를 변경하므로 조회 결과가 이미 같으면
실행하지 않는다.

```bash
kubectl -n pinlog-prod exec postgres-0 -- \
  psql -U pinlog -d pinlog -v ON_ERROR_STOP=1 \
  -c "ALTER EXTENSION vector UPDATE;"
```

빈 DB에서 `installed_version`이 비어 있으면 Backend Flyway와 동일한 DB role의 확장
생성 권한을 별도 승인 후 검증한다.

```bash
kubectl -n pinlog-prod exec postgres-0 -- \
  psql -U pinlog -d pinlog -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

마지막으로 버전을 다시 조회한다. `installed_version = default_version`가 아니면 전환
완료로 처리하지 않고 Backend rollout을 중단한다.

```bash
kubectl -n pinlog-prod exec postgres-0 -- \
  psql -U pinlog -d pinlog -Atqc \
  "select name, default_version, installed_version from pg_available_extensions where name='vector';"
```

PostgreSQL과 Redis Ready, 기존 schema/table 목록, Argo CD 상태, 새 백업 Job 성공과
archive TOC도 다시 확인한다.

## 5. 단계별 rollback

### 확장 생성 전

목표 이미지가 시작하지 못했고 아직 `vector` extension을 생성하지 않았다면 단순
`git revert` 결과를 그대로 사용하지 않는다. 전환 전 manifest의 이미지 태그가
mutable이었기 때문이다. rollback 기능 브랜치에서 검증된 정적 artifact
`docs/rollback/postgres-before-pgvector.yaml`을
`platform/postgres/statefulset.yaml`에 복사하고 PR·CI로 적용한다.

```bash
cp docs/rollback/postgres-before-pgvector.yaml platform/postgres/statefulset.yaml
python3 -m unittest \
  tests.test_postgres_pgvector.PostgresPgvectorContractTest.test_static_rollback_manifest_changes_only_the_image -v
kubectl apply --dry-run=server \
  -f docs/rollback/postgres-before-pgvector.yaml
```

이 artifact는 기존 StatefulSet의 PVC·PGDATA·Secret·resources·probe 계약을 그대로
유지하며 `postgres` 컨테이너 이미지만
`postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777`로
고정한다. PR merge 후 Argo CD가 이 exact digest를 반영하고 StatefulSet Ready와 기존
데이터 목록을 확인한다. PVC는 삭제하거나 새로 만들지 않는다.

### 확장 생성 후

**확장 생성 후에는 stock PostgreSQL 이미지로 rollback하지 않는다.** 데이터베이스
catalog와 VECTOR 컬럼이 `vector` shared library를 요구할 수 있기 때문이다.

- Backend 배포 문제이면 DB 이미지는 pgvector로 유지하고 Backend image만 이전 digest로
  rollback한다.
- pgvector DB 이미지 자체에 문제가 있으면 호환되는 다른 PostgreSQL 16 + pgvector
  digest를 새 PR로 적용한다.
- stock PostgreSQL로 돌아가야 한다면 데이터 손실 범위와 유지보수 시간을 별도 승인받고,
  전환 전 검증된 backup을 격리된 복원 대상으로 먼저 복원·검증한다. 운영 PVC 위에서
  `DROP EXTENSION`, 데이터 디렉터리 삭제, 강제 초기화를 하지 않는다.

## 완료 증거

- Infra PR과 필수 CI 결과
- Argo CD `Synced/Healthy` revision
- 새 Pod imageID, Ready, restart delta
- `pg_available_extensions`와 설치된 `vector` 버전
- 전후 사용자 schema/table 확인
- 전환 직전·직후 archive의 `pg_restore --list` 결과
- PostgreSQL·Redis Ready 및 새 Warning event 부재
