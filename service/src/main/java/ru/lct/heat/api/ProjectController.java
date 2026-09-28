package ru.lct.heat.api;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;

import javax.validation.Valid;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;
import ru.lct.heat.domain.Job;
import ru.lct.heat.domain.Project;
import ru.lct.heat.service.ProjectService;
import ru.lct.heat.service.SolveService;
import ru.lct.heat.service.SolverClient;

/** Проекты: загрузка входного GeoJSON, паспорт, запуск расчёта, входной файл для карты. */
@RestController
@RequestMapping("/api/v1")
@Tag(name = "Проекты и расчёт")
public class ProjectController {

    private final ProjectService projects;
    private final SolveService solves;
    private final SolverClient solver;
    private final ObjectMapper mapper;

    public ProjectController(ProjectService projects, SolveService solves, SolverClient solver, ObjectMapper mapper) {
        this.projects = projects;
        this.solves = solves;
        this.solver = solver;
        this.mapper = mapper;
    }

    @GetMapping("/projects")
    @Operation(summary = "Список проектов")
    public List<Dto.ProjectBrief> list() {
        List<Dto.ProjectBrief> result = new ArrayList<>();
        for (Project project : projects.list()) {
            JsonNode issues = json(project.getIssues());
            List<String> jobIds = solves.forProject(project.getId()).stream().map(Job::getId)
                    .collect(Collectors.toList());
            result.add(Dto.brief(project, json(project.getStats()), issues.isArray() ? issues.size() : 0, jobIds));
        }
        return result;
    }

    @PostMapping(value = "/projects", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    @Operation(summary = "Загрузить входной GeoJSON (до 3 ГБ, потоком на диск)")
    public ResponseEntity<Dto.UploadResult> create(@RequestParam("file") MultipartFile file,
                                                   @RequestParam(value = "name", required = false) String name)
            throws IOException {
        String projectName = (name == null || name.isBlank())
                ? stripExtension(file.getOriginalFilename()) : name.trim();
        Project project;
        try (InputStream stream = file.getInputStream()) {
            project = projects.create(projectName, stream);
        }
        Dto.UploadResult result = new Dto.UploadResult();
        result.id = project.getId();
        result.name = project.getName();
        result.stats = json(project.getStats());
        result.issues = json(project.getIssues());
        return ResponseEntity.status(HttpStatus.CREATED).body(result);
    }

    @GetMapping("/projects/{id}")
    @Operation(summary = "Проект с паспортом входа, протоколом разбора и заданиями")
    public ResponseEntity<Dto.ProjectDetail> get(@PathVariable String id) {
        return projects.get(id).map(project -> {
            Dto.ProjectDetail detail = new Dto.ProjectDetail();
            detail.id = project.getId();
            detail.name = project.getName();
            detail.createdAt = Dto.seconds(project.getCreatedAt());
            detail.stats = json(project.getStats());
            detail.issues = json(project.getIssues());
            detail.jobs = solves.forProject(id).stream()
                    .map(job -> Dto.job(job, json(job.getSummary())))
                    .collect(Collectors.toList());
            return ResponseEntity.ok(detail);
        }).orElseGet(() -> ResponseEntity.notFound().build());
    }

    @DeleteMapping("/projects/{id}")
    @Operation(summary = "Удалить проект вместе с файлами")
    public ResponseEntity<Void> delete(@PathVariable String id) throws IOException {
        return projects.delete(id) ? ResponseEntity.noContent().build() : ResponseEntity.notFound().build();
    }

    @GetMapping(value = "/projects/{id}/input.geojson", produces = "application/geo+json")
    @Operation(summary = "Входной файл проекта")
    public ResponseEntity<Resource> input(@PathVariable String id) {
        return projects.get(id)
                .map(project -> ResponseEntity.ok(fileResource(project.getInputPath())))
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    @PostMapping("/projects/{id}/solve")
    @Operation(summary = "Запустить расчёт всех перспективных ОКС")
    public ResponseEntity<Dto.JobView> solve(@PathVariable String id, @Valid @RequestBody Dto.SolveRequest body) {
        return projects.get(id).map(project -> {
            Job job = solves.submit(project, body.mode, body.maxVariants, body.effort);
            return ResponseEntity.status(HttpStatus.ACCEPTED).body(Dto.job(job, null));
        }).orElseGet(() -> ResponseEntity.notFound().build());
    }

    @GetMapping("/rules")
    @Operation(summary = "Таблицы технического приложения (Ду, стоимости, показатель)")
    public JsonNode rules() {
        return solver.rules();
    }

    // --- вспомогательное ---

    JsonNode json(String text) {
        if (text == null || text.isBlank()) {
            return mapper.nullNode();
        }
        try {
            return mapper.readTree(text);
        } catch (IOException exc) {
            return mapper.nullNode();
        }
    }

    static Resource fileResource(String path) {
        Path file = Paths.get(path);
        if (!Files.exists(file)) {
            throw new NotFoundException("файл не найден");
        }
        return new FileSystemResource(file);
    }

    static String stripExtension(String filename) {
        if (filename == null || filename.isBlank()) {
            return "проект";
        }
        int dot = filename.lastIndexOf('.');
        return dot > 0 ? filename.substring(0, dot) : filename;
    }

    /** 404 из вспомогательных методов, где нет ResponseEntity. */
    public static class NotFoundException extends RuntimeException {
        public NotFoundException(String message) {
            super(message);
        }
    }
}
