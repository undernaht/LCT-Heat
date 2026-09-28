package ru.lct.heat;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.ResponseEntity;

/** Контекст поднимается на H2 без расчётного сервиса; список проектов пуст. */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "spring.datasource.url=jdbc:h2:mem:heat;MODE=PostgreSQL;DB_CLOSE_DELAY=-1",
        "spring.datasource.driver-class-name=org.h2.Driver",
        "spring.datasource.username=sa",
        "spring.datasource.password=",
        "heat.data-dir=target/test-data",
})
class HeatApplicationTest {

    @Autowired
    private TestRestTemplate rest;

    @Test
    void projectsListIsEmptyOnFreshDatabase() {
        ResponseEntity<String> response = rest.getForEntity("/api/v1/projects", String.class);
        assertThat(response.getStatusCode().is2xxSuccessful()).isTrue();
        assertThat(response.getBody()).isEqualTo("[]");
    }

    @Test
    void unknownJobIs404() {
        ResponseEntity<String> response = rest.getForEntity("/api/v1/jobs/nope", String.class);
        assertThat(response.getStatusCodeValue()).isEqualTo(404);
        ResponseEntity<String> validation = rest.getForEntity("/api/v1/jobs/nope/validation", String.class);
        assertThat(validation.getStatusCodeValue()).isEqualTo(404);
    }

    @Test
    void swaggerIsServed() {
        ResponseEntity<String> response = rest.getForEntity("/v3/api-docs", String.class);
        assertThat(response.getStatusCode().is2xxSuccessful()).isTrue();
        assertThat(response.getBody()).contains("/api/v1/projects");
    }

    /** Ресурсы сборки фронтенда не подменяются index.html: отсутствующий файл — 404, а не HTML. */
    @Test
    void missingAssetIsNotForwardedToIndex() {
        ResponseEntity<String> response = rest.getForEntity("/assets/index-missing.js", String.class);
        assertThat(response.getStatusCodeValue()).isEqualTo(404);
    }

    /** Неизвестный путь API — 404 от API, а не страница приложения. */
    @Test
    void unknownApiPathIsNotForwardedToIndex() {
        ResponseEntity<String> response = rest.getForEntity("/api/v1/nothing-here", String.class);
        assertThat(response.getStatusCodeValue()).isEqualTo(404);
    }
}
