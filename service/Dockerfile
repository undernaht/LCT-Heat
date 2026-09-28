# Приложение по ТЗ §3.2. Сборка в два этапа: Maven (Java 11) → тонкий образ с JRE 11.
# Фронтенд собирается отдельно и кладётся в classpath:/static.

FROM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM maven:3.8-eclipse-temurin-11 AS build
WORKDIR /src
COPY service/pom.xml ./
RUN mvn -q -B dependency:go-offline
COPY service/src ./src
COPY --from=frontend /src/dist ./src/main/resources/static
RUN mvn -q -B -DskipTests package

FROM eclipse-temurin:11-jre
WORKDIR /srv/app
COPY --from=build /src/target/heat-network-service-0.1.0.jar app.jar
VOLUME ["/data"]
ENV HEAT_DATA_DIR=/data/projects HEAT_TMP_DIR=/data/tmp
EXPOSE 8080
ENTRYPOINT ["java", "-Xmx2g", "-jar", "app.jar"]
