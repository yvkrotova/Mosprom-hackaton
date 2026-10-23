// Адрес backend. По умолчанию — тот же сервер, что отдал страницу (backend сам раздаёт фронт).
// Если фронт открыт отдельно (файлом или dev-сервером на :5173), backend ищется на :8000.
// Переопределить можно до загрузки app.js: <script>window.LINQ_API_BASE = "http://host:port"</script>.
const standalone = location.protocol === "file:" || location.port === "5173";

export const API_BASE = window.LINQ_API_BASE ?? (standalone ? "http://localhost:8000" : "");

export const wsUrl = (path) => (API_BASE || location.origin).replace(/^http/, "ws") + path;

export const mediaUrl = (path) => (path ? API_BASE + path : "");
