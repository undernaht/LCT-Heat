package ru.lct.heat.service;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.Executor;

import javax.annotation.PostConstruct;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Service;
import ru.lct.heat.domain.Job;
import ru.lct.heat.domain.Project;
import ru.lct.heat.repo.JobRepository;

/**
 * Задания расчёта. Постановка — синхронно (запись в БД), выполнение — в пуле
 * `solveExecutor` (вызов через executor, а не через @Async: аннотация не
 * работает при вызове метода из того же класса). Задания, застрявшие в очереди
 * при перезапуске, помечаются упавшими: результат их всё равно потерян.
 */
@Service
public class SolveService {

    private static final Logger log = LoggerFactory.getLogger(SolveService.class);

    private final JobRepository jobs;
    private final ProjectService projectService;
    private final SolverClient solver;
    private final Executor executor;
    private final ObjectMapper mapper;

    public SolveService(JobRepository jobs, ProjectService projectService, SolverClient solver,
                        @Qualifier("solveExecutor") Executor executor, ObjectMapper mapper) {
        this.jobs = jobs;
        this.projectService = projectService;
        this.solver = solver;
        this.executor = executor;
        this.mapper = mapper;
    }

    @PostConstruct
    void failOrphans() {
        List<Job> orphans = jobs.findAllByStatusIn(List.of(Job.QUEUED, Job.RUNNING));
        for (Job job : orphans) {
            job.setStatus(Job.FAILED);
            job.setError("сервис перезапущен во время расчёта");
            job.setFinishedAt(Instant.now());
        }
        if (!orphans.isEmpty()) {
            jobs.saveAll(orphans);
            log.warn("помечено упавшими заданий после перезапуска: {}", orphans.size());
        }
    }

    public Job submit(Project project, String mode, int maxVariants, String effort) {
        String id = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        Job job = jobs.save(new Job(id, project.getId(), mode, maxVariants, effort));
        executor.execute(() -> run(job.getId(), project));
        return job;
    }

    void run(String jobId, Project project) {
        Job job = jobs.findById(jobId).orElseThrow();
        job.setStatus(Job.RUNNING);
        job.setStartedAt(Instant.now());
        job.setProgress("расчёт");
        jobs.save(job);
        try {
            Path outDir = projectService.projectDir(project.getId()).resolve("results").resolve(jobId);
            Path output = outDir.resolve("output.geojson");
            JsonNode response = solver.solve(project.getInputPath(), output.toString(), job.getMode(),
                    job.getMaxVariants(), job.getEffort(), project.getName());
            job.setOutputPath(output.toString());
            job.setSummary(response.path("summary").toString());
            job.setStatus(Job.DONE);
            job.setProgress("готово");
        } catch (Exception exc) {
            log.error("расчёт {} упал", jobId, exc);
            job.setStatus(Job.FAILED);
            job.setError(exc.getClass().getSimpleName() + ": " + exc.getMessage());
        } finally {
            job.setFinishedAt(Instant.now());
            jobs.save(job);
        }
    }

    public Optional<Job> get(String id) {
        return jobs.findById(id);
    }

    /**
     * Протокол валидатора для готового задания: считается расчётным сервисом при
     * первом обращении и кэшируется рядом с результатом (validation.json).
     */
    public Optional<JsonNode> validation(Job job) throws IOException {
        if (!Job.DONE.equals(job.getStatus()) || job.getOutputPath() == null) {
            return Optional.empty();
        }
        Project project = projectService.get(job.getProjectId()).orElse(null);
        if (project == null) {
            return Optional.empty();
        }
        Path output = Path.of(job.getOutputPath());
        Path cache = output.resolveSibling("validation.json");
        if (Files.exists(cache)) {
            return Optional.of(mapper.readTree(Files.readString(cache, StandardCharsets.UTF_8)));
        }
        JsonNode report = solver.validate(project.getInputPath(), job.getOutputPath());
        Files.writeString(cache, mapper.writeValueAsString(report), StandardCharsets.UTF_8);
        return Optional.of(report);
    }

    public List<Job> forProject(String projectId) {
        return jobs.findAllByProjectIdOrderByCreatedAtAsc(projectId);
    }
}
