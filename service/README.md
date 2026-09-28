# Приложение по ТЗ (Java 11, Spring Boot 2.6.3)

Приём входного GeoJSON потоком на диск (до 3 ГБ), учёт проектов и заданий в
PostgreSQL, вызов расчётного Python-сервиса, выгрузка результата (до 500 МБ),
Swagger UI (springdoc-openapi-ui 1.7.0), раздача собранного фронтенда.

API повторяет форму Python-сервиса (`backend/app/case/api.py`), поэтому фронтенд
одинаково работает с обоими. Контракт с расчётным сервисом — `POST /api/v1/solve-file`
и `POST /api/v1/inspect-file` по путям на общем томе (`docs/02-architecture.md` §0).

## Сборка и запуск

Стенд целиком (приложение, расчётный сервис, PostgreSQL):

```bash
docker-compose up --build
```

Приложение — http://localhost:8080, Swagger — http://localhost:8080/swagger-ui.html.

Локально без Docker (JDK 11+ и Maven; PostgreSQL на 5432 — или H2 файлом,
драйвер в runtime-зависимостях). Расчётный сервис должен слушать 8010:

```bash
mvn -f service/pom.xml -DskipTests package
```

```bash
java -jar service/target/heat-network-service-0.1.0.jar "--spring.datasource.url=jdbc:h2:file:./service/target/run/heat;MODE=PostgreSQL" --spring.datasource.username=sa --spring.datasource.password= --heat.data-dir=./service/target/run/projects --spring.web.resources.static-locations=file:./frontend/dist/
```

Собиралось и проверялось на JDK 17 (JetBrains Runtime) с Maven 3.9.16; исходники
компилируются под Java 11 (`java.version` в pom). В `.claude/launch.json` есть
конфигурация `heat-java-app` с этими же параметрами.

Тесты (поднимают контекст на H2, расчётный сервис не нужен):

```bash
mvn -f service/pom.xml test
```

## Что где

| Файл | Зачем |
|---|---|
| `api/ProjectController.java` | загрузка, список, паспорт, запуск расчёта, входной файл |
| `api/JobController.java` | состояние задания, сводка, выгрузка результата |
| `service/ProjectService.java` | копирование потока на диск, паспорт от расчётного сервиса, удаление |
| `service/SolveService.java` | пул расчётов, вызов `solve-file`, статусы заданий |
| `service/SolverClient.java` | RestTemplate к расчётному сервису с таймаутами |
| `resources/schema.sql` | таблицы `projects`, `jobs` (идемпотентно) |
| `resources/application.yml` | multipart до 3 ГБ на диск, адреса из окружения |
