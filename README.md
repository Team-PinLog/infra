# PinLog Infra

SSAFY 15기 A705 팀 PinLog 프로젝트의 배포 인프라.
**k3s + ArgoCD GitOps** 구조이며, 이 저장소가 클러스터 상태의 단일 진실 원천이다.

> **Terraform을 쓰지 않는 이유**
> 서버는 SSAFY 소유 AWS 계정에 이미 프로비저닝되어 배정된 자원이고,
> 팀에 AWS API 자격증명이 없다. 클라우드 API로 생성·관리할 대상이 없으므로
> 이 저장소는 인프라 프로비저닝이 아니라 **배포 인프라**를 담는다.

---

## 대상 환경

| 항목 | 값 |
|---|---|
| 호스트 | `i15a705.p.ssafy.io` (15.165.74.216) |
| 사양 | 4 vCPU / 15Gi RAM / 309G 디스크, swap 없음 |
| OS | Ubuntu 24.04.3, 커널 6.17, cgroup v2 |
| 리전 | ap-northeast-2a |
| 공개 포트 | 22, 443 (Ingress), 8989 (SSAFY Gerrit) |

### SSAFY 관리 영역 — 건드리지 않는다

| 서비스 | 포트 | 비고 |
|---|---|---|
| Gerrit 3.13.1 | 8988, 29418 | 팀은 GitHub를 사용. 저장소 0개 |
| Apache httpd | 8989 | Gerrit 리버스 프록시 |
| Java 21 (`/opt/java`) | — | Gerrit 전용 |

k3s는 80/443만 사용하므로 위와 충돌하지 않는다.

> ⚠️ **Gerrit에 팀 코드를 올리지 말 것.**
> `auth.type = DEVELOPMENT_BECOME_ANY_ACCOUNT`가 설정되어 있어 누구나 아무 계정으로
> 로그인할 수 있고, 이 설정은 인터넷에서 확인 가능하다. SSAFY 기본 템플릿이라
> 팀이 임의로 바꾸지 않고, 대신 GitHub를 주 저장소로 쓴다.

> ⚠️ **`/opt/httpd/conf/extra/httpd-ssl.conf` 36행에 `Listen 443`이 있다.**
> 현재 `httpd.conf` 510행에서 Include가 주석 처리되어 무해하지만,
> 누군가 주석을 풀면 Apache와 Traefik이 443을 두고 충돌한다.

---

## 저장소 구조

```
infra/
├── bootstrap/          1회성 호스트 스크립트 (GitOps 대상 아님)
├── charts/microservice 모든 서비스가 공용하는 Helm 차트 1개
├── apps/{prod,dev}/    서비스별 values.yaml — 여기에 디렉터리를 추가하면 배포된다
├── platform/           네임스페이스, Ingress, PostgreSQL, Redis
├── argocd/             AppProject, 루트 앱, ApplicationSet
└── secrets/            SealedSecret (공개 저장소에 안전)
```

---

## 최초 부트스트랩

`bootstrap/` 스크립트를 **순서대로** 실행한다.

```bash
sudo ./bootstrap/00-preflight.sh          # ufw/CNI 규칙 — k3s보다 반드시 먼저
sudo ./bootstrap/01-install-k3s.sh        # k3s 설치 + 네트워킹 검증
sudo ./bootstrap/sync-tls-secret.sh       # TLS Secret 주입
sudo ./bootstrap/02-install-sealed-secrets.sh
sudo ./bootstrap/03-install-argocd.sh
sudo ./bootstrap/04-bootstrap-root-app.sh # 마지막 수동 apply
```

### 0단계가 중요한 이유

이 서버의 ufw는 **routed 정책이 deny**다. 그대로 k3s를 설치하면
**파드는 Running인데 DNS/통신이 안 되는** 상태가 되고 원인 파악이 어렵다.
`00-preflight.sh`가 CNI 포워딩을 열어준다.

### Traefik 설정 배포

```bash
sudo cp bootstrap/k3s/traefik-config.yaml \
        /var/lib/rancher/k3s/server/manifests/
```

> 이 파일은 노드에만 존재하는 **드리프트 지점**이다.
> git의 사본이 원본이고, 변경 시 여기를 먼저 고친 뒤 복사한다.

### TLS 자동 동기화 설치

```bash
sudo mkdir -p /opt/pinlog
sudo cp -r bootstrap /opt/pinlog/
sudo cp bootstrap/pinlog-tls-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pinlog-tls-sync.timer
```

### 부트스트랩 후 반드시 할 것

