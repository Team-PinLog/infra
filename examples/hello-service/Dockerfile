# ---- 빌드 단계 ----
# Gradle wrapper를 커밋하지 않고 이미지의 gradle을 쓴다.
# 버전은 고정한다 — latest를 쓰면 어느 날 갑자기 빌드가 깨진다.
FROM gradle:8.14.5-jdk21 AS build
WORKDIR /app

# 의존성 캐시 최적화: 빌드 스크립트만 먼저 복사해 의존성을 받아둔다.
# 소스만 바뀌면 이 레이어가 재사용되어 빌드가 크게 빨라진다.
COPY settings.gradle build.gradle ./
RUN gradle dependencies --no-daemon --quiet || true

COPY src ./src
RUN gradle bootJar --no-daemon

# ---- 실행 단계 ----
FROM eclipse-temurin:21-jre-alpine

# 이 LABEL이 있어야 GHCR 패키지가 저장소에 자동 연결된다.
# 없으면 Packages 화면에서 수동으로 연결해야 한다.
LABEL org.opencontainers.image.source=https://github.com/Team-PinLog/hello-service

# 차트의 securityContext가 runAsNonRoot: true, runAsUser: 1000 을 강제한다.
# 이미지에 해당 UID가 없으면 파드가 CreateContainerConfigError로 죽는다.
RUN addgroup -g 1000 app && adduser -u 1000 -G app -D app

# 빌드 시점 커밋 SHA. CI가 --build-arg 로 넘긴다.
ARG BUILD_SHA=unknown
ENV BUILD_SHA=${BUILD_SHA}

COPY --from=build /app/build/libs/*.jar /app.jar

USER 1000
EXPOSE 8080

# JAVA_TOOL_OPTIONS(MaxRAMPercentage 등)는 차트의 env에서 주입되므로
# 여기서 힙 크기를 하드코딩하지 않는다.
ENTRYPOINT ["java", "-jar", "/app.jar"]
