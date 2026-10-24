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
  scenario: {}, // заполняется из GET /scenarios (setScenarios) — сценарии во фронте не перечисляются
  state: { active: "Идёт", ended: "Завершено" },
  review: { unreviewed: "Не проверено", confirmed: "Подтверждено", false_positive: "Ложное", escalated: "Передано мастеру" },
  priority: { low: "Низкий", medium: "Средний", high: "Высокий", critical: "Критический" },
  status: { idle: "Не запускался", running: "Анализ идёт", stopped: "Остановлен", error: "Ошибка", disconnected: "Нет связи" },
  action: { confirm: "Подтвердил", mark_false: "Отметил ложным", escalate: "Передал мастеру", comment: "Комментарий", reset: "Сбросил проверку" },
  detector: {
    mock: "MOCK · заглушка", replay: "REPLAY · файл детекций",
    onnx_cpu: "ONNX · CPU (не LinQ)", onnx: "ONNX · CPU (не LinQ)",
    linq: "LinQ TPU", linq_tpu: "LinQ TPU", linq_tpu_replay: "LinQ TPU (запись)", onnx_cpu_replay: "CPU (запись)",
  },
};

export const label = (group, key) => LABELS[group]?.[key] ?? key ?? "—";

// Инференс на ускорителе — detector_kind linq / linq_tpu / linq_tpu_replay (запись с TPU). Остальное — явно «не LinQ».
export const isLinq = (kind) => typeof kind === "string" && kind.startsWith("linq");

// Справочник сценариев из GET /scenarios: подписи, иконки, формы параметров.
const SCENARIOS = new Map();
export function setScenarios(list) {
  SCENARIOS.clear();
  for (const info of list || []) {
    SCENARIOS.set(info.scenario_type, info);
    LABELS.scenario[info.scenario_type] = info.title;
  }
}
export const scenarioInfo = (type) => SCENARIOS.get(type) || null;
// Иконка типа нарушения (из API; неизвестный сценарий — общий треугольник).
export const scenarioIcon = (type) => SCENARIOS.get(type)?.icon || "alert-triangle";

// Скорость трека (track.speed): «ростов человека в секунду», не м/с.
export const fmtSpeed = (v) => (v == null ? "—" : `${fmtNum(v, 1)} роста/с`);

// Данные сценария из Event.details — по описанию details_fields из GET /scenarios (одинаково для
// всех сценариев): [{ key, title, now, max }], now/max уже с единицей. Пустые поля пропускаются.
export function detailItems(ev) {
  const fields = SCENARIOS.get(ev.scenario_type)?.details_fields || [];
  const fmt = (v, unit) => (typeof v === "number" ? fmtNum(v, Number.isInteger(v) ? 0 : 1) : String(v)) + (unit ? (unit === "%" ? "%" : ` ${unit}`) : "");
  return fields
    .filter((f) => ev.details?.[f.key] != null || ev.details?.[`${f.key}_max`] != null)
    .map((f) => {
      const now = ev.details[f.key], max = ev.details[`${f.key}_max`];
      return { key: f.key, title: f.title, now: now != null ? fmt(now, f.unit) : null, max: max != null ? fmt(max, f.unit) : null };
    });
}

// Коротко для строки журнала и ленты: «Скорость макс 2,4 роста/с · Предметов 1».
export const detailsShort = (ev) =>
  detailItems(ev).map((d) => `${d.title} ${d.max != null && d.max !== d.now ? "макс " + d.max : d.now ?? d.max}`).join(" · ");

// Кто нарушил: трек человека или зона целиком (track_id = null — например, предмет в проходе).
export const violatorText = (ev) => (ev.track_id == null ? "нарушение зоны" : `трек #${ev.track_id}`);

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
