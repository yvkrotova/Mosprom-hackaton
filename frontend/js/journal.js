// «Журнал нарушений»: быстрые периоды ↔ поля «С»/«По», восемь совместных фильтров (без кнопки
// «Применить»), сортировка и счётчик по всей выборке (на сервере), пагинация, выгрузка CSV на
// сервере. Новые события не сбрасывают фильтры и страницу. Фильтры живут в localStorage.
import { api } from "./api.js";
import { mediaUrl } from "./config.js";
import { openEvent, rate } from "./event-modal.js";
import { hydrateIcons, icon } from "./icons.js";
import { on, sourceById, state } from "./state.js";
import {
  $, $$, confirmModal, episodeSeconds, esc, fmtDate, fmtDuration, fmtHM, fmtTime, fromLocalInput, label, plural,
  statePill, stored, store, toLocalInput, toast, toastError, verdictControl, verdictOf,
} from "./util.js";

const PAGE = 10;
const QUICK = { today: "Сегодня", day: "24 часа", week: "7 дней", all: "Всё время" };
const FILTERS = ["scenario_type", "source_id", "zone_id", "priority", "from", "to", "state", "review_status"];

// Исходное состояние: «Сегодня», остальные фильтры — «все», сначала новые, первая страница.
function initialFilters() {
  return { ...Object.fromEntries(FILTERS.map((k) => [k, ""])), ...quickRange("today"), quick: "today" };
}

function quickRange(kind) {
  const now = new Date();
  if (kind === "today") {
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const end = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59);
    return { from: start.toISOString(), to: end.toISOString() };
  }
  if (kind === "day") return { from: new Date(now - 864e5).toISOString(), to: now.toISOString() };
  if (kind === "week") return { from: new Date(now - 7 * 864e5).toISOString(), to: now.toISOString() };
  return { from: "", to: "" };
}

const jr = {
  filters: { ...initialFilters(), ...stored("journalFilters", {}) },
  order: stored("journalOrder", "desc"),
  offset: 0,
  page: null, // EventPage
  loading: false,
  error: null,
  selected: null,
  updatedAt: null,
  visible: false,
  scrollTop: 0,
};

function save() {
  store("journalFilters", jr.filters);
  store("journalOrder", jr.order);
}

function query() {
  const q = { order: jr.order };
  for (const k of FILTERS) if (jr.filters[k]) q[k] = jr.filters[k];
  return q;
}

const hasFilters = () => FILTERS.some((k) => jr.filters[k] && !["from", "to"].includes(k)) || jr.filters.quick !== "all";

// --- Загрузка -----------------------------------------------------------------------------

let seq = 0;
async function load({ quiet = false } = {}) {
  const my = ++seq;
  if (!quiet) { jr.loading = true; renderBody(); }
  try {
    const page = await api.events({ ...query(), limit: PAGE, offset: jr.offset });
    if (my !== seq) return;
    // Страница опустела (события удалили или фильтр сузился) — на последнюю непустую.
    if (!page.items.length && page.total && jr.offset >= page.total) {
      jr.offset = Math.max(0, Math.floor((page.total - 1) / PAGE) * PAGE);
      return load({ quiet });
    }
    jr.page = page;
    jr.error = null;
    jr.updatedAt = new Date();
  } catch (err) {
    if (my !== seq) return;
    jr.error = err.message;
  }
  jr.loading = false;
  renderBody();
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => jr.visible && load({ quiet: true }), 1200);
}

// --- Отрисовка ----------------------------------------------------------------------------

