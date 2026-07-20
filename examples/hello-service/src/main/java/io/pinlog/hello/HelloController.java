package io.pinlog.hello;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.util.Map;

/**
 * 배포 파이프라인 검증용 엔드포인트.
 *
 * 응답에 빌드 시점의 커밋 SHA를 담는 것이 핵심이다.
 * 이게 없으면 배포가 실제로 갱신되었는지 확인할 방법이 없다 —
 * 응답이 같으면 새 버전인지 옛 버전인지 구분되지 않는다.
 */
@RestController
public class HelloController {

    @Value("${build.sha:unknown}")
    private String buildSha;

    // context-path 가 /api/hello 이므로 여기서는 루트만 매핑한다.
    // "/api/hello" 를 다시 붙이면 실제 경로가 /api/hello/api/hello 가 된다.
    //
    // Spring 6부터 후행 슬래시 자동 매칭이 제거되어
    // "" 와 "/" 를 모두 명시해야 /api/hello 와 /api/hello/ 가 함께 동작한다.
    @GetMapping({"", "/"})
    public Map<String, String> hello() {
        return Map.of(
                "service", "hello-service",
                "sha", buildSha,
                "time", Instant.now().toString()
        );
    }
}
