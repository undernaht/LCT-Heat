package ru.lct.heat;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;
import org.springframework.scheduling.annotation.EnableAsync;

/**
 * Приложение по ТЗ §3.2: приём файлов, учёт проектов и заданий, API и фронтенд.
 * Расчёт делает отдельный Python-сервис; здесь только его вызов и хранение результата.
 */
@SpringBootApplication
@ConfigurationPropertiesScan
@EnableAsync
public class HeatApplication {

    public static void main(String[] args) {
        SpringApplication.run(HeatApplication.class, args);
    }
}
