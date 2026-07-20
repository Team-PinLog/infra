# 운영 런북

장애 대응과 자주 쓰는 명령. **증상에서 출발해 원인을 찾는 순서**로 정리했다.

---

## 0. 접속

### 서버

```bash
ssh ubuntu@i15a705.p.ssafy.io
```

### kubectl

k3s가 `/usr/local/bin/kubectl`을 심볼릭 링크로 만들어둔다. root면 그대로 되고,
일반 사용자로 쓰려면:

```bash
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config
```

> `kubeseal` 등 일부 도구는 `kubectl`과 달리 KUBECONFIG를 자동 감지하지 못한다.
> `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml` 를 명시할 것.

### ArgoCD UI

**인터넷에 노출하지 않는다.** 공개된 ArgoCD + 팀 공용 비밀번호는
클러스터 전체를 내주는 것과 같다.

```bash
# 로컬 노트북에서
ssh -L 8080:localhost:8080 ubuntu@i15a705.p.ssafy.io

# 서버에서
sudo k3s kubectl port-forward svc/argocd-server -n argocd 8080:80
```
→ 브라우저 `http://localhost:8080`

---

## 1. 상태 한눈에 보기

```bash
# 배포 상태 — 여기가 전부 Synced/Healthy면 정상
kubectl -n argocd get applications

# 파드
kubectl get pods -A

# 자원
kubectl top nodes
kubectl top pods -A --sort-by=memory

# 외부에서 서비스 확인 (반드시 서버 "밖"에서)
curl -I https://i15a705.p.ssafy.io/api/<서비스>
```

정상 기준선 (2026-07-20 구축 직후): CPU 13%, 메모리 3.5Gi / 12Gi 여유

---

## 2. 증상별 대응

### 파드가 `ImagePullBackOff`

```bash
kubectl -n pinlog-prod describe pod <파드명> | tail -20
```

| 원인 | 확인 | 해결 |
|---|---|---|
| **GHCR 패키지가 private** (가장 흔함) | `Team-PinLog → Packages` | Package settings → Change visibility → **Public** |
| 태그가 실제로 없음 | Packages에서 태그 목록 확인 | CI 빌드 성공 여부 확인 |
| 저장소 이름 오타 | `values.yaml`의 `image.repository` | 수정 후 커밋 |

> 새 서비스의 **첫 푸시는 항상 private으로 생성**된다. GitHub 기본 동작이라
> 피할 수 없고, 서비스마다 한 번씩 public 전환이 필요하다.

### 파드가 `CreateContainerConfigError`

거의 항상 **비루트 사용자 누락**이다. 차트가 `runAsNonRoot: true`,
`runAsUser: 1000`을 강제하는데 이미지에 UID 1000이 없는 경우.

```dockerfile
RUN addgroup -g 1000 app && adduser -u 1000 -G app -D app
USER 1000
```

Secret 참조 오류일 수도 있다:
```bash
kubectl -n pinlog-prod describe pod <파드명> | grep -A5 Events
```

### 파드는 `Running`인데 Ingress가 503

**프로브 실패로 Ready가 안 된 것이다.** `READY` 열이 `0/1`인지 확인:

```bash
kubectl -n pinlog-prod get pods
kubectl -n pinlog-prod describe pod <파드명> | grep -A10 "Liveness\|Readiness"
```

가장 흔한 원인은 **`context-path`와 프로브 경로 불일치**다.
`context-path: /api/auth`면 actuator 경로는 `/api/auth/actuator/health`가 된다.
차트 기본값 `/actuator/health`로는 404를 받는다.

```yaml
# apps/prod/<서비스>/values.yaml
probes:
  path: /api/<서비스>/actuator/health
```

직접 확인:
```bash
kubectl -n pinlog-prod exec <파드명> -- wget -qO- http://localhost:8080/api/<서비스>/actuator/health
```

포트 불일치도 확인할 것 — `service.targetPort`와 앱의 실제 리슨 포트.

### 404가 나온다

경로 매칭 문제다.

```bash
# Ingress가 실제로 어떤 경로를 잡고 있는지
kubectl -n pinlog-prod get ingress -o wide
kubectl -n pinlog-prod describe ingress <서비스>
```

체크리스트:
1. `values.yaml`의 `ingress.path`와 앱의 `context-path`가 **동일한가**
2. 컨트롤러가 경로를 중복해 붙이지 않았는가
   (`context-path: /api/hello` + `@GetMapping("/api/hello")` → `/api/hello/api/hello`)
3. 후행 슬래시 — Spring 6부터 자동 매칭이 제거되었다. `@GetMapping({"", "/"})`

### 파드가 계속 재시작 (OOM)

