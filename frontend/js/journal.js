// Вкладка «Журнал»: таблица событий из GET /events, фильтры в URL (#journal?source_id=…&from=…), CSV.
import { api } from "./api.js";
import { mediaUrl } from "./config.js";
import { openEvent, priorityBadge, reviewBadge, stateBadge } from "./event-modal.js";
import { hydrateIcons, icon } from "./icons.js";
import { on, state } from "./state.js";
import { $, $$, episodeSeconds, esc, fmtDateTime, fmtDuration, label, toast, toastError } from "./util.js";

const PAGE = 25;
const FILTERS = ["scenario_type", "source_id", "zone_id", "from", "to", "state", "review_status", "priority"];
const QUICK = { today: "Сегодня", day: "24 часа", week: "7 дней", all: "Всё время" };

const jr = {
  filters: Object.fromEntries(FILTERS.map((k) => [k, ""])),
  offset: 0,
  total: 0,
  visible: false,
  loading: false,
};

// --- URL ------------------------------------------------------------------------------------

function readUrl() {
  const [, query = ""] = location.hash.split("?");
  const params = new URLSearchParams(query);
  for (const k of FILTERS) jr.filters[k] = params.get(k) || "";
  jr.offset = Math.max(0, Number(params.get("offset")) || 0);
}

function writeUrl() {
  const params = new URLSearchParams();
  for (const k of FILTERS) if (jr.filters[k]) params.set(k, jr.filters[k]);
  if (jr.offset) params.set("offset", jr.offset);
  const qs = params.toString();
  history.replaceState(null, "", "#journal" + (qs ? "?" + qs : ""));
}

// ISO UTC <-> значение <input type="datetime-local"> в местном времени.
function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

const fromLocalInput = (value) => (value ? new Date(value).toISOString() : "");

// --- Разметка -------------------------------------------------------------------------------

