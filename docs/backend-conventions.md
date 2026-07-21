# 백엔드 개발 규약

**대상**: PinLog 백엔드 서비스를 개발하는 모든 팀원
**중요**: 컨트롤러를 작성하기 **전에** 읽어야 합니다. 나중에 바꾸면 여러 곳이 깨집니다.

---

## 요약 (먼저 이것만)

내가 만드는 서비스가 `auth-service`라면:

```yaml
# src/main/resources/application.yml
server:
  port: 8080
  servlet:
    context-path: /api/auth        # ← 이 한 줄이 핵심
```

```dockerfile
# Dockerfile
RUN addgroup -g 1000 app && adduser -u 1000 -G app -D app
USER 1000                          # ← 이거 없으면 배포 안 됨
```

외부 접근 주소는 `https://i15a705.p.ssafy.io/api/auth/...` 가 됩니다.

---

## 1. 왜 경로로 나누는가 (서브도메인 불가)

`auth.pinlog.com` 같은 서브도메인을 **쓸 수 없습니다.**

서버에 제공된 SSL 인증서가 `*.p.ssafy.io` 하나뿐인데, 와일드카드 인증서는
**정확히 한 단계만** 커버합니다. `i15a705.p.ssafy.io`는 되지만
`auth.i15a705.p.ssafy.io`는 안 됩니다. 게다가 DNS를 SSAFY가 관리해서
우리가 주소를 새로 만들 수도 없습니다.

그래서 **주소 하나에 경로로 서비스를 구분**합니다.

| 경로 | 서비스 |
|---|---|
| `https://i15a705.p.ssafy.io/api/auth/**` | auth-service |
| `https://i15a705.p.ssafy.io/api/post/**` | post-service |
| `https://i15a705.p.ssafy.io/` | 프론트엔드 |

---

## 2. 서비스가 경로 prefix를 직접 갖는다

인프라에서 `/api/auth`를 떼어내고 넘겨주지 **않습니다.** 요청이 온 그대로
`/api/auth/login` 이 서비스에 도착합니다.

### 왜 떼어내지 않는가

prefix를 벗기면 이런 것들이 전부 깨집니다:

- 스프링이 자동 생성하는 **리다이렉트 URL** (로그인 후 이동 등)
- **Swagger UI** 의 "Try it out" 요청 주소
- **OAuth 콜백 URL** (소셜 로그인)

서비스가 자기 경로를 알고 있는 편이 디버깅도 훨씬 쉽습니다.

### 설정 방법

```yaml
server:
  servlet:
    context-path: /api/auth
```

이렇게 하면 컨트롤러는 **prefix를 빼고** 작성합니다.

```java
@RestController
@RequestMapping("/login")          // 실제 주소: /api/auth/login
public class AuthController { }
```

> ⚠️ **흔한 실수**: `context-path: /api/auth` 를 설정해놓고 컨트롤러에도
> `@RequestMapping("/api/auth")` 를 쓰면 실제 주소가
> `/api/auth/api/auth` 가 되어 404가 납니다.

### 후행 슬래시 주의

Spring 6부터 후행 슬래시 자동 매칭이 제거되었습니다. 루트 경로를 매핑할 때는
둘 다 명시하세요.

```java
@GetMapping({"", "/"})
```

---

## 3. Actuator 경로가 함께 내려갑니다

`context-path`를 설정하면 actuator 주소도 그 아래로 이동합니다.

```
/actuator/health              →  /api/auth/actuator/health
/actuator/prometheus          →  /api/auth/actuator/prometheus
```

**인프라의 헬스체크 설정도 여기에 맞춰야 합니다.** 안 맞추면
파드는 살아 있는데 계속 `503`이 나오고, 원인을 찾기 매우 어렵습니다.

`infra` 저장소의 `apps/prod/<서비스>/values.yaml`:

```yaml
probes:
  path: /api/auth/actuator/health
```

Actuator는 **필수 의존성**입니다. 없으면 헬스체크가 실패해 배포가 안 됩니다.

```groovy
implementation 'org.springframework.boot:spring-boot-starter-actuator'
```

```yaml
management:
  endpoints:
    web:
      exposure:
        include: health
  endpoint:
    health:
      probes:
        enabled: true
```

---

## 4. 컨테이너는 비루트로 실행됩니다

보안 설정상 컨테이너가 **UID 1000**으로 강제 실행됩니다.
이미지에 해당 사용자가 없으면 파드가 `CreateContainerConfigError`로 죽습니다.

```dockerfile
FROM eclipse-temurin:21-jre-alpine
RUN addgroup -g 1000 app && adduser -u 1000 -G app -D app
USER 1000
```

파일을 써야 한다면 `/tmp` 를 쓰거나 인프라 담당자에게 볼륨을 요청하세요.

