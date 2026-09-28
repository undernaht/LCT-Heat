package ru.lct.heat.domain;

import java.time.Instant;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Lob;
import javax.persistence.Table;

/** Задание расчёта: одно обращение к расчётному сервису по одному проекту. */
@Entity
@Table(name = "jobs")
public class Job {

    public static final String QUEUED = "queued";
    public static final String RUNNING = "running";
    public static final String DONE = "done";
    public static final String FAILED = "failed";

    @Id
    @Column(length = 32)
    private String id;

    @Column(name = "project_id", nullable = false, length = 32)
    private String projectId;

    @Column(nullable = false, length = 16)
    private String mode;

    @Column(name = "max_variants", nullable = false)
    private int maxVariants;

    @Column(nullable = false, length = 16)
    private String effort;

    @Column(nullable = false, length = 16)
    private String status;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "started_at")
    private Instant startedAt;

    @Column(name = "finished_at")
    private Instant finishedAt;

    private String progress;

    @Lob
    @Column(columnDefinition = "TEXT")
    private String error;

    @Column(name = "output_path", length = 1024)
    private String outputPath;

    @Lob
    @Column(columnDefinition = "TEXT")
    private String summary;

    protected Job() {
    }

    public Job(String id, String projectId, String mode, int maxVariants, String effort) {
        this.id = id;
        this.projectId = projectId;
        this.mode = mode;
        this.maxVariants = maxVariants;
        this.effort = effort;
        this.status = QUEUED;
        this.createdAt = Instant.now();
        this.progress = "";
    }

    public String getId() {
        return id;
    }

    public String getProjectId() {
        return projectId;
    }

    public String getMode() {
        return mode;
    }

    public int getMaxVariants() {
        return maxVariants;
    }

    public String getEffort() {
        return effort;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getStartedAt() {
        return startedAt;
    }

    public void setStartedAt(Instant startedAt) {
        this.startedAt = startedAt;
    }

    public Instant getFinishedAt() {
        return finishedAt;
    }

    public void setFinishedAt(Instant finishedAt) {
        this.finishedAt = finishedAt;
    }

    public String getProgress() {
        return progress;
    }

    public void setProgress(String progress) {
        this.progress = progress;
    }

    public String getError() {
        return error;
    }

    public void setError(String error) {
        this.error = error;
    }

    public String getOutputPath() {
        return outputPath;
    }

    public void setOutputPath(String outputPath) {
        this.outputPath = outputPath;
    }

    public String getSummary() {
        return summary;
    }

    public void setSummary(String summary) {
        this.summary = summary;
    }
}
