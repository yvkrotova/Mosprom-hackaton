// Общие помощники: экранирование, форматирование, подписи, плашки статусов, тосты, окна, меню.
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
  state: { active: "Продолжается", ended: "Завершён" },
  // escalated (старые данные «передано мастеру») показывается как подтверждённое.
  review: { unreviewed: "Не оценено", confirmed: "Подтверждено", false_positive: "Ложное", escalated: "Подтверждено" },
  priority: { low: "Низкий", medium: "Средний", high: "Высокий", critical: "Критический" },
  status: { idle: "Не запускалась", running: "Подключена", stopped: "Остановлена", error: "Ошибка", disconnected: "Нет связи" },
  action: {
    confirm: "Подтвердил нарушение", mark_false: "Отметил ложным", escalate: "Передал мастеру", comment: "Комментарий",
    reset: "Сбросил проверку", emergency: "Вызвал экстренные службы",
  },
};

export const label = (group, key) => LABELS[group]?.[key] ?? key ?? "—";

// Плашки — не кнопки: точка + текст (статус различается текстом, а не только цветом).
const REVIEW_PILL = { unreviewed: "pill-amber", confirmed: "pill-green", escalated: "pill-green", false_positive: "pill-gray" };
export const reviewPill = (status, cls = "") => `<span class="pill ${REVIEW_PILL[status] || "pill-gray"} ${cls}">${esc(label("review", status))}</span>`;
export const statePill = (st, cls = "") => `<span class="pill ${st === "active" ? "pill-red" : "pill-blue"} ${cls}">${esc(label("state", st))}</span>`;

// Оценка оператора: confirmed (в т. ч. «передано мастеру») | false | none — для подсветки строки.
export const verdictOf = (status) =>
  status === "false_positive" ? "false" : status === "confirmed" || status === "escalated" ? "confirmed" : "none";

// Две кнопки оценки в один клик; нажатая — текущая оценка. data-verdict ловит обработчик экрана.
export function verdictControl(ev, { large = false, busy = false } = {}) {
  const v = verdictOf(ev.review_status);
  const ok = "Подтверждено";
  return `<div class="verdict ${large ? "verdict-lg" : ""} ${busy ? "busy" : ""}" data-verdict-for="${esc(ev.id)}">
    ${v === "none" ? `<span class="v-state">Не оценено</span>` : ""}
    <div class="v-btns" role="group" aria-label="Оценка оператора">
      <button type="button" class="v-btn v-ok ${v === "confirmed" ? "on" : ""}" data-verdict="confirmed" aria-pressed="${v === "confirmed"}" ${busy ? "disabled" : ""}
        title="Нарушение подтверждено">${icon("check")}${ok}</button>
      <button type="button" class="v-btn v-bad ${v === "false" ? "on" : ""}" data-verdict="false_positive" aria-pressed="${v === "false"}" ${busy ? "disabled" : ""}
        title="Ложное срабатывание">${icon("x")}${large ? "Ложное срабатывание" : "Ложное"}</button>
    </div></div>`;
}

// Среда выполнения по фактическому detector_kind. Replay (…_replay) — предзаписанные результаты.
export const isLinq = (kind) => typeof kind === "string" && kind.startsWith("linq");
export const isReplay = (kind) => typeof kind === "string" && kind.endsWith("_replay");
export function envLabel(kind) {
  if (!kind) return { text: "Среда неизвестна", cls: "" };
  if (isReplay(kind) && isLinq(kind)) return { text: "Replay — предзаписанные результаты LinQ", cls: "replay" };
  if (isReplay(kind)) return { text: `Replay — запись (${kind.replace(/_replay$/, "")}, не LinQ)`, cls: "replay" };
  if (isLinq(kind)) return { text: "LinQ TPU", cls: "ok" };
  if (kind === "onnx_cpu" || kind === "onnx") return { text: "ONNX · CPU (не LinQ)", cls: "" };
  if (kind === "mock") return { text: "Заглушка (mock) · не LinQ", cls: "" };
  return { text: `${kind} · не LinQ`, cls: "" };
}

// --- Сценарии (справочник из GET /scenarios) ------------------------------------------------

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

// Кто нарушил: трек человека или зона целиком (track_id = null — например, предмет в проходе).
export const violatorText = (ev) => (ev.track_id == null ? "нарушение зоны (без привязки к человеку)" : `трек #${ev.track_id}`);

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
export function fmtHM(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
}
export const fmtDateTime = (iso) => (iso ? `${fmtDate(iso)} ${fmtTime(iso)}` : "—");

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

// Длительность эпизода: по таймлайну видео; у идущего — до последнего наблюдения.
export const episodeSeconds = (ev) => ev.video_ts_end - ev.video_ts_start;

// <input type="datetime-local"> ↔ ISO (местное время браузера).
export function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function fromLocalInput(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

// «24 события», «1 событие», «5 событий».
export function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

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

// Выпадающее меню у кнопки: items = [{ label, icon, run }]. Закрывается кликом мимо и Escape.
export function openMenu(anchor, items) {
  document.querySelector(".dropdown")?.remove();
  const menu = h(`<div class="dropdown" role="menu">${items.map((it, i) =>
    `<button role="menuitem" data-i="${i}">${it.icon ? icon(it.icon) : ""}${esc(it.label)}</button>`).join("")}</div>`);
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const left = Math.min(r.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.min(r.bottom + 6, window.innerHeight - menu.offsetHeight - 8)}px`;
  const close = () => { menu.remove(); document.removeEventListener("mousedown", outside, true); document.removeEventListener("keydown", key); };
  const outside = (e) => { if (!menu.contains(e.target) && e.target !== anchor) close(); };
  const key = (e) => e.key === "Escape" && close();
  for (const btn of $$("button", menu)) btn.onclick = (e) => { e.stopPropagation(); close(); items[Number(btn.dataset.i)].run(); };
  setTimeout(() => { document.addEventListener("mousedown", outside, true); document.addEventListener("keydown", key); });
}

// Подсказка «?»: title не показывается мгновенно и плохо читается — свой всплывающий текст.
export function helpIcon(text) {
  return `<span class="help" tabindex="0" data-tip="${esc(text)}" aria-label="${esc(text)}">${icon("help-circle")}</span>`;
}
let tipEl = null;
document.addEventListener("mouseover", (e) => {
  const target = e.target.closest?.("[data-tip]");
  if (!target) { tipEl?.remove(); tipEl = null; return; }
  if (tipEl?.dataset.for === target.dataset.tip) return;
  tipEl?.remove();
  tipEl = h(`<div class="tooltip-tip" data-for="${esc(target.dataset.tip)}">${esc(target.dataset.tip)}</div>`);
  document.body.append(tipEl);
  const r = target.getBoundingClientRect();
  tipEl.style.left = `${Math.min(r.left, window.innerWidth - tipEl.offsetWidth - 10)}px`;
  tipEl.style.top = `${r.bottom + 8 + tipEl.offsetHeight > window.innerHeight ? r.top - tipEl.offsetHeight - 8 : r.bottom + 8}px`;
});
