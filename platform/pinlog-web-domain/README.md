# PinLog web domain Cloudflare Tunnel

Cloudflare tunnel `pin-log-service`는 `pin-log.com` 전용이다. Kubernetes connector는
`pinlog-web-cloudflared`로 관리한다. 기존 레거시 호스트 Ingress와
`ticket-pinlog`/cowork connector는 그대로 유지하며, AI와 Grafana는 이 host에 route하지 않는다.

```text
Cloudflare Edge TLS
  -> pin-log-service tunnel / pinlog-web-cloudflared connector
  -> Traefik HTTPS
  -> Host pin-log.com
     /api/core    -> back.pinlog-prod:80
     /api-console -> api-console.pinlog-dev:80
     /             -> front.pinlog-dev:80
```

## Cloudflare dashboard handoff

Cloudflare Zero Trust dashboard의 `pin-log-service` tunnel에 Public Hostname을 다음 계약으로 등록한다.

- Hostname: `pin-log.com`
- Service URL: `https://traefik.kube-system.svc.cluster.local:443`
- Origin Server Name: 레거시 origin 인증서의 SAN 호스트 (비공개 운영 인벤토리 참조)
- HTTP Host Header: `pin-log.com`
- No TLS Verify: `false`

Cloudflare는 TLS SNI를 Origin Server Name으로 보내므로 Traefik의 기존 레거시 origin
인증서를 검증할 수 있다. HTTP Host Header는 별도로
`pin-log.com`을 유지해 신규 Ingress router만 선택한다. Ingress의 `tls`에는
`secretName`을 두지 않고 cluster-wide default TLSStore를 사용한다.

connector token은 권한 있는 운영자가 hidden handoff로 sealing했고,
`secrets/dev/pinlog-web-cloudflared-token.sealedsecret.yaml`의 strict-scoped SealedSecret이
`pinlog-dev/pinlog-web-cloudflared-token` Secret의 `token` key를 생성한다. 평문 token은
Git, PR, 채팅, shell history, 명령 인자 또는 임시 평문 파일에 노출하지 않는다.

## activation

전용 dashboard route와 encrypted token 준비가 확인되어 Deployment는 `replicas: 1`로
활성화 요청된 상태다. merge 전 Argo CD diff에서 다음 경계를 다시 확인하고, sync 후
runtime smoke를 완료해야 activation이 끝난다.

1. SealedSecret의 name/namespace/key가 각각 `pinlog-web-cloudflared-token`, `pinlog-dev`, `token`이고 strict scope인지 확인한다.
2. dashboard Public Hostname과 비공개 Origin Server Name이 운영 계약과 정확히 일치하고 No TLS Verify가 false인지 확인한다.
3. Argo CD diff에서 기존 Ingress, cowork Deployment/token, AI/Grafana 리소스 변경이 없는지 확인한다.
4. sync 후 cloudflared `/ready` probe와 metrics port 2000이 정상인지 확인한다.

## smoke

활성화 뒤 외부와 origin 계약을 함께 검증한다.

- `https://pin-log.com/`이 frontend 응답을 반환한다.
- `https://pin-log.com/api-console/`이 API console 응답을 반환한다.
- `https://pin-log.com/api/core/actuator/health/readiness`가 backend readiness 응답을 반환한다.
- 임의의 AI/Grafana 경로가 해당 서비스로 연결되지 않는다.
- Cloudflare connector가 `pin-log-service` tunnel에만 연결되고 TLS 검증 오류가 없는지 확인한다.
- 레거시 호스트와 기존 API 경로가 계속 동작하는지 회귀 확인한다.

## availability

connector는 단일 replica와 `Recreate` 전략을 사용하므로 image 또는 pod template 업데이트 시
기존 Pod 종료부터 새 connector Ready까지 짧은 tunnel 중단이 발생한다. 레거시
호스트는 이 구간에도 rollback 경로로 유지한다.

## rollback

긴급 장애 시 Application이 존재하는 상태에서 reviewed GitOps 변경으로 `replicas: 0`을
먼저 sync해 connector를 중지한 뒤 Cloudflare dashboard의 `pin-log.com` Public Hostname을
비활성화한다. 레거시 호스트와 cowork tunnel은 수정하거나 재사용하지 않는다.

영구 제거는 root Application prune 순서에 의존하지 않도록 반드시 두 단계로 수행한다.

1. **1단계:** `pinlog-web-domain` Application을 유지한 채 별도 PR에서 Deployment/Ingress
   manifest를 제거하고 sync/prune한다. `pinlog-web-cloudflared` Deployment와 두
   `pin-log.com` Ingress가 실제로 사라졌는지 **Deployment/Ingress 삭제 확인**을 완료한다.
2. **2단계:** 위 확인 후 두 번째 별도 PR에서 `pinlog-web-domain` Application 제거를
   수행한다. child Application의 resource finalizer는 비정상 순서에서도 orphan 생성을
   막는 방어선이며, 1단계 절차를 생략하는 근거가 아니다.

SealedSecret은 `secrets-dev` Application의 `prune: false` 정책으로 별도 관리된다.
서비스 rollback에서는 보존하며, tunnel을 영구 폐기할 때만 Cloudflare token 폐기와
SealedSecret/복호화 Secret 제거를 **별도 승인** 작업으로 수행한다.
