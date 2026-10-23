// Вкладка «Аналитика и отчёты»: показатели, замеры LinQ, графики, журнал с фильтрами, CSV и печать.
import { api } from "./api.js";
import { barChart, donutChart, hbarChart } from "./charts.js";
import { mediaUrl } from "./config.js";
import { openEvent, priorityBadge, reviewBadge, stateBadge } from "./event-modal.js";
import { hydrateIcons, icon } from "./icons.js";
import { on, state } from "./state.js";
import {
  $, $$, episodeSeconds, esc, fmtDateTime, fmtDuration, fmtNum, label, pct, toast, toastError,
} from "./util.js";

const PAGE = 20;
const PERIODS = { today: "Сегодня", day: "24 часа", week: "7 дней", all: "Всё время" };
const REVIEW_COLORS = { unreviewed: "#f59e0b", confirmed: "#ef4444", false_positive: "#64748b", escalated: "#a78bfa" };

const an = {
  period: "today",
  sourceId: "",
  journal: { scenario_type: "", zone_id: "", state: "", review_status: "", priority: "" },
  offset: 0,
  total: 0,
  visible: false,
  stale: true,
};

function periodRange(period) {
  const now = new Date();
  if (period === "today") return { from: new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString() };
  if (period === "day") return { from: new Date(now - 864e5).toISOString() };
  if (period === "week") return { from: new Date(now - 7 * 864e5).toISOString() };
  return {};
}

export function initAnalytics(root) {
  root.innerHTML = `
    <div class="print-only"><h2>Отчёт LinQ Safety</h2><p id="print-meta"></p></div>
    <div class="toolbar">
      <div class="seg" id="period">${Object.entries(PERIODS).map(([k, v]) => `<button data-period="${k}">${v}</button>`).join("")}</div>
      <select class="select" id="an-source" aria-label="Камера"></select>
      <div class="tabs-spacer"></div>
      <button class="btn btn-sm" id="an-refresh"><i data-icon="refresh"></i>Обновить</button>
      <button class="btn btn-sm" id="an-print"><i data-icon="printer"></i>Печать отчёта</button>
      <button class="btn btn-sm btn-primary" id="an-csv"><i data-icon="download"></i>Скачать CSV</button>
    </div>

    <div class="kpis" id="kpis"></div>

    <div class="card" style="margin-bottom:18px">
      <div class="card-head">
        <div><h3 class="card-title"><i data-icon="cpu"></i>Замеры скорости на LinQ</h3>
          <p class="card-sub">Последние значения от модуля анализа. Для отчёта о скорости запускайте анализ без режима «Реальное время».</p></div>
        <span id="linq-detector"></span>
      </div>
      <div class="linq-panel" id="linq-panel"></div>
    </div>

    <div class="charts">
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="activity"></i>Нарушения по часам</h3><div id="ch-hours"></div></div>
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="check-circle"></i>Результаты проверки</h3><div id="ch-review"></div></div>
    </div>
    <div class="charts-3">
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="target"></i>По зонам</h3><div id="ch-zones"></div></div>
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="video"></i>По камерам</h3><div id="ch-sources"></div></div>
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="alert-triangle"></i>По типам нарушений</h3><div id="ch-types"></div></div>
    </div>

    <div class="card">
      <div class="card-head">
        <h3 class="card-title"><i data-icon="database"></i>Журнал нарушений</h3>
        <span class="muted" id="journal-total"></span>
      </div>
      <div class="filters no-print">
        <select class="select" data-f="scenario_type"><option value="">Все типы</option>
          ${Object.entries({ danger_zone: 0, fall_immobility: 0 }).map(([k]) => `<option value="${k}">${label("scenario", k)}</option>`).join("")}</select>
        <select class="select" data-f="zone_id"><option value="">Все зоны</option></select>
        <select class="select" data-f="state"><option value="">Любое состояние</option><option value="active">Идёт</option><option value="ended">Завершено</option></select>
        <select class="select" data-f="review_status"><option value="">Любая проверка</option>
          ${["unreviewed", "confirmed", "false_positive", "escalated"].map((k) => `<option value="${k}">${label("review", k)}</option>`).join("")}</select>
        <select class="select" data-f="priority"><option value="">Любой приоритет</option>
          ${["low", "medium", "high", "critical"].map((k) => `<option value="${k}">${label("priority", k)}</option>`).join("")}</select>
      </div>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>Кадр</th><th>Начало</th><th>Камера / зона</th><th>Нарушение</th><th>Длительность</th><th>Уверенность</th><th>Состояние</th><th>Проверка</th></tr></thead>
        <tbody id="journal"></tbody>
      </table></div>
      <div class="pager no-print">
        <span id="pager-info"></span>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm" id="prev-page"><i data-icon="chevron-left"></i>Назад</button>
          <button class="btn btn-sm" id="next-page">Вперёд<i data-icon="chevron-right"></i></button>
        </div>
      </div>
    </div>`;
  hydrateIcons(root);

  for (const btn of $$("[data-period]", root)) {
    btn.onclick = () => { an.period = btn.dataset.period; an.offset = 0; refresh(); };
  }
  $("#an-source").onchange = (e) => { an.sourceId = e.target.value; an.journal.zone_id = ""; an.offset = 0; refresh(); };
  for (const sel of $$("[data-f]", root)) {
    sel.onchange = () => { an.journal[sel.dataset.f] = sel.value; an.offset = 0; loadJournal(); };
  }
  $("#an-refresh").onclick = refresh;
  $("#an-print").onclick = printReport;
  $("#an-csv").onclick = exportCsv;
  $("#prev-page").onclick = () => { an.offset = Math.max(0, an.offset - PAGE); loadJournal(); };
  $("#next-page").onclick = () => { an.offset += PAGE; loadJournal(); };

  on("sources", renderSourceFilter);
  on("zones", renderZoneFilter);
  on("event", () => { an.stale = true; if (an.visible) scheduleRefresh(); });
}

