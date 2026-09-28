import { Component, StrictMode, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles.css";

/** Последний рубеж: ошибка рендера не должна оставлять белый экран на защите —
 *  показываем причину и кнопку перезагрузки. */
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Ошибка интерфейса:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="fatal">
        <h1>Интерфейс упал</h1>
        <p className="error">{String(this.state.error.message ?? this.state.error)}</p>
        <p className="note">
          Данные на сервере целы: проекты и результаты расчётов сохранены. Обновите страницу;
          если ошибка повторяется на конкретном проекте — удалите его и загрузите файл заново.
        </p>
        <button className="primary" onClick={() => window.location.reload()}>
          Обновить страницу
        </button>
      </div>
    );
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
