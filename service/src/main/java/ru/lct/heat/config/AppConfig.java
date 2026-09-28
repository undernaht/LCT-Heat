package ru.lct.heat.config;

import java.time.Duration;
import java.util.concurrent.Executor;

import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Info;
import org.springframework.boot.web.client.RestTemplateBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.client.RestTemplate;

/** Клиент расчётного сервиса, пул расчётов и описание API для Swagger. */
@Configuration
public class AppConfig {

    /** Расчёт долгий: таймаут чтения — из настроек, а не по умолчанию. */
    @Bean
    public RestTemplate solverRestTemplate(RestTemplateBuilder builder, HeatProperties props) {
        return builder
                .rootUri(props.getSolverUrl())
                .setConnectTimeout(Duration.ofSeconds(10))
                .setReadTimeout(Duration.ofSeconds(props.getSolverTimeoutSeconds()))
                .build();
    }

    /**
     * Пул расчётов: их мало и они тяжёлые, поэтому ограничены отдельно от HTTP-потоков.
     * Очередь не ограничена — задание ждёт, а не отклоняется.
     */
    @Bean(name = "solveExecutor")
    public Executor solveExecutor(HeatProperties props) {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(props.getSolveThreads());
        executor.setMaxPoolSize(props.getSolveThreads());
        executor.setThreadNamePrefix("solve-");
        executor.initialize();
        return executor;
    }

    @Bean
    public OpenAPI openApi() {
        return new OpenAPI().info(new Info()
                .title("Сервис моделирования трасс подключения к тепловым сетям")
                .description("Кейс ЛЦТ 2026. Загрузка GeoJSON по техническому приложению, "
                        + "автоматическое построение вариантов подключения, выгрузка результата.")
                .version("0.1.0"));
    }
}