function renderShell(root) {
  root.innerHTML = `
    <div class="panel page-head">
      <div><h1>Журнал нарушений</h1><p>История событий и результаты проверки</p></div>
      <div class="head-actions">
        <button class="btn btn-outline-primary" id="jr-csv">${icon("download")}Скачать CSV</button>
        <button class="btn btn-danger" id="jr-clear" title="Удалить все события журнала">${icon("trash")}Очистить всё</button>
        <span class="updated" id="jr-updated"></span>
      </div>
    </div>
    <div class="panel toolbar">
      <div class="seg" id="jr-quick">${Object.entries(QUICK).map(([k, v]) => `<button data-quick="${k}">${v}</button>`).join("")}</div>
      <span class="spacer"></span>
      <button class="btn" id="jr-reset">${icon("refresh")}Сбросить фильтры</button>
    </div>
    <div class="panel filters">
      <label class="field"><span>Тип нарушения</span><select class="select" data-f="scenario_type"></select></label>
      <label class="field"><span>Камера</span><select class="select" data-f="source_id"></select></label>
      <label class="field"><span>Зона</span><select class="select" data-f="zone_id"></select></label>
      <label class="field"><span>Приоритет</span><select class="select" data-f="priority">
        <option value="">Любой</option>${["critical", "high", "medium", "low"].map((p) => `<option value="${p}">${label("priority", p)}</option>`).join("")}</select></label>
      <label class="field date-input"><span>С (местное время)</span><input class="input" type="datetime-local" data-f="from" /><span class="error-text" data-err="from"></span></label>
      <label class="field date-input"><span>По</span><input class="input" type="datetime-local" data-f="to" /><span class="error-text" data-err="to"></span></label>
      <label class="field"><span>Состояние эпизода</span><select class="select" data-f="state">
        <option value="">Любое</option><option value="active">${label("state", "active")}</option><option value="ended">${label("state", "ended")}</option></select></label>
      <label class="field"><span>Проверка оператора</span><select class="select" data-f="review_status">
        <option value="">Любая</option><option value="unreviewed">Не оценено</option><option value="confirmed">Подтверждено</option><option value="false_positive">Ложное</option></select></label>
    </div>
    <div class="panel table-panel">
      <div class="table-head"><b id="jr-total"></b>
        <button class="sort-btn" id="jr-sort">${icon("sort")}<span></span></button></div>
      <div id="jr-body"></div>
      <div class="pager" id="jr-pager"></div>
    </div>
    <div class="footnote-row">${icon("info")}Нажмите на событие, чтобы посмотреть эпизод и подробности</div>`;
  hydrateIcons(root);

  for (const el of $$("[data-f]", root)) {
    el.onchange = () => {
      const key = el.dataset.f;
      if (el.type === "datetime-local") return onDateChange(key, el.value);
      jr.filters[key] = el.value;
      if (key === "source_id") jr.filters.zone_id = "";
      changed();
    };
  }
  for (const b of $$("[data-quick]", root)) {
    b.onclick = () => {
      Object.assign(jr.filters, quickRange(b.dataset.quick), { quick: b.dataset.quick });
      changed();
    };
  }
  $("#jr-reset").onclick = () => { jr.filters = initialFilters(); jr.order = "desc"; changed(); };
  $("#jr-sort").onclick = () => { jr.order = jr.order === "desc" ? "asc" : "desc"; changed(); };
  $("#jr-csv").onclick = exportCsv;
  $("#jr-clear").onclick = clearJournal;
}

// Дата: применяется только корректная, «По» не раньше «С» — иначе пояснение у поля.
function onDateChange(key, value) {
  const iso = fromLocalInput(value);
  const err = $(`[data-err="${key}"]`);
  const input = $(`[data-f="${key}"]`);
  if (value && iso === null) {
    err.textContent = "Неверная дата";
    input.classList.add("invalid");
    return;
  }
  const next = { ...jr.filters, [key]: iso || "" };
  if (next.from && next.to && new Date(next.to) < new Date(next.from)) {
    err.textContent = key === "to" ? "«По» не может быть раньше «С»" : "«С» не может быть позже «По»";
    input.classList.add("invalid");
    return;
  }
  for (const k of ["from", "to"]) {
    $(`[data-err="${k}"]`).textContent = "";
    $(`[data-f="${k}"]`).classList.remove("invalid");
  }
  jr.filters = { ...next, quick: matchQuick(next) };
  changed();
}

// Если границы совпали с быстрым периодом — подсвечиваем его, иначе ни один.
function matchQuick(f) {
  if (!f.from && !f.to) return "all";
  for (const k of ["today"]) {
    const r = quickRange(k);
    if (toLocalInput(r.from) === toLocalInput(f.from) && toLocalInput(r.to) === toLocalInput(f.to)) return k;
  }
  return "";
}

function changed() {
  jr.offset = 0;
  save();
  renderControls();
  load();
}

