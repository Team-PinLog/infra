# hello-service

PinLog 배포 계약을 검증하기 위한 최소 서비스 애플리케이션 예제다.

현재 `back`·`front` 저장소와 서비스 CI는 미구현이므로 사람 손 없는 배포 E2E가
완료된 상태가 아니다. 이 예제의 중첩 `.github/workflows/deploy.yaml`은 초기 prototype
기록이며 새 서비스 저장소에 복사하지 않는다. 실제 CI는 기능 브랜치·PR 기반으로
별도 구현하고 검증해야 한다.

## 엔드포인트

```
GET /api/hello
→ {"service":"hello-service","sha":"sha-a1b2c3d","time":"..."}
```

응답의 `sha`가 빌드 시점 커밋이다. **이 값이 바뀌는지로 배포 갱신 여부를 판별**한다.

## 검증 방법

```bash
git switch -c test/S15P11A705-123-deployment
git commit --allow-empty -m "test: 배포 검증"
git push -u origin HEAD
# 서비스 PR 생성 → CI 성공 → merge
```

이후 순서대로 확인:

| 단계 | 확인 위치 | 정상 |
|---|---|---|
| 1. 빌드 | Actions 로그 | JAR 생성 |
| 2. GHCR | `Team-PinLog → Packages` | `sha-xxxxx` 태그 |
| 3. infra 변경 | infra PR | image tag와 Jira/TDD 증거 |
| 4. infra CI | PR checks | `pr-policy`, `guardrails`, `helm` 성공 |
| 5. ArgoCD | `kubectl -n argocd get app hello-service-prod` | `Synced/Healthy` |
| 6. 파드 | `kubectl -n pinlog-prod get pods` | 새 파드 `1/1 Running` |
| 7. 응답 | `curl https://i15a705.p.ssafy.io/api/hello` | **새 SHA** |

ArgoCD 폴링 주기가 최대 3분이라 5~7단계는 약간 기다려야 한다.

## 사전 준비

서비스 CI identity와 infra PR token 설계는 아직 확정되지 않았다. 임의의 classic PAT를
공유하거나 infra main 직접 push 권한을 부여하지 않는다. 구현 시
[`../../docs/git-governance.md`](../../docs/git-governance.md)의 최소 권한·PR 계약을
따른다.

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
