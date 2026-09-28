package ru.lct.heat.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import ru.lct.heat.domain.Job;
import ru.lct.heat.service.SolveService;

/** Задания: статус, сводка для интерфейса, выгрузка результата (до 500 МБ, потоком). */
@RestController
@RequestMapping("/api/v1/jobs")
@Tag(name = "Задания расчёта")
public class JobController {

    private final SolveService solves;
    private final ObjectMapper mapper;

    public JobController(SolveService solves, ObjectMapper mapper) {
        this.solves = solves;
        this.mapper = mapper;
    }

    @GetMapping("/{id}")
    @Operation(summary = "Состояние задания")
    public ResponseEntity<Dto.JobView> get(@PathVariable String id) {
        return solves.get(id)
                .map(job -> ResponseEntity.ok(Dto.job(job, summary(job))))
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    @GetMapping("/{id}/summary")
    @Operation(summary = "Сводка результата: варианты, участки, камеры, проверки")
    public ResponseEntity<JsonNode> summary(@PathVariable String id) {
        return solves.get(id)
                .filter(job -> Job.DONE.equals(job.getStatus()) && job.getSummary() != null)
                .map(job -> ResponseEntity.ok(summary(job)))
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    @GetMapping(value = "/{id}/output.geojson", produces = "application/geo+json")
    @Operation(summary = "Выходной GeoJSON по техническому приложению")
    public ResponseEntity<Resource> output(@PathVariable String id) {
        return solves.get(id)
                .filter(job -> Job.DONE.equals(job.getStatus()) && job.getOutputPath() != null)
                .map(job -> ResponseEntity.ok()
                        .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"result_" + id + ".geojson\"")
                        .body(ProjectController.fileResource(job.getOutputPath())))
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    @GetMapping("/{id}/validation")
    @Operation(summary = "Протокол независимого валидатора по выходу задания")
    public ResponseEntity<JsonNode> validation(@PathVariable String id) throws java.io.IOException {
        Job job = solves.get(id).orElse(null);
        if (job == null) {
            return ResponseEntity.notFound().build();
        }
        return solves.validation(job)
                .map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    private JsonNode summary(Job job) {
        if (job.getSummary() == null || job.getSummary().isBlank()) {
            return null;
        }
        try {
            return mapper.readTree(job.getSummary());
        } catch (java.io.IOException exc) {
            return null;
        }
    }
}
