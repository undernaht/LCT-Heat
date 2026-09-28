package ru.lct.heat.api;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import javax.validation.constraints.Max;
import javax.validation.constraints.Min;
import javax.validation.constraints.Pattern;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.JsonNode;
import ru.lct.heat.domain.Job;
import ru.lct.heat.domain.Project;

/**
 * Формы ответов повторяют Python-сервис (backend/app/case/api.py): фронтенд
 * работает с обоими без правок. Имена полей — в snake_case, как там.
 */
public final class Dto {

    private Dto() {
    }

    public static class SolveRequest {
        @Pattern(regexp = "2d|depth")
        public String mode = "2d";
        @Min(1) @Max(3)
        @JsonProperty("max_variants")
        public int maxVariants = 3;
        @Pattern(regexp = "standard|thorough")
        public String effort = "standard";
    }

    public static class ProjectBrief {
        public String id;
        public String name;
        @JsonProperty("created_at")
        public double createdAt;
        public JsonNode stats;
        public int issues;
        public List<String> jobs;
    }

    public static class ProjectDetail {
        public String id;
        public String name;
        @JsonProperty("created_at")
        public double createdAt;
        public JsonNode stats;
        public JsonNode issues;
        public List<JobView> jobs;
    }

    public static class JobView {
        public String id;
        @JsonProperty("project_id")
        public String projectId;
        public String mode;
        @JsonProperty("max_variants")
        public int maxVariants;
        public String effort;
        public String status;
        @JsonProperty("created_at")
        public double createdAt;
        @JsonProperty("started_at")
        public Double startedAt;
        @JsonProperty("finished_at")
        public Double finishedAt;
        public String progress;
        public String error;
        @JsonProperty("output_path")
        public String outputPath;
        public JsonNode summary;
        @JsonProperty("elapsed_s")
        public Double elapsedS;
    }

    public static class UploadResult {
        public String id;
        public String name;
        public JsonNode stats;
        public JsonNode issues;
    }

    public static class Error {
        public String detail;

        public Error(String detail) {
            this.detail = detail;
        }
    }

    // --- сборка ---

    static double seconds(Instant instant) {
        return instant.toEpochMilli() / 1000.0;
    }

    static JobView job(Job job, JsonNode summary) {
        JobView view = new JobView();
        view.id = job.getId();
        view.projectId = job.getProjectId();
        view.mode = job.getMode();
        view.maxVariants = job.getMaxVariants();
        view.effort = job.getEffort();
        view.status = job.getStatus();
        view.createdAt = seconds(job.getCreatedAt());
        view.startedAt = job.getStartedAt() == null ? null : seconds(job.getStartedAt());
        view.finishedAt = job.getFinishedAt() == null ? null : seconds(job.getFinishedAt());
        view.progress = job.getProgress();
        view.error = job.getError();
        view.outputPath = job.getOutputPath();
        view.summary = summary;
        if (job.getStartedAt() != null) {
            Instant end = job.getFinishedAt() == null ? Instant.now() : job.getFinishedAt();
            view.elapsedS = Math.round((end.toEpochMilli() - job.getStartedAt().toEpochMilli()) / 100.0) / 10.0;
        }
        return view;
    }

    static ProjectBrief brief(Project project, JsonNode stats, int issues, List<String> jobIds) {
        ProjectBrief brief = new ProjectBrief();
        brief.id = project.getId();
        brief.name = project.getName();
        brief.createdAt = seconds(project.getCreatedAt());
        brief.stats = stats;
        brief.issues = issues;
        brief.jobs = jobIds;
        return brief;
    }

    static Map<String, Object> ok(String key, Object value) {
        return Map.of(key, value);
    }
}