export function initJournal(root) {
  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title"><i data-icon="database"></i>Журнал нарушений</h2>
          <p class="card-sub">Одна строка — один эпизод нарушения. Время — местное. Фильтры сохраняются в адресе страницы: ссылкой можно поделиться.</p>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <span class="muted" id="jr-total"></span>
          <button class="btn btn-sm" id="jr-reset"><i data-icon="undo"></i>Сбросить фильтры</button>
          <button class="btn btn-sm btn-primary" id="jr-csv"><i data-icon="download"></i>CSV</button>
        </div>
      </div>
      <div class="toolbar" style="margin-bottom:10px">
        <div class="seg" id="jr-quick">${Object.entries(QUICK).map(([k, v]) => `<button data-quick="${k}">${v}</button>`).join("")}</div>
      </div>
      <div class="filters">
        <label class="field"><span>Тип нарушения</span><select class="select" data-f="scenario_type"><option value="">Все типы</option>
          ${Object.keys({ danger_zone: 0, fall_immobility: 0 }).map((k) => `<option value="${k}">${label("scenario", k)}</option>`).join("")}</select></label>
        <label class="field"><span>Камера</span><select class="select" data-f="source_id"></select></label>
        <label class="field"><span>Зона</span><select class="select" data-f="zone_id"></select></label>
        <label class="field"><span>С (местное время)</span><input class="input" type="datetime-local" data-f="from" /></label>
        <label class="field"><span>По</span><input class="input" type="datetime-local" data-f="to" /></label>
        <label class="field"><span>Состояние эпизода</span><select class="select" data-f="state"><option value="">Любое</option>
          <option value="active">Идёт</option><option value="ended">Завершено</option></select></label>
        <label class="field"><span>Проверка оператора</span><select class="select" data-f="review_status"><option value="">Любая</option>
          ${["unreviewed", "confirmed", "false_positive", "escalated"].map((k) => `<option value="${k}">${label("review", k)}</option>`).join("")}</select></label>
        <label class="field"><span>Приоритет</span><select class="select" data-f="priority"><option value="">Любой</option>
          ${["critical", "high", "medium", "low"].map((k) => `<option value="${k}">${label("priority", k)}</option>`).join("")}</select></label>
      </div>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>Кадр</th><th>Начало</th><th>Камера / зона</th><th>Нарушение</th><th>Длительность</th><th>Уверенность</th><th>Состояние</th><th>Проверка</th></tr></thead>
        <tbody id="jr-body"></tbody>
      </table></div>
      <div class="pager">
        <span id="jr-pager"></span>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm" id="jr-prev"><i data-icon="chevron-left"></i>Назад</button>
          <button class="btn btn-sm" id="jr-next">Вперёд<i data-icon="chevron-right"></i></button>
        </div>
      </div>
    </div>`;
  hydrateIcons(root);

  for (const el of $$("[data-f]", root)) {
    el.onchange = () => {
      const key = el.dataset.f;
      jr.filters[key] = el.type === "datetime-local" ? fromLocalInput(el.value) : el.value;
      if (key === "source_id") jr.filters.zone_id = "";
      jr.offset = 0;
      apply();
    };
  }
  for (const btn of $$("[data-quick]", root)) btn.onclick = () => setQuick(btn.dataset.quick);
  $("#jr-reset").onclick = () => { for (const k of FILTERS) jr.filters[k] = ""; jr.offset = 0; apply(); };
  $("#jr-prev").onclick = () => { jr.offset = Math.max(0, jr.offset - PAGE); apply(); };
  $("#jr-next").onclick = () => { jr.offset += PAGE; apply(); };
  $("#jr-csv").onclick = exportCsv;

  on("sources", renderSelects);
  on("zones", renderSelects);
  on("event", () => jr.visible && scheduleReload());
}

export function setJournalVisible(visible) {
  jr.visible = visible;
  if (visible) {
    readUrl();
    apply();
  }
}

function setQuick(kind) {
  const now = new Date();
  const from = { today: new Date(now.getFullYear(), now.getMonth(), now.getDate()), day: new Date(now - 864e5), week: new Date(now - 7 * 864e5) }[kind];
  jr.filters.from = from ? from.toISOString() : "";
  jr.filters.to = "";
  jr.offset = 0;
  apply();
}

function renderSelects() {
  const src = $('[data-f="source_id"]');
  if (!src) return;
  src.innerHTML = `<option value="">Все камеры</option>` + state.sources.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  src.value = jr.filters.source_id;
  const zones = jr.filters.source_id ? state.zones[jr.filters.source_id] || [] : Object.values(state.zones).flat();
  const zone = $('[data-f="zone_id"]');
  zone.innerHTML = `<option value="">Все зоны</option>` + zones.map((z) => `<option value="${z.id}">${esc(z.name)}</option>`).join("");
  zone.value = jr.filters.zone_id;
}

function renderControls() {
  renderSelects();
  for (const el of $$("[data-f]")) {
    if (el.dataset.f === "source_id" || el.dataset.f === "zone_id") continue;
    el.value = el.type === "datetime-local" ? toLocalInput(jr.filters[el.dataset.f]) : jr.filters[el.dataset.f];
  }
  for (const btn of $$("[data-quick]")) btn.classList.remove("active");
}

function apply() {
  writeUrl();
  renderControls();
  load();
}

let reloadTimer = null;
function scheduleReload() {
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(load, 1000); // пачка WS-событий -> один запрос
}

function query(extra = {}) {
  const q = { ...extra };
  for (const k of FILTERS) if (jr.filters[k]) q[k] = jr.filters[k];
  return q;
}

async function load() {
  const body = $("#jr-body");
  if (!body.children.length) body.innerHTML = `<tr><td colspan="8"><div class="empty">Загрузка…</div></td></tr>`;
  let page;
  try {
    page = await api.events(query({ limit: PAGE, offset: jr.offset }));
  } catch (err) {
    body.innerHTML = `<tr><td colspan="8"><div class="empty err">${icon("alert-triangle")}<div>Журнал не загружен: ${esc(err.message)}</div></div></td></tr>`;
    return;
  }
  jr.total = page.total;
  $("#jr-total").textContent = `${page.total} событий`;
  $("#jr-pager").textContent = page.total ? `${jr.offset + 1}–${jr.offset + page.items.length} из ${page.total}` : "";
  $("#jr-prev").disabled = jr.offset === 0;
  $("#jr-next").disabled = jr.offset + PAGE >= page.total;
  const filtered = FILTERS.some((k) => jr.filters[k]);
  body.innerHTML = page.items.length
    ? page.items.map((e) => `<tr data-ev="${e.id}" class="${e.priority === "critical" ? "row-crit" : ""} ${e.state === "active" ? "row-active" : ""}">
        <td>${e.thumbnail_url ? `<img src="${esc(mediaUrl(e.thumbnail_url))}" alt="" loading="lazy" />` : icon("film")}</td>
        <td class="mono">${fmtDateTime(e.started_at)}</td>
        <td>${esc(e.source_name || "камера удалена")}<div class="sub">${esc(e.zone_name || "без зоны")}</div></td>
        <td>${esc(label("scenario", e.scenario_type))}<div class="sub">${priorityBadge(e.priority)}</div></td>
        <td class="mono">${e.state === "active" ? "идёт" : fmtDuration(episodeSeconds(e))}</td>
        <td class="mono">${Math.round(e.confidence * 100)}%</td>
        <td>${stateBadge(e)}</td>
        <td>${reviewBadge(e.review_status)}</td></tr>`).join("")
    : `<tr><td colspan="8"><div class="empty">${icon("check-circle")}<div>${filtered ? "По выбранным фильтрам событий нет" : "Событий пока нет"}</div></div></td></tr>`;
  for (const row of $$("[data-ev]", body)) row.onclick = () => openEvent(row.dataset.ev);
}

// --- CSV ------------------------------------------------------------------------------------

async function exportCsv() {
  const btn = $("#jr-csv");
  btn.disabled = true;
  try {
    const rows = [];
    for (let offset = 0; offset < 10000; offset += 500) {
      const page = await api.events(query({ limit: 500, offset }));
      rows.push(...page.items);
      if (offset + 500 >= page.total) break;
    }
    const header = ["ID", "Начало", "Конец", "Длительность, с", "Камера", "Зона", "Нарушение", "Приоритет", "Состояние",
      "Проверка", "Уверенность, %", "Трек", "Подтверждено за (видео), с", "Модель", "Версия правила", "Кадр"];
    const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const lines = rows.map((e) => [
      e.id, fmtDateTime(e.started_at), e.ended_at ? fmtDateTime(e.ended_at) : "", episodeSeconds(e).toFixed(1).replace(".", ","),
      e.source_name, e.zone_name, label("scenario", e.scenario_type), label("priority", e.priority), label("state", e.state),
      label("review", e.review_status), Math.round(e.confidence * 100), e.track_id,
      (e.video_ts_detected - e.video_ts_start).toFixed(2).replace(".", ","), e.model_version, e.rule_version,
      e.thumbnail_url ? new URL(mediaUrl(e.thumbnail_url), location.href).href : "",
    ].map(cell).join(";"));
    // BOM и «;» — чтобы русский Excel открыл файл без мастера импорта.
    const blob = new Blob(["﻿" + [header.map(cell).join(";"), ...lines].join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `linq_safety_journal_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`Выгружено событий: ${rows.length}`, "success", { timeout: 2500 });
  } catch (err) {
    toastError(err);
  } finally {
    btn.disabled = false;
  }
}
