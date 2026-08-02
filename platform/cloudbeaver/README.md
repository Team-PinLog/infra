# CloudBeaver 운영 handoff

CloudBeaver Service는 `pinlog-prod` 내부의 ClusterIP를 계속 사용한다. Kubernetes Ingress, NodePort, LoadBalancer는 생성하지 않는다. 인증된 운영자의 Tailscale 및 `kubectl port-forward` 경로도 유지한다.

전용 Cloudflare Tunnel의 public hostname은 `db.pin-log.com`, origin service는 `http://cloudbeaver.pinlog-prod.svc.cluster.local:8978`이다. Public Hostname 활성화 전에 Cloudflare Access 정책을 먼저 구성하고 검증해야 한다. Cloudflare tunnel의 token owner는 최종 `cloudbeaver-cloudflared-token` SealedSecret 생성과 운영 소유권 handoff를 담당한다. 토큰 평문이나 임시 Secret/SealedSecret은 Git에 저장하지 않는다.

현재 CNI는 FQDN NetworkPolicy를 지원하지 않고 Cloudflare anycast 주소가 변경될 수 있다. 따라서 cloudflared egress는 DNS, 동일 namespace의 CloudBeaver TCP 8978, 그리고 Cloudflare 연결용 TCP/UDP 7844 및 HTTPS TCP 443으로만 제한한다. 외부 목적지는 최소 실용 fallback인 `0.0.0.0/0`이지만 위 포트 외 outbound는 허용하지 않는다.

DB 연결 대상은 `postgres.pinlog-prod.svc.cluster.local:5432`이며 운영 점검 계정은 조회 전용으로 발급한다. 이 저장소에는 DB credential을 두지 않으며 CloudBeaver 초기 설정과 사용자 인증은 외부 handoff 절차로 전달한다.

rollback 시 이전 Git revision과 고정 image digest로 되돌린다. StatefulSet 삭제 또는 축소에도 PVC 보존 정책이 적용되므로 workspace PVC를 삭제하지 않는다.