function renderControls() {
  if (!$("#jr-quick")) return;
  for (const b of $$("[data-quick]")) b.classList.toggle("active", b.dataset.quick === jr.filters.quick);
  const scen = $('[data-f="scenario_type"]');
  scen.innerHTML = `<option value="">Все типы</option>` + state.scenarios.map((s) => `<option value="${s.scenario_type}">${esc(s.title)}</option>`).join("");
  const src = $('[data-f="source_id"]');
  src.innerHTML = `<option value="">Все камеры</option>` + state.sources.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  const zones = jr.filters.source_id ? state.zones[jr.filters.source_id] || [] : Object.values(state.zones).flat();
  $('[data-f="zone_id"]').innerHTML = `<option value="">Все зоны</option><option value="none">Весь кадр (без зоны)</option>`
    + zones.map((z) => `<option value="${z.id}">${esc(z.name)}${jr.filters.source_id ? "" : ` · ${esc(sourceById(z.source_id)?.name || "")}`}</option>`).join("");
  for (const el of $$("[data-f]")) {
    const v = jr.filters[el.dataset.f] || "";
    el.value = el.type === "datetime-local" ? toLocalInput(v) : v;
  }
  $("#jr-sort span").textContent = jr.order === "desc" ? "Сначала новые" : "Сначала старые";
}

function renderBody() {
  const body = $("#jr-body");
  if (!body) return;
  const scroller = $("#content");
  const keepScroll = scroller.scrollTop;
  $("#jr-updated").textContent = jr.updatedAt ? `Обновлено в ${fmtHM(jr.updatedAt.toISOString())}` : "";
  const page = jr.page;
  const total = page?.total;
  $("#jr-total").textContent = total == null ? "" : `Найдено ${total} ${plural(total, "событие", "события", "событий")}`;
  if (jr.loading && !page) {
    body.innerHTML = `<div style="padding:0 18px 12px">${[1, 2, 3, 4].map(() => `<div class="skeleton" style="height:82px;margin-top:10px"></div>`).join("")}</div>`;
    $("#jr-pager").innerHTML = "";
    return;
  }
  if (jr.error) {
    body.innerHTML = `<div class="empty">${icon("alert-triangle")}<b>Журнал не загружен</b><span>${esc(jr.error)}</span>
      <button class="btn btn-sm" data-retry>${icon("refresh")}Повторить</button></div>`;
    $("[data-retry]", body).onclick = () => load();
    $("#jr-pager").innerHTML = "";
    return;
  }
  if (!page.items.length) {
    body.innerHTML = hasFilters() && page.total === 0
      ? `<div class="empty">${icon("list")}<b>По выбранным условиям событий не найдено</b>
          <button class="btn btn-sm" data-reset>${icon("refresh")}Сбросить фильтры</button></div>`
      : `<div class="empty">${icon("check-circle")}<b>Событий пока нет</b></div>`;
    const reset = $("[data-reset]", body);
    if (reset) reset.onclick = () => $("#jr-reset").click();
    $("#jr-pager").innerHTML = "";
    return;
  }
  body.innerHTML = `<div class="table-wrap"><table class="table">
    <thead><tr><th>Кадр</th><th>Начало</th><th>Камера / зона</th><th>Нарушение</th><th>Длительность</th><th>Уверенность</th><th>Состояние</th><th>Оценка оператора</th><th></th></tr></thead>
    <tbody>${page.items.map(row).join("")}</tbody></table></div>`;
  for (const tr of $$("tbody tr", body)) {
    tr.onclick = (e) => {
      const cur = e.currentTarget; // строка могла быть перерисована после оценки
      const btn = e.target.closest("[data-verdict]");
      if (btn) return onVerdict(cur.dataset.id, btn.dataset.verdict, btn);
      jr.selected = cur.dataset.id;
      for (const r of $$("#jr-body tbody tr")) r.classList.toggle("selected", r === cur);
      openEvent(cur.dataset.id);
    };
  }
  renderPager();
  scroller.scrollTop = keepScroll;
}

