import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // MapLibre поднимает свой web worker через `new Worker(new URL('./worker.js',
  // import.meta.url))`. Предбандлинг зависимостей ломает это разрешение пути:
  // воркер стартует, но пустой и на сообщения не отвечает. Внешне это выглядит
  // как полностью пустая карта — стиль загружен, слои и источники добавлены,
  // WebGL исправен, но у КАЖДОГО источника `loaded() === false`, потому что
  // GeoJSON парсится именно в воркере. Ошибок при этом нет никаких.
  optimizeDeps: { exclude: ["maplibre-gl"] },
  server: {
    port: 5173,
    // По умолчанию Vite слушает только на ::1, и браузер, резолвящий localhost
    // в 127.0.0.1, получает ERR_CONNECTION_REFUSED. Слушаем на всех адресах.
    host: true,
    // Прокси на бэкенд: фронтенд ходит по относительным путям, CORS не нужен.
    proxy: {
      "/api": { target: "http://127.0.0.1:8010", changeOrigin: true },
    },
  },
});
