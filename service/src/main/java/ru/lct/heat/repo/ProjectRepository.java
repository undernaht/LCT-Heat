package ru.lct.heat.repo;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;
import ru.lct.heat.domain.Project;

public interface ProjectRepository extends JpaRepository<Project, String> {

    List<Project> findAllByOrderByCreatedAtDesc();
}
