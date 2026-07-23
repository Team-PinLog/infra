# Cowork dev Cloudflare Tunnel

외부 경로:

```text
https://ticket.pin-log.com
  -> Cloudflare Tunnel ticket-pinlog
  -> http://cowork.pinlog-dev.svc.cluster.local:80
```

Cloudflare Zero Trust의 `ticket-pinlog` Tunnel에서 Public Hostname을 다음처럼 설정한다.

- Hostname: `ticket.pin-log.com`
- Service type: `HTTP`
- URL: `cowork.pinlog-dev.svc.cluster.local:80`

connector token은 `pinlog-dev/cowork-cloudflared-token` Secret의 `token` key로만 전달한다.
평문 token은 Git, 채팅, command argv에 기록하지 않는다.
