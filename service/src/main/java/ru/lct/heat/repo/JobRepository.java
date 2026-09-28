package ru.lct.heat.repo;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;
import ru.lct.heat.domain.Job;

public interface JobRepository extends JpaRepository<Job, String> {

    List<Job> findAllByProjectIdOrderByCreatedAtAsc(String projectId);

    List<Job> findAllByStatusIn(List<String> statuses);
}