// Оценка в один клик: строка меняется сразу (оптимистично), при ошибке — возвращается как было.
const busy = new Set();
async function onVerdict(id, verdict, btn) {
  if (btn.classList.contains("on") || busy.has(id)) return;
  const items = jr.page?.items || [];
  const i = items.findIndex((x) => x.id === id);
  if (i < 0) return;
  const before = items[i];
  busy.add(id);
  items[i] = { ...before, review_status: verdict };
  replaceRow(items[i]);
  const updated = await rate(id, verdict);
  busy.delete(id);
  const j = jr.page.items.findIndex((x) => x.id === id);
  if (j >= 0) {
    jr.page.items[j] = updated ? { ...jr.page.items[j], ...updated } : before;
    replaceRow(jr.page.items[j]);
  }
  // Фильтр «Проверка оператора» мог исключить строку — пересчитаем выборку тихо.
  if (updated && jr.filters.review_status) scheduleRefresh();
}

function replaceRow(e) {
  const tr = $(`#jr-body tr[data-id="${e.id}"]`);
  if (!tr) return;
  const tmp = document.createElement("tbody");
  tmp.innerHTML = row(e);
  const fresh = tmp.firstElementChild;
  fresh.onclick = tr.onclick;
  tr.replaceWith(fresh);
}

function row(e) {
  const zone = e.zone_name || (e.zone_id ? "удалённая зона" : "Весь кадр");
  const cam = e.source_name || "удалённая камера";
  const rv = { confirmed: "rv-confirmed", false: "rv-false", none: "rv-none" }[verdictOf(e.review_status)];
  return `<tr data-id="${e.id}" class="${rv} ${e.id === jr.selected ? "selected" : ""}">
    <td><div class="thumb">${e.thumbnail_url ? `<img src="${esc(mediaUrl(e.thumbnail_url))}" alt="" loading="lazy" />` : "Кадр недоступен"}</div></td>
    <td class="t-date">${fmtDate(e.started_at)}<br>${fmtTime(e.started_at)}</td>
    <td class="t-cam"><b class="ellipsis" title="${esc(cam)}">${esc(cam)}</b><span class="ellipsis" title="${esc(zone)}">${esc(zone)}</span></td>
    <td>${esc(label("scenario", e.scenario_type))}${e.emergency_at ? `<div class="sub" style="color:#ff8585" title="${esc(e.emergency_service)}">${icon("phone")} Вызваны службы</div>` : ""}</td>
    <td class="nowrap" ${e.state === "active" ? `data-live-duration="${e.id}"` : ""}>${fmtDuration(liveSeconds(e))}</td>
    <td>${Math.round(e.confidence * 100)}%</td>
    <td>${statePill(e.state)}</td>
    <td>${verdictControl(e, { busy: busy.has(e.id) })}</td>
    <td><span class="row-go" aria-hidden="true">${icon("chevron-right")}</span></td></tr>`;
}

// Длительность идущего эпизода растёт: по видео до последнего наблюдения плюс время с него.
function liveSeconds(e) {
  if (e.state !== "active") return episodeSeconds(e);
  const since = (Date.now() - new Date(e.last_seen_at)) / 1000;
  return episodeSeconds(e) + Math.max(0, since);
}

function renderPager() {
  const page = jr.page;
  const pages = Math.max(1, Math.ceil(page.total / PAGE));
  const current = Math.floor(jr.offset / PAGE) + 1;
  const nums = new Set([1, pages, current - 1, current, current + 1].filter((p) => p >= 1 && p <= pages));
  const sorted = [...nums].sort((a, b) => a - b);
  let html = "";
  sorted.forEach((p, i) => {
    if (i && p - sorted[i - 1] > 1) html += `<span class="gap">…</span>`;
    html += `<button data-page="${p}" class="${p === current ? "active" : ""}">${p}</button>`;
  });
  const first = jr.offset + 1, last = jr.offset + page.items.length;
  $("#jr-pager").innerHTML = `<span class="muted">${first}–${last} из ${page.total}</span>
    <div class="pager-pages"><button data-page="${current - 1}" ${current === 1 ? "disabled" : ""}>${icon("arrow-left")} Назад</button>
    ${html}<button data-page="${current + 1}" ${current === pages ? "disabled" : ""}>Вперёд ${icon("arrow-right")}</button></div>`;
  for (const b of $$("[data-page]", $("#jr-pager"))) {
    b.onclick = () => { jr.offset = (Number(b.dataset.page) - 1) * PAGE; load(); $("#content").scrollTop = 0; };
  }
}

