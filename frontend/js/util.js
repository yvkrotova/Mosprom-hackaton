// Общие помощники: экранирование, форматирование, подписи enum'ов, тосты, модальные окна.
import { icon } from "./icons.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export function h(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

// --- Подписи -------------------------------------------------------------------------------

export const LABELS = {
  scenario: { danger_zone: "Человек в опасной зоне", fall_immobility: "Падение / неподвижность" },
  state: { active: "Идёт", ended: "Завершено" },
  review: { unreviewed: "Не проверено", confirmed: "Подтверждено", false_positive: "Ложное", escalated: "Передано мастеру" },
  priority: { low: "Низкий", medium: "Средний", high: "Высокий", critical: "Критический" },
  status: { idle: "Не запускался", running: "Анализ идёт", stopped: "Остановлен", error: "Ошибка", disconnected: "Нет связи" },
  action: { confirm: "Подтвердил", mark_false: "Отметил ложным", escalate: "Передал мастеру", comment: "Комментарий", reset: "Сбросил проверку" },
  detector: {
    mock: "MOCK · заглушка", replay: "REPLAY · файл детекций",
    onnx_cpu: "ONNX · CPU (не LinQ)", onnx: "ONNX · CPU (не LinQ)", linq: "LinQ NPU",
  },
};

export const label = (group, key) => LABELS[group]?.[key] ?? key ?? "—";

// Настоящий замер на ускорителе — только detector_kind = "linq". Остальное помечаем явно.
export const isLinq = (kind) => kind === "linq";

// Та же таблица переходов, что в backend (docs/CONTRACT.md).
export const TRANSITIONS = {
  confirm: ["unreviewed", "escalated"],
  mark_false: ["unreviewed", "escalated", "confirmed"],
  escalate: ["unreviewed", "confirmed"],
  reset: ["confirmed", "false_positive", "escalated"],
};
export const canDo = (action, status) => TRANSITIONS[action]?.includes(status) ?? true;

// --- Время и числа -------------------------------------------------------------------------

const pad = (n) => String(n).padStart(2, "0");

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${fmtTime(iso)}`;
}

export function fmtDuration(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  return m ? `${m} мин ${pad(s % 60)} с` : `${s} с`;
}

export function fmtNum(value, digits = 1, suffix = "") {
  if (value == null || Number.isNaN(value)) return "—";
  return Number(value).toFixed(digits).replace(".", ",") + suffix;
}

export const pct = (value) => (value == null ? "—" : `${Math.round(value * 100)}%`);

// Длительность эпизода: по таймлайну видео (не зависит от скорости обработки).
export const episodeSeconds = (ev) => ev.video_ts_end - ev.video_ts_start;

// --- Локальные настройки (переживают перезагрузку страницы) --------------------------------

export function stored(key, fallback) {
  try {
    const raw = localStorage.getItem("linq." + key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function store(key, value) {
  try { localStorage.setItem("linq." + key, JSON.stringify(value)); } catch { /* приватный режим */ }
}

// --- Тосты ---------------------------------------------------------------------------------

export function toast(message, kind = "info", { action, timeout = 5000 } = {}) {
  const icons = { info: "info", success: "check-circle", error: "alert-triangle", alarm: "siren" };
  const el = h(`<div class="toast toast-${kind}" role="status">
      ${icon(icons[kind] || "info")}<div class="toast-body">${esc(message)}</div></div>`);
  if (action) {
    const btn = h(`<button class="btn btn-sm">${esc(action.label)}</button>`);
    btn.onclick = () => { action.run(); el.remove(); };
    el.append(btn);
  }
  const close = h(`<button class="toast-close" aria-label="Закрыть">${icon("x")}</button>`);
  close.onclick = () => el.remove();
  el.append(close);
  $("#toasts").append(el);
  if (timeout) setTimeout(() => el.remove(), timeout);
  return el;
}

export function toastError(err) {
  toast(err?.message || String(err), "error", { timeout: 8000 });
}

// --- Модальные окна ------------------------------------------------------------------------

export function openModal(contentHtml, { wide = false, onClose } = {}) {
  const backdrop = h(`<div class="modal-backdrop"><div class="modal ${wide ? "modal-wide" : ""}" role="dialog" aria-modal="true">
      <button class="modal-close" aria-label="Закрыть">${icon("x")}</button>${contentHtml}</div></div>`);
  const close = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKey);
    onClose?.();
  };
  const onKey = (e) => e.key === "Escape" && close();
  backdrop.addEventListener("mousedown", (e) => e.target === backdrop && close());
  $(".modal-close", backdrop).onclick = close;
  document.addEventListener("keydown", onKey);
  document.body.append(backdrop);
  return { root: $(".modal", backdrop), close };
}

export function promptModal(title, { value = "", placeholder = "", okLabel = "Сохранить" } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const { root, close } = openModal(`<h3 class="modal-title">${esc(title)}</h3>
        <input class="input" value="${esc(value)}" placeholder="${esc(placeholder)}" />
        <div class="modal-actions"><button class="btn" data-cancel>Отмена</button>
        <button class="btn btn-primary" data-ok>${esc(okLabel)}</button></div>`, { onClose: () => !done && resolve(null) });
    const input = $("input", root);
    const ok = () => { done = true; resolve(input.value.trim()); close(); };
    $("[data-ok]", root).onclick = ok;
    $("[data-cancel]", root).onclick = close;
    input.addEventListener("keydown", (e) => e.key === "Enter" && ok());
    setTimeout(() => input.select(), 0);
  });
}

export function confirmModal(title, text, { okLabel = "Удалить", danger = true } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const { root, close } = openModal(`<h3 class="modal-title">${esc(title)}</h3><p class="muted">${esc(text)}</p>
        <div class="modal-actions"><button class="btn" data-cancel>Отмена</button>
        <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-ok>${esc(okLabel)}</button></div>`,
      { onClose: () => !done && resolve(false) });
    $("[data-ok]", root).onclick = () => { done = true; resolve(true); close(); };
    $("[data-cancel]", root).onclick = close;
  });
}
