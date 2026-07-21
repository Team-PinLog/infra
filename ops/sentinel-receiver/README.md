# PinLog Sentinel Receiver

Alertmanager의 webhook payload를 PinLog 전용 Hermes 프로필(`pinlog-alerts`)로 가공하고 기존 Mattermost Incoming Webhook으로 전송하는 호스트 서비스입니다.

## 보안 경계

- Alertmanager 요청은 32자 이상의 Bearer token으로 인증합니다.
- Receiver는 HTTPS 전용으로 동작하며 Alertmanager는 CA Secret으로 인증서를 검증합니다.
- Receiver 접근 source IP는 loopback과 k3s pod CIDR(`10.42.0.0/16`)로 제한합니다.
- 원본 payload와 Mattermost URL은 로그·SQLite에 저장하지 않습니다.
- payload는 프로세스 argv에 넣지 않고 Hermes worker subprocess stdin으로 전달합니다.
- Hermes worker는 tool schema를 하나도 받지 않습니다(`toolsets=[]`).
- Mattermost URL과 TLS private key는 systemd `LoadCredential=`로 전달합니다.
- Receiver 런타임은 Kubernetes kubeconfig/API 권한 없이 `pinlog-sentinel` 전용 사용자로 실행합니다.
- 실패 저장소에는 payload hash, dedupe key, stage, 오류 유형만 저장하며 최대 1,000건으로 제한합니다.

## 설치

```bash
cd /root/infra/ops/sentinel-receiver
./install.sh
```

설치 스크립트는 다음을 수행합니다.

- `/etc/pinlog-sentinel/receiver.env`가 없으면 Receiver Bearer token을 생성합니다.
- `monitoring/mattermost-alert-webhook` Secret을 설치 시점에 한 번 읽어 `/etc/pinlog-sentinel/mattermost_url` credential 파일로 저장합니다.
- 자체 서명 TLS 인증서/키가 없으면 생성합니다.
- `pinlog-alerts` Hermes 프로필을 `/var/lib/pinlog-sentinel/hermes`로 복사하고 전용 사용자 소유로 맞춥니다.
- systemd unit을 재시작합니다.

`receiver.env`의 token 원문과 Mattermost URL은 Git, 터미널 출력, Jira, 로그에 남기지 않습니다. Alertmanager용 Secret/SealedSecret에는 token만 전달하고 TLS 검증용 공개 인증서(`ca.crt`)는 별도 Secret으로 전달합니다.

## 확인

```bash
systemctl status pinlog-sentinel-receiver
curl --fail --cacert /etc/pinlog-sentinel/tls.crt \
  --resolve pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765:127.0.0.1 \
  https://pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765/healthz
curl --fail --cacert /etc/pinlog-sentinel/tls.crt \
  --resolve pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765:127.0.0.1 \
  https://pinlog-sentinel-receiver.monitoring.svc.cluster.local:9765/metrics
journalctl -u pinlog-sentinel-receiver --since '30 minutes ago'
```

## 전달 정책

- Critical FIRING: trusted code가 정확히 한 번 `@channel` 삽입, Alertmanager repeat 1시간
- Warning FIRING: 멘션 없음, repeat 6시간
- RESOLVED: 항상 전송, 멘션 없음
- 모델 출력의 Mattermost mention과 URL은 deterministic하게 제거합니다.
- 동일 group/status/fingerprint/content는 Receiver에서 5분 동안 중복 억제합니다.
- Sentinel 또는 Mattermost 실패 시 HTTP 502를 반환하여 Alertmanager가 재시도합니다.

## 롤백

```bash
systemctl disable --now pinlog-sentinel-receiver
```

Alertmanager Helm values에서 receiver route를 제거하거나 Alertmanager를 다시 비활성화한 뒤 동기화합니다.