// «Очистить всё»: удаляет ВСЕ события (не только отфильтрованные) с историей и кадрами. Необратимо.
async function clearJournal() {
  let total = null;
  try {
    total = (await api.events({ limit: 1 })).total;
  } catch { /* число не обязательно */ }
  if (total === 0) return toast("Журнал уже пуст", "info", { timeout: 2000 });
  const ok = await confirmModal(
    "Очистить журнал?",
    `Будут удалены все события${total != null ? ` (${total})` : ""} — независимо от фильтров, вместе с кадрами и историей оценок. `
      + "Отменить это нельзя. Скачайте CSV заранее, если данные нужны.",
    { okLabel: "Удалить все события" },
  );
  if (!ok) return;
  const btn = $("#jr-clear");
  btn.disabled = true;
  try {
    const { deleted } = await api.clearEvents();
    jr.offset = 0;
    jr.selected = null;
    await load();
    toast(`Журнал очищен: удалено ${deleted} ${plural(deleted, "событие", "события", "событий")}`, "success", { timeout: 3500 });
  } catch (err) {
    toastError(err);
  } finally {
    btn.disabled = false;
  }
}

// Выгрузка всей выборки на сервере; повторное нажатие во время выгрузки игнорируется.
async function exportCsv() {
  const btn = $("#jr-csv");
  if (btn.classList.contains("loading")) return;
  btn.classList.add("loading");
  btn.innerHTML = `${icon("refresh")}Готовим CSV…`;
  try {
    const blob = await api.exportCsv({ ...query(), tz_offset_min: -new Date().getTimezoneOffset() });
    const url = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement("a"), { href: url, download: `журнал_${fmtDate(new Date().toISOString())}.csv` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast("CSV выгружен", "success", { timeout: 2500 });
  } catch (err) {
    toastError(err);
  } finally {
    btn.classList.remove("loading");
    btn.innerHTML = `${icon("download")}Скачать CSV`;
  }
}

// Переход из других экранов: { event, source_id, from, to }.
function openWith(params = {}) {
  if (params.from !== undefined || params.source_id !== undefined) {
    jr.filters = { ...initialFilters(), quick: "", from: params.from || "", to: params.to || "", source_id: params.source_id || "" };
    jr.filters.quick = matchQuick(jr.filters);
    jr.offset = 0;
    save();
  }
  if (params.event) jr.selected = params.event;
}

export function initJournal(root) {
  renderShell(root);
  on("loaded", () => { renderControls(); if (jr.visible) load(); });
  on("sources", renderControls);
  on("zones", renderControls);
  on("events-cleared", () => { jr.page = null; jr.offset = 0; if (jr.visible) load(); });
  // Удалённое событие пропадает из таблицы сразу, затем выборка и счётчик пересчитываются.
  on("event-deleted", (id) => {
    if (!jr.page) return;
    jr.page.items = jr.page.items.filter((e) => e.id !== id);
    jr.page.total = Math.max(0, jr.page.total - 1);
    renderBody();
    if (jr.visible) scheduleRefresh();
  });
  on("event", (ev) => {
    const items = jr.page?.items;
    const i = items?.findIndex((e) => e.id === ev.id) ?? -1;
    if (i >= 0) {
      items[i] = { ...items[i], ...ev }; // обновление эпизода — та же строка, без перезагрузки
      renderBody();
    } else if (jr.visible) {
      scheduleRefresh(); // новое событие: счётчик и страница обновятся, фильтры и страница — те же
    }
  });
  // Длительность идущих эпизодов на экране обновляется раз в секунду.
  setInterval(() => {
    if (!jr.visible || !jr.page) return;
    for (const td of $$("[data-live-duration]")) {
      const e = jr.page.items.find((x) => x.id === td.dataset.liveDuration);
      if (e) td.textContent = fmtDuration(liveSeconds(e));
    }
  }, 1000);
}

export function setJournalVisible(visible, params) {
  jr.visible = visible;
  if (!visible) return;
  if (params) openWith(params);
  if (jr.filters.quick && jr.filters.quick !== "all") Object.assign(jr.filters, quickRange(jr.filters.quick)); // «Сегодня» — на сегодня
  renderControls();
  load({ quiet: !!jr.page });
  if (params?.event) openEvent(params.event);
}
