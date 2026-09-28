package ru.lct.heat.service;

import java.util.HashMap;
import java.util.Map;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;

/**
 * Клиент расчётного Python-сервиса. Оба сервиса видят один том с данными,
 * поэтому файлы передаются путями, а не телом запроса (вход — до 3 ГБ).
 */
@Component
public class SolverClient {

    private final RestTemplate rest;

    public SolverClient(RestTemplate solverRestTemplate) {
        this.rest = solverRestTemplate;
    }

    /** Паспорт входа и протокол разбора без расчёта. */
    public JsonNode inspect(String inputPath, String name) {
        Map<String, Object> body = new HashMap<>();
        body.put("input_path", inputPath);
        body.put("name", name);
        return rest.postForObject("/api/v1/inspect-file", body, JsonNode.class);
    }

    /** Синхронный расчёт; таймаут держит RestTemplate. */
    public JsonNode solve(String inputPath, String outputPath, String mode, int maxVariants, String effort,
                          String name) {
        Map<String, Object> body = new HashMap<>();
        body.put("input_path", inputPath);
        body.put("output_path", outputPath);
        body.put("mode", mode);
        body.put("max_variants", maxVariants);
        body.put("effort", effort);
        body.put("name", name);
        return rest.postForObject("/api/v1/solve-file", body, JsonNode.class);
    }

    /** Протокол независимого валидатора по входному и выходному файлам. */
    public JsonNode validate(String inputPath, String outputPath) {
        Map<String, Object> body = new HashMap<>();
        body.put("input_path", inputPath);
        body.put("output_path", outputPath);
        return rest.postForObject("/api/v1/validate-file", body, JsonNode.class);
    }

    public JsonNode rules() {
        return rest.getForObject("/api/v1/rules", JsonNode.class);
    }
}
