# CloudBeaver 운영 handoff

CloudBeaver는 `pinlog-prod` 내부의 ClusterIP로만 제공한다. Ingress 또는 Cloudflare 경로는 만들지 않는다.
인증된 운영자는 Tailscale로 관리망에 접속한 뒤 `kubectl port-forward`를 사용한다. 이 저장소에는 DB credential을 두지 않으며, CloudBeaver 초기 설정과 사용자 인증은 외부 handoff 절차로 전달한다.

DB 연결 대상은 `postgres.pinlog-prod.svc.cluster.local:5432`이며 운영 점검 계정은 조회 전용으로 발급한다. PostgreSQL 포트와 CloudBeaver 포트를 외부에 공개하지 않는다.

rollback 시 이전 Git revision과 고정 image digest로 되돌린다. StatefulSet 삭제 또는 축소에도 PVC 보존 정책이 적용되므로 workspace PVC를 삭제하지 않는다.
