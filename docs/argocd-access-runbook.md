# Argo CD access and credential runbook

이 문서는 credential **값을 출력하지 않고** Argo CD 접근·인증 변경을 검토하고 검증하기 위한 운영 계약이다. 터미널 기록, PR, 채팅, 티켓, 스크린샷에는 사용자명·비밀번호·토큰·쿠키·Secret 이름·조회 명령·복호화 결과를 남기지 않는다. 실제 작업은 승인된 비밀 관리 채널과 감사를 지원하는 운영 환경에서만 수행한다.

## 현재 상태와 변경 금지선

- 기준 상태는 `admin.enabled=true`, SSO 없음(`dex.enabled=false`)이다. 이 PR은 그 상태를 명시할 뿐 account, RBAC, SSO, Secret 또는 live cluster를 변경하지 않는다.
- 별도 변경 승인, 소유자 지정, 테스트·rollback 계획, maintenance window가 모두 준비되기 전에는 **승인 전 적용 금지**다.
- bootstrap 실행 결과에도 credential 값, Secret 이름, 조회·decode 절차를 표시하지 않는다.

## Account, RBAC, SSO 선택지

1. 현 상태 유지: 제한된 운영자만 로컬 admin을 사용한다. 최소 변경이지만 공유 계정의 귀속성과 장기 감사성이 약하다.
2. 로컬 named account + RBAC: 개인별 계정과 최소 권한 role을 정의한다. SSO 장애와 독립적이지만 lifecycle과 credential rotation을 직접 운영해야 한다.
3. SSO + RBAC: IdP의 불변 식별자와 그룹을 Argo CD role에 매핑하고 일반 운영은 SSO로 전환한다. IdP 가용성, group claim, 탈퇴자 회수, 과권한 매핑을 사전 검증한다.
4. 권장 목표: SSO named identity + 최소 권한 RBAC를 일상 경로로 하고, 로컬 admin은 봉인된 break-glass 경로로만 유지한다. 실제 선택과 전환은 별도 승인 PR에서 한다.

각 안은 read-only, deploy operator, administrator 역할을 분리하고 AppProject·Application 범위를 명시한다. deny-by-default에서 필요한 action만 추가하며, 익명 접근과 팀 공용 토큰은 허용하지 않는다.

## Credential owner와 storage

- 서비스 오너와 보안 오너를 서로 다른 두 사람으로 지정하고 생성·사용·회수 승인자를 기록한다.
- credential은 승인된 비밀 저장소에만 보관하고 저장소의 접근 감사, version history, 복구 정책을 사용한다. Git, 로컬 shell history, CI log, 문서에는 저장하지 않는다.
- 운영자는 값 자체가 아니라 owner, 생성 시각, 만료/rotation 기한, 마지막 검증 시각, 상태 식별자만 기록한다.
- 인수인계와 퇴사 시 접근권한을 즉시 재평가하고 개인 credential은 폐기한다.

## Rotation

1. change owner, reviewer, window, 영향 Application 목록과 rollback 기준을 승인받는다.
2. 새 credential을 승인된 저장소에 새 version으로 준비하되 기존 version은 즉시 제거하지 않는다.
3. 격리된 세션에서 새 credential로 UI/CLI와 최소 권한을 검증한다. 출력은 성공/실패와 시간만 남긴다.
4. 기존 credential을 폐기하고 활성 세션·토큰을 회수한다.
5. 전체 Application health와 sync 상태를 재검증한 뒤 metadata만 감사 기록에 남긴다.

정기 주기는 조직 정책을 따르며 인력 변경, 노출 의심, IdP/RBAC 변경 시 즉시 비정기 rotation을 수행한다.

## Break-glass recovery

- 조건: SSO 또는 정상 운영 계정 장애로 승인된 복구가 불가능하고 서비스 영향이 확인된 경우에만 사용한다.
- incident commander와 보안 오너의 이중 승인을 받고, 격리된 관리 단말·승인된 네트워크 경로·비밀 저장소의 복구 절차를 사용한다.
- 화면 공유·명령 기록·debug 출력에서 값을 가리고, 값이나 조회 절차 대신 승인자·시각·목적·결과만 기록한다.
- 복구 후 즉시 credential을 rotation하고 임시 권한과 세션을 회수하며 전체 Application health를 확인한다.

## Rollback

