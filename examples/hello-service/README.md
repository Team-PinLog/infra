# hello-service

PinLog 배포 파이프라인 검증용 최소 서비스.

기능이 목적이 아니라 **푸시 한 번이 사람 손 없이 배포까지 도달하는지 증명**하는
장치다. 실제 기능 개발이 시작된 뒤에 파이프라인을 처음 시험하면 배포 실패의
원인이 앱 코드인지 파이프라인인지 구분되지 않는다.

## 엔드포인트

```
GET /api/hello
→ {"service":"hello-service","sha":"sha-a1b2c3d","time":"..."}
```

응답의 `sha`가 빌드 시점 커밋이다. **이 값이 바뀌는지로 배포 갱신 여부를 판별**한다.

## 검증 방법

```bash
git commit --allow-empty -m "test: 배포 검증"
git push
```

이후 순서대로 확인:

| 단계 | 확인 위치 | 정상 |
|---|---|---|
| 1. 빌드 | Actions 로그 | JAR 생성 |
| 2. GHCR | `Team-PinLog → Packages` | `sha-xxxxx` 태그 |
| 3. infra 갱신 | infra 커밋 이력 | `chore(hello-service): sha-xxxxx` |
| 4. ArgoCD | `kubectl -n argocd get app hello-service-prod` | `Synced` |
| 5. 파드 | `kubectl -n pinlog-prod get pods` | 새 파드 `1/1 Running` |
| 6. 응답 | `curl https://i15a705.p.ssafy.io/api/hello` | **새 SHA** |

ArgoCD 폴링 주기가 최대 3분이라 4~6단계는 약간 기다려야 한다.

## 사전 준비

**`INFRA_REPO_TOKEN`** 시크릿이 필요하다. CI가 `infra` 저장소의 이미지 태그를
갱신하는 데 쓴다.

- classic PAT 기준 **`public_repo` 스코프만** 체크 (`workflow`는 불필요 —
  CI가 워크플로 파일을 건드리지 않으므로)
- org 레벨 시크릿으로 등록 권장:
  `Team-PinLog Settings → Secrets and variables → Actions`
  서비스 저장소가 늘어날 때마다 재등록하지 않아도 된다

GHCR 푸시는 내장 `GITHUB_TOKEN`으로 충분해 별도 토큰이 필요 없다.

## 첫 푸시 후 반드시 할 것

**GHCR 패키지가 private으로 생성된다.** GitHub 기본 동작이라 피할 수 없고,
그 상태로 두면 클러스터가 이미지를 못 받아 `ImagePullBackOff`가 난다.

```
Team-PinLog → Packages → hello-service
  → Package settings → Change visibility → Public
```

public이면 `imagePullSecret` 없이 동작한다. **새 서비스를 만들 때마다 한 번씩
필요한 작업**이다.

## 알아둘 설계 사항

### context-path와 프로브 경로

`application.yml`의 `server.servlet.context-path`가 `/api/hello`다.
Ingress에서 StripPrefix를 쓰지 않기로 했기 때문에 서비스가 경로 prefix를
그대로 받는다. prefix를 벗기면 리다이렉트·Swagger UI·OAuth 콜백이 깨진다.

이 때문에 **actuator 경로도 `/api/hello/actuator/health`로 내려간다.**
infra 저장소의 `apps/prod/hello-service/values.yaml`에서 `probes.path`를
여기에 맞춰뒀다. 안 맞추면 프로브가 404를 받아 파드가 Running인데도
Ready가 되지 않고, Ingress가 503을 반환한다.

**새 서비스를 만들 때도 동일하게 맞춰야 한다.**

### 비루트 사용자

차트가 `runAsNonRoot: true`, `runAsUser: 1000`을 강제하므로 Dockerfile에서
UID 1000 사용자를 만들고 `USER 1000`을 지정한다. 없으면 파드가
`CreateContainerConfigError`로 죽는다.

### 이미지 태그

불변 태그 `sha-<커밋>`만 쓴다. `latest`는 지금 무엇이 돌고 있는지 알 수 없게
만들고, 그게 필요한 순간은 발표 전날 새벽이다.

## 로컬 실행

```bash
docker build -t hello-service --build-arg BUILD_SHA=local .
docker run -p 8080:8080 hello-service
curl http://localhost:8080/api/hello
```
