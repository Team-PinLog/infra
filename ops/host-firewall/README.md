# Host management-port firewall

`harden-management-ports.sh`는 AWS/VPC/public 인터페이스 `enX0`에서 다음 TCP 포트를 명시적으로 차단합니다.

- Gerrit: `8988`, `8989`, `29418`
- PinLog Sentinel Receiver: `9765`
- node-exporter: `9100`
- kubelet: `10250`
- k3s API: `6443`

적용 전에 `/var/backups/pinlog-host-firewall/management-ports-<UTC>/`에 UFW 설정과 iptables ruleset, 관련 서비스 상태를 저장하고 실행 가능한 `rollback.sh`를 생성합니다. 모든 UFW rule은 dry-run으로 먼저 파싱합니다.

보존 계약:

- `tailscale0`: Gerrit web `8989`와 기존 SSH 관리 경로
- `cni0`, source `10.42.0.0/16`: Sentinel, node-exporter, kubelet, k3s API consumer
- Gerrit `8988/8989/29418`: broad `cni0` allow보다 앞선 deny로 pod 접근도 차단
- localhost: Gerrit reverse proxy와 host-local k3s consumer
- public `80/443`, CNI forwarding, ServiceLB forwarding: 변경하지 않음

실행:

```bash
sudo ops/host-firewall/harden-management-ports.sh
```

롤백은 실행 결과에 출력된 절대 경로의 `rollback.sh`를 root로 실행합니다. 이 롤백은 UFW 정본만 복원하며 서비스를 재시작하지 않습니다.
