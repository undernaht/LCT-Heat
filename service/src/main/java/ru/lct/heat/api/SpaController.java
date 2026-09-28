package ru.lct.heat.api;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;

/**
 * Фронтенд — одностраничное приложение из classpath:/static. Любой путь, чей
 * первый сегмент не содержит точки и не принадлежит API, ресурсам сборки или
 * Swagger, отдаёт index.html, чтобы работали адреса внутри SPA.
 */
@Controller
public class SpaController {

    private static final String SPA = "^(?!api$|assets$|swagger-ui$|v3$|webjars$|error$)[^\\.]*";

    @GetMapping({"/", "/{path:" + SPA + "}", "/{path:" + SPA + "}/**"})
    public String index() {
        return "forward:/index.html";
    }
}