- 변경 전 auth/RBAC 설정의 Git revision, IdP 설정 metadata, 접근 가능한 break-glass 경로와 담당자를 확인한다.
- 실패 조건은 UI/CLI 로그인 불가, 의도한 role 오매핑, 기존 허용 작업 거부, 예상 밖 권한 허용, Application health 악화다.
- 조건 발생 시 배포를 중단하고 승인된 이전 Git revision과 이전 IdP/RBAC 설정으로 복귀한다. credential 값은 Git rollback 대상이 아니며 비밀 저장소 version 정책으로 별도 복구한다.
- rollback 후 UI/CLI, 권한 matrix, 전체 Application health를 다시 검증하고 원인 분석 전 재시도하지 않는다.

## Maintenance

- 저위험 window, incident 연락망, 변경/검증/rollback 담당자, 동결 시간과 최대 중단 시간을 공지한다.
- 변경 중 신규 sync·배포를 제한하고 진행 중 operation 유무를 확인한다.
- 관측 항목은 API 오류율, login 실패, controller/repo-server 상태, Application health·sync 변화이며 값·token은 수집하지 않는다.
- 종료 조건은 UI/CLI와 권한 matrix가 기대대로 동작하고 모든 Application의 상태를 설명할 수 있는 것이다.

## UI/CLI 재검증

- UI: 승인된 네트워크 경로에서 로그인 성공, 대상 프로젝트만 표시, 허용 action 성공, 금지 action 거부, logout 후 session 무효화를 확인한다.
- CLI: password/token을 argv나 history에 넣지 않는 승인된 입력 방식으로 login과 account 확인을 수행한다. 최소 read 작업과 승인된 sync dry-run/preview, 금지 작업 거부를 확인한다.
- 각 role과 break-glass 경로를 별도 세션에서 검증한다. 기록에는 actor role, 시각, pass/fail, 오류 분류만 남기고 credential 값은 남기지 않는다.

## 전체 Application health 검증

- 변경 전후 모든 namespace의 전체 Application 목록을 기계 판독 가능한 출력으로 수집하되 manifest, parameter, Secret data는 제외한다.
- 각 Application의 Health, Sync, operation 진행 여부를 비교하고 `Healthy/Synced`가 아닌 항목은 기존 상태인지 변경 영향인지 분류한다.
- root app과 child app 수가 기준과 일치하고 누락·추가가 없는지 확인한다.
- Degraded, Missing, Unknown, OutOfSync 또는 장시간 Progressing이 새로 발생하면 rollback gate로 처리한다.
- 증거에는 총 개수와 상태별 개수, 실패한 Application 식별자, 시간만 기록하며 credential 값을 출력하지 않는다.

## argocd-server 내부 ingress NetworkPolicy 설계안

현재 Tailscale/port-forward의 실제 source namespace, pod selector, node 경유 방식과 CNI 해석을 확인하지 못했다. 따라서 이 PR에서는 NetworkPolicy manifest를 추가하거나 변경하지 않는다. 추측 기반 ingress 제한은 유일한 관리 경로와 rollback 경로를 동시에 차단할 수 있다.

후속 변경은 다음 순서로 설계한다.

1. UI와 CLI 각각에 대해 Tailscale/port-forward의 source pod/namespace/node, destination port, DNS 및 control-plane 의존성을 packet/flow 관측으로 식별한다.
2. 현재 허용 트래픽 baseline과 비상 접근 경로를 문서화한다.
3. default-deny와 명시적 allow 정책을 별도 branch에서 render하고 임시 namespace에서 동일 CNI로 검증한다.
4. 두 개의 독립 관리 세션과 즉시 되돌릴 수 있는 out-of-band 경로를 확보한 maintenance window에서만 별도 승인 후 적용한다.

## NetworkPolicy acceptance criteria

- 정책 미적용 baseline과 적용 후보의 YAML render/schema 검증이 통과한다.
- 승인된 Tailscale UI 경로와 CLI 경로가 새 세션에서 모두 성공한다.
- 승인된 port-forward 경로와 break-glass 경로가 성공한다.
- 비승인 namespace/pod와 외부 source의 argocd-server ingress는 실패한다.
- controller, repo-server, Redis, Kubernetes API, DNS 등 Argo CD 내부 필수 통신과 모든 Application reconcile이 정상이다.
- 전체 Application health가 baseline보다 악화되지 않고 관측 가능하다.
- 정책 제거 rollback을 사전 연습하고 정해진 시간 내 접근 복구를 입증한다.
- source selector, CNI 동작, 테스트 증거와 책임자 승인이 확인되기 전에는 production manifest를 만들거나 적용하지 않는다.