1. **Sealed Secrets 개인키 백업** → `secrets/README.md` 참고
2. **ArgoCD admin 비밀번호 변경** 후 `argocd-initial-admin-secret` 삭제
3. `postgres-credentials` SealedSecret 생성

---

## 서비스 추가하기

**디렉터리 하나 추가가 전부다.**

```bash
mkdir -p apps/prod/auth-service
cat > apps/prod/auth-service/values.yaml <<'EOF'
image:
  repository: ghcr.io/team-pinlog/auth-service
  tag: sha-PLACEHOLDER
ingress:
  path: /api/auth
service:
  targetPort: 8080
EOF
git add . && git commit -m "feat: auth-service 추가" && git push
```

`services-prod` ApplicationSet이 새 디렉터리를 감지해 ArgoCD Application을
자동 생성한다. ArgoCD YAML을 직접 쓸 일이 없다.

설정 가능한 값은 `charts/microservice/values.yaml`에 전부 주석과 함께 있다.

---

## 라우팅 규칙 (백엔드 팀 필독)

**서브도메인을 쓸 수 없다.** 와일드카드 인증서 `*.p.ssafy.io`는 한 레벨만
매칭하므로 `i15a705.p.ssafy.io`는 되지만 `api.i15a705.p.ssafy.io`는 안 된다.
게다가 `p.ssafy.io` DNS는 SSAFY가 관리해서 팀이 레코드를 만들 수도 없다.

따라서 **호스트 하나에 경로 기반 라우팅**을 쓴다.

| 경로 | 서비스 |
|---|---|
| `/api/auth` | auth-service |
| `/api/post` | post-service |
| `/` | 프론트엔드 |

**각 서비스가 자기 경로 prefix를 그대로 소유한다.** StripPrefix를 쓰지 않는다 —
prefix를 벗기면 생성된 리다이렉트, Swagger UI, OAuth 콜백이 깨진다.

Spring Boot라면:

```yaml
server:
  servlet:
    context-path: /api/auth   # values.yaml의 ingress.path와 동일하게
```

---

## CI/CD 흐름

```
서비스 저장소 push
  → GitHub Actions 빌드
  → ghcr.io/team-pinlog/<서비스>:sha-<커밋> 푸시
  → CI가 infra 저장소의 values.yaml tag를 yq로 갱신 후 커밋
  → ArgoCD가 감지해 동기화
  → 파드 롤링 업데이트
```

서비스 저장소 워크플로에 필요한 것:

- `permissions: packages: write` — GHCR 푸시에 `GITHUB_TOKEN`이면 충분 (PAT 불필요)
- `INFRA_REPO_TOKEN` 시크릿 — `Team-PinLog/infra`에 `contents:write`만 가진
  org 레벨 fine-grained PAT

```yaml
- uses: actions/checkout@v4
  with:
    repository: Team-PinLog/infra
    token: ${{ secrets.INFRA_REPO_TOKEN }}
    path: infra
- run: |
    yq -i '.image.tag = "sha-${{ steps.meta.outputs.short_sha }}"' \
      infra/apps/prod/auth-service/values.yaml
    cd infra
    git config user.name  "pinlog-ci"
    git config user.email "ci@pinlog.local"
    git commit -am "chore(auth-service): bump to ${{ github.sha }}"
    git push
```

**태그는 불변 `sha-<커밋>`을 쓴다.** `latest` 금지 — mutable 태그를 쓰면
지금 뭐가 돌고 있는지 알 수 없게 되고, 그게 필요한 순간은 발표 전날 새벽이다.

**GHCR 패키지는 public으로 둔다.** 소스가 이미 public이라 private으로 해서
얻는 게 없는데 네임스페이스마다 pull secret과 토큰 로테이션 비용이 든다.
private을 고수한다면 `bootstrap/k3s/registries.yaml.example` 참고.

---

## 접속

**ArgoCD** (인터넷에 노출하지 않는다)

```bash
# 로컬에서
ssh -L 8080:localhost:8080 ubuntu@i15a705.p.ssafy.io
# 서버에서
sudo k3s kubectl port-forward svc/argocd-server -n argocd 8080:80
# → http://localhost:8080
```

**dev 환경** — Ingress 없음. port-forward 또는 Tailscale로 접근.

```bash
kubectl -n pinlog-dev scale deploy/auth-service --replicas=1
kubectl -n pinlog-dev port-forward svc/auth-service 8080:80
```

---

## 자원 예산

| 구성요소 | 메모리 |
|---|---|
| OS + SSAFY(Gerrit/Apache) | ~2.2Gi |
| k3s + 시스템 파드 | ~1.3Gi |
| ArgoCD | ~1.0Gi |
| Traefik + Sealed Secrets | ~130Mi |
| PostgreSQL + Redis | ~0.9Gi |
| **애플리케이션 가용분** | **~9.5Gi** |

