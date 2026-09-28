package ru.lct.heat.domain;

import java.time.Instant;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Lob;
import javax.persistence.Table;

/** Проект = один загруженный входной файл. Паспорт и протокол хранятся как JSON-текст. */
@Entity
@Table(name = "projects")
public class Project {

    @Id
    @Column(length = 32)
    private String id;

    @Column(nullable = false)
    private String name;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "input_path", nullable = false, length = 1024)
    private String inputPath;

    @Column(name = "input_size", nullable = false)
    private long inputSize;

    @Lob
    @Column(columnDefinition = "TEXT")
    private String stats;

    @Lob
    @Column(columnDefinition = "TEXT")
    private String issues;

    protected Project() {
    }

    public Project(String id, String name, Instant createdAt, String inputPath, long inputSize) {
        this.id = id;
        this.name = name;
        this.createdAt = createdAt;
        this.inputPath = inputPath;
        this.inputSize = inputSize;
    }

    public String getId() {
        return id;
    }

    public String getName() {
        return name;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public String getInputPath() {
        return inputPath;
    }

    public long getInputSize() {
        return inputSize;
    }

    public String getStats() {
        return stats;
    }

    public void setStats(String stats) {
        this.stats = stats;
    }

    public String getIssues() {
        return issues;
    }

    public void setIssues(String issues) {
        this.issues = issues;
    }
}
