# examples

새 마이크로서비스를 만들 때 복사해서 쓰는 참조 구현.

**여기 있는 것은 배포되지 않는다.** ArgoCD의 ApplicationSet은 `apps/*/*`만
스캔하므로 `examples/` 아래 파일은 클러스터에 아무 영향을 주지 않는다.
(`examples/hello-service/.github/workflows/deploy.yaml` 도 마찬가지다 —
GitHub는 저장소 루트의 `.github/workflows/`만 읽는다.)

## hello-service

배포 파이프라인이 실제로 동작하는지 검증하기 위해 만든 최소 Spring Boot
서비스. `gradle:8.14.5-jdk21` 컨테이너에서 빌드·실행해 다음을 확인했다.

```
/api/hello                  -> 200 {"sha":"sha-xxxxx",...}
/api/hello/                 -> 200 (후행 슬래시)
/api/hello/actuator/health  -> 200 {"status":"UP"}
/actuator/health            -> 404  (context-path 때문. 의도된 동작)
```

## 새 서비스를 만드는 순서

### 1. 서비스 저장소 생성

`Team-PinLog/<서비스명>` (**Public** 권장 — Actions가 무료·무제한이고
GHCR 패키지도 public으로 둘 수 있다).

`examples/hello-service/` 전체를 복사한 뒤 다음을 바꾼다:

| 파일 | 바꿀 것 |
|---|---|
| `settings.gradle` | `rootProject.name` |
| `src/main/java/io/pinlog/hello/` | 패키지 디렉터리명 |
| `HelloController.java` | 클래스명, 응답 내용 |
| `application.yml` | `context-path` → `/api/<서비스명>` |
| `Dockerfile` | `LABEL org.opencontainers.image.source` |
| `.github/workflows/deploy.yaml` | `IMAGE`, `apps/prod/<서비스명>/values.yaml` 경로 |

### 2. infra 저장소에 배포 정의 추가

```bash
mkdir -p apps/prod/<서비스명>
cat > apps/prod/<서비스명>/values.yaml <<'EOF'
image:
  repository: ghcr.io/team-pinlog/<서비스명>
  tag: sha-PLACEHOLDER      # CI가 갱신한다. 수동으로 고치지 말 것

ingress:
  enabled: true
  host: i15a705.p.ssafy.io
  path: /api/<서비스명>

service:
  targetPort: 8080

# context-path 때문에 actuator 경로가 함께 내려간다.
# 이걸 빠뜨리면 프로브가 404를 받아 파드가 Running인데도 Ready가 되지 않고
# Ingress가 503을 반환한다. 원인 찾기 어려운 종류이니 반드시 맞출 것.
probes:
  path: /api/<서비스명>/actuator/health
EOF
git add . && git commit -m "feat: <서비스명> 추가" && git push
```

ApplicationSet이 새 디렉터리를 감지해 ArgoCD Application을 자동 생성한다.
ArgoCD YAML을 직접 쓸 일은 없다.

### 3. 시크릿 등록 (한 번만)

`INFRA_REPO_TOKEN` — CI가 infra 저장소의 이미지 태그를 갱신하는 데 쓴다.
**org 레벨 시크릿**으로 등록하면 서비스가 늘어나도 재등록할 필요가 없다.

`Team-PinLog Settings → Secrets and variables → Actions → New organization secret`

classic PAT 기준 **`public_repo` 스코프만** 체크한다
(`workflow`는 불필요 — CI가 워크플로 파일을 건드리지 않는다).

GHCR 푸시는 내장 `GITHUB_TOKEN`으로 충분해 별도 토큰이 필요 없다.

### 4. 첫 빌드 후 GHCR 패키지를 public으로

**첫 푸시에서 패키지가 private으로 생성된다.** GitHub 기본 동작이라 피할 수
없고, 그대로 두면 클러스터가 이미지를 못 받아 `ImagePullBackOff`가 난다.

```
Team-PinLog → Packages → <서비스명>
  → Package settings → Change visibility → Public
```

**서비스를 만들 때마다 한 번씩 필요한 작업이다.**

## 반드시 지켜야 할 규약

### 경로 prefix를 서비스가 소유한다

와일드카드 인증서 `*.p.ssafy.io`가 한 레벨만 매칭하므로
`api.i15a705.p.ssafy.io` 같은 서브도메인은 쓸 수 없다. 호스트 하나에
경로 기반으로 나눈다.

Ingress에서 StripPrefix를 쓰지 않으므로 **각 서비스가 `/api/<서비스명>`을
그대로 받아야 한다.** prefix를 벗기면 생성된 리다이렉트, Swagger UI,
OAuth 콜백이 깨진다.

### 비루트 사용자

차트가 `runAsNonRoot: true`, `runAsUser: 1000`을 강제한다.
Dockerfile에서 UID 1000 사용자를 만들고 `USER 1000`을 지정하지 않으면
파드가 `CreateContainerConfigError`로 죽는다.

### 불변 태그

`sha-<커밋>`만 쓴다. `latest`는 지금 무엇이 돌고 있는지 알 수 없게 만들고,
그게 필요한 순간은 발표 전날 새벽이다.
