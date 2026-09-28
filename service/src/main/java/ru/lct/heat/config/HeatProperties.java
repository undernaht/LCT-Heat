package ru.lct.heat.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.ConstructorBinding;

/** Настройки из application.yml (`heat.*`): каталог данных и адрес расчётного сервиса. */
@ConfigurationProperties(prefix = "heat")
@ConstructorBinding
public class HeatProperties {

    private final String dataDir;
    private final String solverUrl;
    private final int solverTimeoutSeconds;
    private final int solveThreads;

    public HeatProperties(String dataDir, String solverUrl, int solverTimeoutSeconds, int solveThreads) {
        this.dataDir = dataDir;
        this.solverUrl = solverUrl;
        this.solverTimeoutSeconds = solverTimeoutSeconds;
        this.solveThreads = solveThreads;
    }

    public String getDataDir() {
        return dataDir;
    }

    public String getSolverUrl() {
        return solverUrl;
    }

    public int getSolverTimeoutSeconds() {
        return solverTimeoutSeconds;
    }

    public int getSolveThreads() {
        return solveThreads;
    }
}