```bash
kubectl -n pinlog-prod describe pod <파드명> | grep -i -A3 "last state\|reason"
kubectl top pods -n pinlog-prod
```

`OOMKilled`면:

1. **JVM이 cgroup 한도를 모르는 경우** — `JAVA_TOOL_OPTIONS`에
   `-XX:MaxRAMPercentage=70`이 있는지 확인. 없으면 힙을 한도 너머로 잡는다
2. `resources.limits.memory` 상향 — 단, **swap이 없어서 메모리 압박은
   느려짐이 아니라 즉사다.** 전체 예산(§ architecture.md 4장)을 보고 올릴 것

### ArgoCD가 git 변경을 안 따라옴

기본 폴링 주기가 **최대 3분**이다. 먼저 기다려보고:

```bash
kubectl -n argocd get app <앱명> -o jsonpath='{.status.sync.status}{"\n"}'
kubectl -n argocd get app <앱명> -o jsonpath='{range .status.conditions[*]}{.type}: {.message}{"\n"}{end}'
```

| 메시지 | 원인 |
|---|---|
| `InvalidSpecError: ... do not match any of the allowed destinations` | AppProject `destinations`에 해당 네임스페이스 누락 |
| `rpc error ... repository not found` | 저장소 접근 실패 (public인지 확인) |
| `ComparisonError` | 매니페스트 문법 오류 — 로컬에서 `helm template`으로 재현 |

강제 동기화:
```bash
kubectl -n argocd patch app <앱명> --type merge \
  -p '{"operation":{"sync":{"revision":"main"}}}'
```

### 파드 간 통신이 안 됨 (DNS 실패)

**ufw 포워딩 규칙 문제**일 가능성이 높다.

```bash
# 카나리아 — 반드시 FQDN을 쓸 것
kubectl run dns-test --image=busybox:1.36 --rm -i --restart=Never -- \
  nslookup kubernetes.default.svc.cluster.local
```

> ⚠️ 짧은 이름(`kubernetes.default`)으로 확인하면 busybox가 search domain을
> 제대로 적용하지 못해 **정상 클러스터에서도 NXDOMAIN**이 나온다.

실패하면:
```bash
sudo ufw status | grep -i fwd     # ALLOW FWD 규칙 4개가 보여야 정상
sudo /root/infra/bootstrap/00-preflight.sh
```

### HTTPS 인증서 오류

```bash
# 반드시 서버 "밖"에서. 안에서 하면 보안그룹 문제가 가려진다
openssl s_client -connect i15a705.p.ssafy.io:443 \
  -servername i15a705.p.ssafy.io </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

`Verify return code: 0`이어야 한다. 체인이 불완전하면 `fullchain.pem`이 아니라
`cert.pem`을 쓴 것이다.

```bash
# Secret 재동기화
sudo /opt/pinlog/bootstrap/sync-tls-secret.sh

# 인증서 개수 확인 — 1개면 리프만 있는 것(잘못됨), 여러 개여야 정상
kubectl -n kube-system get secret pinlog-wildcard-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | grep -c "BEGIN CERTIFICATE"

# 타이머 상태
systemctl status pinlog-tls-sync.timer
journalctl -u pinlog-tls-sync.service -n 20
```

### GitHub Actions가 이상하다

**설정을 뒤지기 전에 장애부터 확인한다.** 구축 중 실제로 `startup_failure`가
두 번 났는데 전부 GitHub 측 장애였다.

```bash
curl -s https://www.githubstatus.com/api/v2/summary.json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status']['description']); [print(c['name'], c['status']) for c in d['components'] if c['name'] in ('Actions','API Requests')]"
```

---

## 3. 정기 작업

### 주 1회 — 서버 밖 백업 (담당자 지정 필수)

**클러스터 안 백업만으로는 부족하다.** 단일 노드·단일 디스크라
인스턴스 장애나 SSAFY 재이미징 한 번으로 전부 사라진다.

```bash
# 서버에서 최신 덤프 위치 확인
sudo k3s kubectl -n pinlog-prod exec postgres-0 -- ls -lh /backup/ 2>/dev/null || \
sudo find /var/lib/rancher/k3s/storage -name 'pinlog-*.dump' -newer /tmp -ls 2>/dev/null | tail -3

# 로컬로 복사
scp ubuntu@i15a705.p.ssafy.io:<덤프경로> ./backups/
```

### 백업 검증 (한 달에 한 번은)

**테스트하지 않은 백업은 백업이 아니다.**

```bash
kubectl -n pinlog-prod create job --from=cronjob/postgres-backup backup-test
kubectl -n pinlog-prod wait --for=condition=complete job/backup-test --timeout=180s
kubectl -n pinlog-prod logs job/backup-test

