package ru.lct.heat.api;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.multipart.MaxUploadSizeExceededException;

/** Ошибки — в той же форме, что у Python-сервиса: {"detail": "..."}. */
@RestControllerAdvice
public class ApiExceptionHandler {

    @ExceptionHandler(ProjectController.NotFoundException.class)
    public ResponseEntity<Dto.Error> notFound(ProjectController.NotFoundException exc) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND).body(new Dto.Error(exc.getMessage()));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Dto.Error> badRequest(IllegalArgumentException exc) {
        return ResponseEntity.badRequest().body(new Dto.Error(exc.getMessage()));
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Dto.Error> invalid(MethodArgumentNotValidException exc) {
        return ResponseEntity.badRequest().body(new Dto.Error("недопустимые параметры запроса"));
    }

    @ExceptionHandler(MaxUploadSizeExceededException.class)
    public ResponseEntity<Dto.Error> tooLarge(MaxUploadSizeExceededException exc) {
        return ResponseEntity.status(HttpStatus.PAYLOAD_TOO_LARGE).body(new Dto.Error("файл больше 3 ГБ"));
    }
}