export function setAnalyticsVisible(visible) {
  an.visible = visible;
  if (visible && an.stale) refresh();
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 1500); // пачка WS-событий -> одно обновление
}

function baseQuery() {
  return { ...periodRange(an.period), source_id: an.sourceId || undefined };
}

async function refresh() {
  an.stale = false;
  $$("[data-period]").forEach((b) => b.classList.toggle("active", b.dataset.period === an.period));
  renderSourceFilter();
  renderZoneFilter();
  const q = baseQuery();
  try {
    const [stats, metrics, sample] = await Promise.all([
      api.stats(q),
      api.metrics(an.sourceId || undefined),
      api.events({ ...q, limit: 500 }),
    ]);
    renderKpis(stats, metrics, sample);
    renderLinq(metrics);
    renderCharts(stats);
  } catch (err) {
    toastError(err);
  }
  loadJournal();
}

// --- Показатели --------------------------------------------------------------------------------

function renderKpis(stats, metrics, sample) {
  const rv = stats.review;
  const reviewed = rv.confirmed + rv.false_positive;
  const precision = reviewed ? rv.confirmed / reviewed : null;
  const ended = sample.items.filter((e) => e.state === "ended");
  const avgDur = ended.length ? ended.reduce((s, e) => s + episodeSeconds(e), 0) / ended.length : null;
  const active = sample.items.filter((e) => e.state === "active").length;
  const ape = metrics.latency.alerts_per_episode;
  const kpi = (title, value, note, ico, color, noteColor = "var(--muted)") => `
    <div class="card kpi"><div><div class="kpi-label">${title}</div><div class="kpi-value">${value}</div>
      <div class="kpi-note" style="color:${noteColor}">${note}</div></div>
      <div class="kpi-ico ${color}">${icon(ico)}</div></div>`;
  $("#kpis").innerHTML = [
    kpi("Нарушений за период", stats.total, active ? `${active} идёт прямо сейчас` : "активных нет", "siren", "red", active ? "#fca5a5" : undefined),
    kpi("Ждут реакции оператора", rv.unreviewed, rv.unreviewed ? "требуется проверка" : "все проверены", "bell", "amber", rv.unreviewed ? "#fcd34d" : "#6ee7b7"),
    kpi("Точность по проверке", pct(precision), reviewed ? `${rv.confirmed} подтверждено · ${rv.false_positive} ложных` : "нет проверенных событий", "check-circle", "green"),
    kpi("Среднее время в зоне", avgDur == null ? "—" : fmtDuration(avgDur), ended.length ? `по ${ended.length} завершённым эпизодам` : "нет завершённых эпизодов", "clock", "purple"),
    kpi("Тревог на эпизод", ape == null ? "—" : fmtNum(ape, 2), ape == null ? "нет данных" : ape <= 1.0001 ? "повторы подавлены" : "есть повторные тревоги", "shield", "blue", ape != null && ape <= 1.0001 ? "#6ee7b7" : undefined),
  ].join("");
}