서비스 기본값은 `requests 384Mi / limits 768Mi`다.

**주의사항**

- **dev/prod 전체 미러링은 안 들어간다.** dev는 `replicaCount: 0`이 기본이고
  작업 중인 것만 올린다.
- **이 서버에 self-hosted Actions runner를 돌리지 말 것.** Gradle 빌드가
  4 vCPU를 다 먹고 파드를 축출한다. public 저장소는 GitHub 러너가 무료다.
- **swap이 없어서 메모리 압박은 느려짐이 아니라 즉사다.** JVM 서비스는
  `JAVA_TOOL_OPTIONS`의 `MaxRAMPercentage`를 반드시 유지한다.

---

## ⚠️ 알려진 리스크

### 1. TLS 인증서 만료 — 2026-09-21 (프로젝트 기간 내 영향 없음)

`*.p.ssafy.io` 인증서는 **수동 DNS-01**로 발급되어(`/etc/letsencrypt/renewal/p.ssafy.io.conf`)
팀이 갱신할 수 없다. 다만 **프로젝트가 만료일 전에 종료**되므로 실제 영향은 없다.

`pinlog-tls-sync.timer`가 매일 호스트 인증서를 확인하고 있어서,
SSAFY가 그 전에 갱신하면 24시간 내 클러스터가 자동으로 반영한다.

만약 일정이 밀려 9월 21일을 넘기게 되면 그날 HTTPS가 통째로 죽는다.
그 경우 SSAFY에 80/tcp 개방을 요청하고 cert-manager로 `i15a705.p.ssafy.io`
단일 인증서를 HTTP-01로 발급받으면 영구 자동 갱신된다 (DNS가 이미 우리 IP를
가리키므로 SSAFY DNS 개입 불필요).

### 2. 단일 노드 / 단일 디스크

인스턴스 장애 하나 또는 SSAFY 재이미징으로 전부 사라진다.

**주 1회 서버 밖 백업 복사는 선택이 아니라 필수다.** 담당자를 지정해
스프린트 체크리스트에 넣을 것. 클러스터 안 백업은 `DROP TABLE`은 막아도
박스를 잃는 건 못 막는다.

### 3. Sealed Secrets 개인키

분실 시 저장소의 모든 SealedSecret이 영구 복호화 불가. 첫날 백업할 것.

---

## SSAFY 담당자에게 확인할 사항

1. `*.p.ssafy.io` 인증서(**2026-09-21 만료**) 갱신 주체와 일정,
   갱신본이 `/etc/letsencrypt/live/p.ssafy.io/`에 들어가는지
2. **80/tcp 보안그룹 개방** 가능 여부 (HTTPS 리다이렉트 + 자체 ACME 갱신용)
3. 이 인스턴스의 **재이미징·회수 가능성** (백업 정책이 여기 달림)
4. A705 팀에 **서버 추가 배정** 여부
5. (참고) Gerrit 기본 템플릿의 `DEVELOPMENT_BECOME_ANY_ACCOUNT` 설정은
   15기 전체 서버 공통 이슈일 가능성이 높음

---

## 검증

```bash
# 클러스터
k3s kubectl get nodes
k3s kubectl get pods -A
k3s kubectl top nodes

# 파드 네트워킹 (ufw 카나리아)
k3s kubectl run t --image=busybox --rm -it --restart=Never -- nslookup kubernetes.default

# TLS — 반드시 서버 "밖"에서. 안에서 하면 보안그룹 문제가 가려진다.
openssl s_client -connect i15a705.p.ssafy.io:443 \
  -servername i15a705.p.ssafy.io </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -dates

# ArgoCD
k3s kubectl -n argocd get applications   # 전부 Synced / Healthy

# 백업 — 테스트하지 않은 백업은 백업이 아니다
k3s kubectl -n pinlog-prod create job --from=cronjob/postgres-backup manual-test
k3s kubectl -n pinlog-prod logs job/manual-test
```

### 엔드투엔드 검증 (가장 중요)

실제 기능이 생기기 전에 `hello-service`로 전체 왕복을 뚫는다.

1. `Team-PinLog/hello-service` 생성 — `GET /api/hello`가 빌드 SHA 반환
2. main에 푸시
3. Actions 빌드 → GHCR 태그 → CI가 `infra`에 커밋 → ArgoCD 동기화 → 롤링
4. 노트북에서 `curl https://i15a705.p.ssafy.io/api/hello` → **새 SHA** 확인

**이 왕복이 사람 손 없이 돌면 플랫폼은 완성이다.**
