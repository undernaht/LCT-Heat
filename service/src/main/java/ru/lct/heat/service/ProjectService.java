package ru.lct.heat.service;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.client.RestClientException;
import ru.lct.heat.config.HeatProperties;
import ru.lct.heat.domain.Project;
import ru.lct.heat.repo.JobRepository;
import ru.lct.heat.repo.ProjectRepository;

/** Проекты: приём файла потоком на диск, паспорт входа, удаление вместе с файлами. */
@Service
public class ProjectService {

    private static final Logger log = LoggerFactory.getLogger(ProjectService.class);

    private final ProjectRepository projects;
    private final JobRepository jobs;
    private final SolverClient solver;
    private final Path root;

    public ProjectService(ProjectRepository projects, JobRepository jobs, SolverClient solver, HeatProperties props)
            throws IOException {
        this.projects = projects;
        this.jobs = jobs;
        this.solver = solver;
        this.root = Paths.get(props.getDataDir()).toAbsolutePath();
        Files.createDirectories(root);
    }

    public Path projectDir(String projectId) {
        return root.resolve(projectId);
    }

    /**
     * Файл копируется из входного потока прямо на диск: ни одного массива на 3 ГБ
     * в памяти (ТЗ §3.2). Паспорт входа считает расчётный сервис по пути к файлу.
     */
    @Transactional
    public Project create(String name, InputStream stream) throws IOException {
        String id = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        Path dir = projectDir(id);
        Files.createDirectories(dir);
        Path input = dir.resolve("input.geojson");
        long size = Files.copy(stream, input, StandardCopyOption.REPLACE_EXISTING);
        if (size == 0) {
            Files.deleteIfExists(input);
            Files.deleteIfExists(dir);
            throw new IllegalArgumentException("пустой файл");
        }

        Project project = new Project(id, name, Instant.now(), input.toString(), size);
        try {
            JsonNode passport = solver.inspect(input.toString(), name);
            project.setStats(passport.path("stats").toString());
            project.setIssues(passport.path("issues").toString());
        } catch (RestClientException exc) {
            // Расчётный сервис недоступен или файл не GeoJSON: проект сохраняем,
            // но паспорт пустой — интерфейс покажет предупреждение.
            log.warn("паспорт входа для {} не получен: {}", id, exc.getMessage());
            project.setStats("{}");
            project.setIssues("[{\"level\":\"error\",\"where\":\"solver\",\"detail\":\""
                    + exc.getMessage().replace("\"", "'") + "\",\"count\":1}]");
        }
        return projects.save(project);
    }

    public List<Project> list() {
        return projects.findAllByOrderByCreatedAtDesc();
    }

    public Optional<Project> get(String id) {
        return projects.findById(id);
    }

    @Transactional
    public boolean delete(String id) throws IOException {
        Optional<Project> project = projects.findById(id);
        if (project.isEmpty()) {
            return false;
        }
        jobs.deleteAll(jobs.findAllByProjectIdOrderByCreatedAtAsc(id));
        projects.delete(project.get());
        Path dir = projectDir(id);
        if (Files.exists(dir)) {
            try (var walk = Files.walk(dir)) {
                walk.sorted(java.util.Comparator.reverseOrder()).forEach(path -> {
                    try {
                        Files.deleteIfExists(path);
                    } catch (IOException exc) {
                        log.warn("не удалён {}: {}", path, exc.getMessage());
                    }
                });
            }
        }
        return true;
    }
}