function renderLinq(metrics) {
  const s = metrics.sources.find((x) => x.fps != null) || metrics.sources[0] || {};
  const l = metrics.latency;
  $("#linq-detector").innerHTML = s.detector_kind
    ? `<span class="badge ${s.detector_kind === "linq" ? "badge-green" : "badge-amber"}">${esc(label("detector", s.detector_kind))}</span>`
    : "";
  const m = (title, value, note) => `<div class="metric"><div class="metric-label">${title}</div><div class="metric-value">${value}</div><div class="metric-note">${note}</div></div>`;
  $("#linq-panel").innerHTML = [
    m("Скорость обработки", fmtNum(s.fps, 1, " FPS"), s.source_name ? esc(s.source_name) : "анализ ещё не запускался"),
    m("Инференс, среднее", fmtNum(s.inference_ms_avg, 1, " мс"), "время нейросети на кадр"),
    m("Инференс, p95", fmtNum(s.inference_ms_p95, 1, " мс"), "95% кадров быстрее"),
    m("Кадров обработано", s.frames_processed ?? "—", esc(s.model_version || "—")),
    m("Обнаружение (видео)", fmtNum(l.detection_delay_video_ts_s_avg, 2, " с"), "от входа в зону до подтверждения"),
    m("Обнаружение (реально)", fmtNum(l.detection_delay_real_s_avg, 2, " с"), "честно в режиме реального времени"),
    m("Оповещение", fmtNum(l.notify_delay_ms_avg, 1, " мс"), `p95 ${fmtNum(l.notify_delay_ms_p95, 1, " мс")} · до отправки тревоги`),
    m("Эпизодов в замере", l.events_count, "все события выбранных камер"),
  ].join("");
}

function renderCharts(stats) {
  // Часы приходят в UTC — показываем в местном времени.
  const hours = stats.by_hour.map((h) => ({ d: new Date(h.hour), c: h.count }));
  const labels = [], values = [];
  if (hours.length) {
    // От первого часа с событиями до последнего: не меньше 12 и не больше 168 столбиков.
    const H = 36e5;
    const last = hours[hours.length - 1].d.getTime();
    const start = Math.max(Math.min(hours[0].d.getTime(), last - 11 * H), last - 167 * H);
    const count = Math.round((last - start) / H) + 1;
    const byTime = new Map(hours.map((h) => [h.d.getTime(), h.c]));
    for (let i = 0; i < count; i++) {
      const t = start + i * 36e5;
      const d = new Date(t);
      labels.push(count > 24 ? `${d.getDate()}.${String(d.getMonth() + 1).padStart(2, "0")} ${d.getHours()}ч` : `${String(d.getHours()).padStart(2, "0")}:00`);
      values.push(byTime.get(t) || 0);
    }
  }
  barChart($("#ch-hours"), { labels, values, color: "var(--blue)" });
  donutChart($("#ch-review"), Object.entries(stats.review).map(([k, v]) => ({ label: label("review", k), value: v, color: REVIEW_COLORS[k] })), { centerLabel: "событий" });
  hbarChart($("#ch-zones"), stats.by_zone.map((z) => ({ label: z.label || "удалена", value: z.count })), { color: "var(--amber)" });
  hbarChart($("#ch-sources"), stats.by_source.map((s) => ({ label: s.label || "удалена", value: s.count })), { color: "var(--cyan)" });
  hbarChart($("#ch-types"), stats.by_scenario_type.map((s) => ({ label: label("scenario", s.key), value: s.count })), { color: "var(--red)" });
}

// --- Фильтры ---------------------------------------------------------------------------------

function renderSourceFilter() {
  const sel = $("#an-source");
  if (!sel) return;
  sel.innerHTML = `<option value="">Все камеры</option>` + state.sources.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  sel.value = an.sourceId;
}