# 스크래치 DB에 실제 복원해볼 것 (덤프가 열리는지까지 확인)
kubectl -n pinlog-prod exec postgres-0 -- psql -U pinlog -d postgres -c "CREATE DATABASE restore_check;"
# ... pg_restore 후 행 수 확인 ...
kubectl -n pinlog-prod exec postgres-0 -- psql -U pinlog -d postgres -c "DROP DATABASE restore_check;"

kubectl -n pinlog-prod delete job backup-test
```

---

## 4. 자주 쓰는 작업

### dev 환경에서 서비스 띄우기

dev는 기본 `replicaCount: 0`이다 (자원 부족으로 전체 미러링 불가).

```bash
kubectl -n pinlog-dev scale deploy/<서비스> --replicas=1
kubectl -n pinlog-dev port-forward svc/<서비스> 8080:80
```

`services-dev` ApplicationSet은 `selfHeal: false`라 ArgoCD가 되돌리지 않는다.

### DB 접속

```bash
kubectl -n pinlog-prod exec -it postgres-0 -- psql -U pinlog -d pinlog

# 비밀번호가 필요하면 (SealedSecret이 복호화한 값)
kubectl -n pinlog-prod get secret postgres-credentials \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

### 로그

```bash
kubectl -n pinlog-prod logs -f deploy/<서비스>
kubectl -n pinlog-prod logs deploy/<서비스> --previous   # 재시작 전 로그
kubectl -n kube-system logs -f deploy/traefik            # 라우팅 문제
kubectl -n argocd logs -f deploy/argocd-repo-server      # 동기화 문제
```

### 롤백

**git이 진실의 원천이므로 `git revert`가 곧 롤백이다.**

```bash
cd infra
git revert <이미지_태그_올린_커밋>
git push
```

ArgoCD가 3분 내 이전 이미지로 되돌린다. `kubectl rollout undo`는 쓰지 말 것 —
ArgoCD가 다시 되돌려 놓는다.

### 새 시크릿 추가

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl create secret generic <이름> \
  --namespace pinlog-prod \
  --from-literal=<키>='<값>' \
  --dry-run=client -o yaml \
| kubeseal --format yaml \
    --controller-name sealed-secrets-controller \
    --controller-namespace kube-system \
> secrets/prod/<이름>.sealedsecret.yaml

git add secrets/prod/<이름>.sealedsecret.yaml
git commit -m "feat: <이름> 시크릿 추가" && git push
```

### 워커 노드 추가 (서버가 더 배정되면)

```bash
# 마스터에서 토큰 확인
sudo cat /var/lib/rancher/k3s/server/node-token

# 새 서버에서
curl -sfL https://get.k3s.io | K3S_URL=https://172.26.14.189:6443 \
  K3S_TOKEN=<토큰> sh -
```

> PostgreSQL에는 이미 `nodeSelector`가 걸려 있어 마스터에 고정된다.
> `local-path` PV가 노드에 묶이기 때문이며, 이 설정이 없으면 파드가 다른
> 노드로 스케줄되어 데이터를 찾지 못한다.

---

## 5. 비상시

### 클러스터 전체 재시작

```bash
sudo systemctl restart k3s
kubectl get pods -A -w
```

### k3s 완전 제거 (최후의 수단)

```bash
sudo /usr/local/bin/k3s-uninstall.sh
```

> ⚠️ **PVC 데이터가 사라진다.** `local-path-retain` StorageClass가
> `reclaimPolicy: Retain`이라 `/var/lib/rancher/k3s/storage/` 아래 디렉터리는
> 남지만, 재구축 후 수동으로 연결해야 한다. 반드시 백업을 먼저 확보할 것.

재구축은 `bootstrap/` 스크립트를 순서대로 실행하면 된다
(`README.md` 참고). **단, Sealed Secrets 개인키를 복원하지 않으면
저장소의 모든 SealedSecret이 복호화 불가**가 되므로 백업 키가 필수다.

### SSAFY 서비스가 영향받았는지 확인

```bash
curl -sk -o /dev/null -w "Gerrit: HTTP %{http_code}\n" https://i15a705.p.ssafy.io:8989/
systemctl is-active gerrit httpd
sudo ufw status | grep -E "^(22|8989|443)"
```

---

## 관련 문서

- [`architecture.md`](architecture.md) — 구조와 설계 결정 근거
- [`../README.md`](../README.md) — 부트스트랩 절차
- [`../examples/README.md`](../examples/README.md) — 새 서비스 추가
- [`../secrets/README.md`](../secrets/README.md) — 시크릿 관리