---

## 5. DB · Redis 접속 정보

클러스터 내부 주소입니다. 외부에서는 접근할 수 없습니다.

| 대상 | 주소 |
|---|---|
| PostgreSQL | `postgres.pinlog-prod.svc.cluster.local:5432` |
| Redis | `redis.pinlog-prod.svc.cluster.local:6379` |

- DB 이름: `pinlog` / 사용자: `pinlog`
- **비밀번호는 코드나 설정 파일에 넣지 마세요.** 환경변수로 주입됩니다.

```yaml
spring:
  datasource:
    url: jdbc:postgresql://postgres.pinlog-prod.svc.cluster.local:5432/pinlog
    username: pinlog
    password: ${DB_PASSWORD}       # 환경변수로 주입됨
  data:
    redis:
      host: redis.pinlog-prod.svc.cluster.local
      port: 6379
```

새 비밀번호나 API 키가 필요하면 **인프라 담당자에게 요청**하세요.
암호화해서 저장소에 넣고 환경변수로 연결해드립니다.

> Redis는 캐시·세션 전용이라 **재시작하면 비워집니다.** 잃으면 안 되는
> 데이터(예: refresh token)를 넣어야 하면 미리 말씀해주세요. 설정을 바꿔야 합니다.

---

## 6. 배포 방식

기능 브랜치와 Pull Request에서 검증한 뒤 merge합니다. `main`에 직접 push하지
않습니다.

```
서비스 PR·CI → 불변 이미지(GHCR) → infra image tag PR·필수 CI
→ ArgoCD sync → 배포 확인
```

- 이미지 태그는 **커밋 SHA**로 자동 지정됩니다. `latest`를 쓰지 않습니다
- infra 변경과 롤백도 기능 브랜치·PR을 사용합니다
- **배포 상태 확인**: 인프라 담당자에게 문의하거나 Grafana에서 로그 확인

현재 서비스 저장소 CI는 아직 구현되지 않았습니다. 저장소가 준비되면 인프라
담당자와 함께 build·GHCR·infra PR·ArgoCD E2E를 검증해야 합니다.

### 첫 배포 시 필요한 것

인프라 담당자가 처리합니다. 저장소를 만들면 알려주세요.

---

## 7. 로그와 메트릭 보기

**https://i15a705.p.ssafy.io/grafana**

### 로그

좌측 메뉴 → **Explore** → 데이터소스를 **Loki** 선택:

```logql
{namespace="pinlog-prod", app="auth-service"}

# 에러만
{namespace="pinlog-prod", app="auth-service"} |= "ERROR"
```

> 헬스체크 로그는 자동으로 걸러집니다. 안 보이는 게 정상입니다.

### 메트릭 (선택)

기본은 꺼져 있습니다. 켜려면 의존성을 추가하고 인프라 담당자에게 알려주세요.

```groovy
implementation 'io.micrometer:micrometer-registry-prometheus'
```

```yaml
management:
  endpoints:
    web:
      exposure:
        include: health,prometheus
```

---

## 8. 로컬 개발

로컬에서는 `context-path` 때문에 주소에 prefix가 붙습니다.

```
http://localhost:8080/api/auth/login
```

DB는 로컬 Docker로 띄우고 접속 정보만 바꾸면 됩니다.
`application-local.yml` 로 프로파일을 분리하는 것을 권장합니다.

---

## 체크리스트

서비스를 만들 때 확인하세요.

- [ ] `context-path` 를 `/api/<서비스명>` 으로 설정했다
- [ ] 컨트롤러에 prefix를 **중복해서** 붙이지 않았다
- [ ] `spring-boot-starter-actuator` 의존성을 추가했다
- [ ] Dockerfile에 `USER 1000` 과 사용자 생성이 있다
- [ ] 비밀번호·API 키를 코드에 넣지 않았다 (환경변수 사용)
- [ ] 저장소를 만들고 인프라 담당자에게 알렸다

---

## 자주 겪는 문제

| 증상 | 원인 |
|---|---|
| **404** | `context-path` 와 컨트롤러 경로가 중복됨 |
| **503** | 헬스체크 경로 불일치 (actuator가 context-path 아래로 이동) |
| 배포는 됐는데 파드가 안 뜸 | Dockerfile에 `USER 1000` 누락 |
| 파드가 계속 재시작 | 메모리 부족. 인프라 담당자에게 문의 |

막히면 인프라 담당자에게 **서비스명과 증상**을 알려주세요.

---

관련 문서: [`../examples/README.md`](../examples/README.md) (서비스 추가 절차),
[`git-governance.md`](git-governance.md) (브랜치·PR 규칙),
[`monitoring.md`](monitoring.md) (모니터링 상세)