function renderZoneFilter() {
  const sel = $('[data-f="zone_id"]');
  if (!sel) return;
  const zones = an.sourceId ? state.zones[an.sourceId] || [] : Object.values(state.zones).flat();
  sel.innerHTML = `<option value="">Все зоны</option>` + zones.map((z) => `<option value="${z.id}">${esc(z.name)}</option>`).join("");
  sel.value = an.journal.zone_id;
}

function journalQuery(extra = {}) {
  const q = { ...baseQuery(), ...extra };
  for (const [k, v] of Object.entries(an.journal)) if (v) q[k] = v;
  return q;
}

// --- Журнал ----------------------------------------------------------------------------------

async function loadJournal() {
  let page;
  try {
    page = await api.events(journalQuery({ limit: PAGE, offset: an.offset }));
  } catch (err) {
    return toastError(err);
  }
  an.total = page.total;
  $("#journal-total").textContent = `${page.total} событий`;
  $("#pager-info").textContent = page.total ? `${an.offset + 1}–${an.offset + page.items.length} из ${page.total}` : "";
  $("#prev-page").disabled = an.offset === 0;
  $("#next-page").disabled = an.offset + PAGE >= page.total;
  const body = $("#journal");
  body.innerHTML = page.items.length
    ? page.items.map((e) => `<tr data-ev="${e.id}">
        <td>${e.thumbnail_url ? `<img src="${esc(mediaUrl(e.thumbnail_url))}" alt="" loading="lazy" />` : icon("film")}</td>
        <td class="mono">${fmtDateTime(e.started_at)}</td>
        <td>${esc(e.source_name || "удалена")}<div class="sub">${esc(e.zone_name || "без зоны")}</div></td>
        <td>${esc(label("scenario", e.scenario_type))}<div class="sub">${priorityBadge(e.priority)}</div></td>
        <td class="mono">${e.state === "active" ? "идёт" : fmtDuration(episodeSeconds(e))}</td>
        <td class="mono">${Math.round(e.confidence * 100)}%</td>
        <td>${stateBadge(e)}</td>
        <td>${reviewBadge(e.review_status)}</td></tr>`).join("")
    : `<tr><td colspan="8"><div class="empty">${icon("check-circle")}<div>За выбранный период нарушений нет</div></div></td></tr>`;
  for (const row of $$("[data-ev]", body)) row.onclick = () => openEvent(row.dataset.ev);
}

// --- Экспорт -----------------------------------------------------------------------------------

async function exportCsv() {
  const btn = $("#an-csv");
  btn.disabled = true;
  try {
    const rows = [];
    for (let offset = 0; offset < 10000; offset += 500) {
      const page = await api.events(journalQuery({ limit: 500, offset }));
      rows.push(...page.items);
      if (offset + 500 >= page.total) break;
    }
    const header = ["ID", "Начало", "Конец", "Длительность, с", "Камера", "Зона", "Нарушение", "Приоритет", "Состояние",
      "Проверка", "Уверенность, %", "Трек", "Подтверждено за (видео), с", "Модель", "Кадр"];
    const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const lines = rows.map((e) => [
      e.id, fmtDateTime(e.started_at), e.ended_at ? fmtDateTime(e.ended_at) : "", episodeSeconds(e).toFixed(1).replace(".", ","),
      e.source_name, e.zone_name, label("scenario", e.scenario_type), label("priority", e.priority), label("state", e.state),
      label("review", e.review_status), Math.round(e.confidence * 100), e.track_id,
      (e.video_ts_detected - e.video_ts_start).toFixed(2).replace(".", ","), e.model_version,
      e.thumbnail_url ? new URL(mediaUrl(e.thumbnail_url), location.href).href : "",
    ].map(cell).join(";"));
    // BOM и «;» — чтобы русский Excel открыл файл без мастера импорта.
    const blob = new Blob(["﻿" + [header.map(cell).join(";"), ...lines].join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `linq_safety_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`Выгружено событий: ${rows.length}`, "success", { timeout: 2500 });
  } catch (err) {
    toastError(err);
  } finally {
    btn.disabled = false;
  }
}

function printReport() {
  const cam = an.sourceId ? state.sources.find((s) => s.id === an.sourceId)?.name : "все камеры";
  $("#print-meta").textContent = `Период: ${PERIODS[an.period]} · Камеры: ${cam} · Сформирован: ${fmtDateTime(new Date().toISOString())} · Оператор: ${state.operator}`;
  window.print();
}
