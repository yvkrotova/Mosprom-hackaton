// «Статистика»: сводные KPI, динамика инцидентов по дням (по сценариям), рейтинг начальников смен
// (индекс безопасности, тренд, бейджи), горячие точки (зоны и камеры) и технические замеры.
// Данные — GET /analytics/overview (реальные и демо-события вместе) и GET /metrics.
// Ноль — «данные есть, их ноль»; прочерк — «нет данных».
import { api } from "./api.js";
import { hydrateIcons, icon } from "./icons.js";
import { kpiIcon } from "./kpi-icons.js";
import { SERIES_COLORS, hideTip, legendHtml, showTip, stackedBars } from "./stacked-bars.js";
import { emit, on, refreshSources, state } from "./state.js";
import {
  $, $$, confirmModal, envLabel, esc, fmtDateTime, fmtHM, fmtNum, fromLocalInput, helpIcon, isLinq, isReplay,
  label, openMenu, openModal, plural, stored, store, toast, toastError, toLocalInput,
} from "./util.js";

const PERIODS = [7, 14, 30];
const saved = stored("analyticsFilters", {});

const st = {
  days: PERIODS.includes(saved.days) ? saved.days : 14,
  supervisor: saved.supervisor || "",
  data: null, // GET /analytics/overview
  metrics: null,
  metricsSource: null,
  selectedDay: null,
  table: false, // табличный вид динамики
  error: null,
  loading: false,
  visible: false,
};

const tz = () => -new Date().getTimezoneOffset();

async function load({ quiet = false } = {}) {
  if (st.loading) return; // защита от повторного «Обновить»
  st.loading = true;
  if (!quiet) render();
  try {
    const [data, metrics] = await Promise.all([
      api.analytics({ days: st.days, supervisor: st.supervisor || undefined, tz_offset_min: tz() }),
      api.metrics(),
    ]);
    const changed = JSON.stringify(data) !== JSON.stringify(st.data) || JSON.stringify(metrics) !== JSON.stringify(st.metrics);
    st.data = data;
    st.metrics = metrics;
    st.error = null;
    if (st.selectedDay && !data.daily.some((d) => d.date === st.selectedDay)) st.selectedDay = null;
    st.loading = false;
    if (changed || !quiet) render(); // тихое обновление не сбрасывает наведение, если данных не прибавилось
  } catch (err) {
    st.error = err.message; // последние значения остаются на экране
    st.loading = false;
    render();
  }
}

function save() {
  store("analyticsFilters", { days: st.days, supervisor: st.supervisor });
}

// Серии — сценарии в порядке GET /scenarios; цвет по позиции, без повторов.
const series = () => state.scenarios.map((s, i) => ({ key: s.scenario_type, title: s.title, color: SERIES_COLORS[i % SERIES_COLORS.length] }));
const pctText = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const RISK = { high: ["pill-red", "Высокий"], medium: ["pill-amber", "Средний"], low: ["pill-green", "Низкий"] };
const TREND = {
  improving: ["trend-good", "arrow-down", "улучшение"],
  worsening: ["trend-bad", "arrow-up", "ухудшение"],
  stable: ["trend-flat", "arrow-right", "стабильно"],
};

// --- KPI ----------------------------------------------------------------------------------------

function kpiCards(s) {
  const loading = !st.data;
  const change = s?.change;
  return [
    {
      ic: "siren", title: "Всего инцидентов", value: loading ? null : String(s.total),
      // При малой базе (меньше 5 событий) процент вводит в заблуждение — показываем число.
      sub: change == null || s.prev_total < 5
        ? `за ${st.days} ${plural(st.days, "день", "дня", "дней")} · в прошлом периоде: ${s?.prev_total ?? "—"}`
        : `${change > 0 ? "+" : ""}${Math.round(change * 100)}% к прошлым ${st.days} дням`,
      cls: change == null || s.prev_total < 5 ? "" : change > 0 ? "bad" : "good",
      tip: "Все эпизоды, начавшиеся за период (включая отмеченные ложными). Сравнение — с таким же предыдущим периодом.",
    },
    {
      ic: "alert", title: "Опасная зона", value: loading ? null : s.top_zone?.name || "—",
      sub: s?.top_zone ? `${pctText(s.top_zone.share)} нарушений · ${s.top_zone.source_name || ""}` : "нарушений в зонах нет",
      tip: "Зона, на которую пришлась самая большая доля нарушений (без ложных) за период.",
    },
    {
      ic: "check", title: "Лучший начальник смены", value: loading ? null : s.best_supervisor?.supervisor || "—",
      sub: s?.best_supervisor ? `индекс безопасности ${s.best_supervisor.safety_index} · ${fmtNum(s.best_supervisor.per_shift, 1)} на смену` : "смен в периоде нет",
      tip: "Начальник с наибольшим индексом безопасности за период (меньше всего нарушений на смену).",
    },
    {
      ic: "bell", title: "Доля подтверждённых", value: loading ? null : pctText(s.confirmed_share),
      sub: s?.rated ? `${s.confirmed} из ${s.rated} оценённых оператором` : "оценок оператора нет",
      tip: "Сколько из оценённых оператором тревог оказались настоящими нарушениями. Неоценённые не учитываются.",
    },
  ];
}

// --- Блок 1: динамика -----------------------------------------------------------------------------

function dayDetails(d) {
  const s = series().filter((x) => d.by_scenario[x.key]);
  const sups = d.supervisors.length ? d.supervisors.map((n) => `${esc(n)}${d.by_supervisor[n] != null ? ` (${d.by_supervisor[n]})` : ""}`).join(", ") : "смен нет";
  const [y, m, dd] = d.date.split("-");
  return `<div class="tip-title">${dd}.${m}.${y}</div>
    <div class="tip-row"><span>Начальник смены</span><b>${sups}</b></div>
    <div class="tip-row"><span>Инцидентов</span><b>${d.total}</b></div>
    ${s.map((x) => `<div class="tip-row"><span><i class="sw" style="background:${x.color}"></i>${esc(x.title)}</span><b>${d.by_scenario[x.key]}</b></div>`).join("")}
    <div class="tip-row"><span>Подтверждено</span><b>${d.confirmed}</b></div>
    <div class="tip-row"><span>Ложных</span><b>${d.false_positive}</b></div>`;
}

function dynamicsBlock() {
  const data = st.data;
  const sel = data?.daily.find((d) => d.date === st.selectedDay);
  const total = data ? data.daily.reduce((a, d) => a + d.total, 0) : 0;
  return `<div class="panel metrics">
    <div class="metrics-head"><div><h2 class="section-title">${icon("chart")}Динамика инцидентов по дням
      ${helpIcon("Столбец — инциденты дня с разбивкой по сценариям. Наведите на день — подробности, щёлкните — закрепить и открыть в журнале.")}</h2></div>
      <button class="btn btn-sm no-print" id="st-table">${icon(st.table ? "chart" : "list")}${st.table ? "График" : "Таблица"}</button></div>
    ${!data ? `<div class="skeleton" style="height:300px"></div>`
      : !total ? `<div class="empty">${icon("check-circle")}<b>Инцидентов за период нет</b>
          <span>Для демонстрации можно сгенерировать историю: «Демо-история» вверху страницы.</span></div>`
      : st.table ? dailyTable(data.daily)
      : `${legendHtml(series())}<div class="chart-box" id="st-chart"></div>`}
    ${sel ? `<div class="day-detail">${dayDetails(sel)}
      <div class="day-actions"><button class="btn btn-sm" id="st-day-journal">${icon("list")}Открыть день в журнале</button>
        <button class="btn btn-sm btn-ghost" id="st-day-close">${icon("x")}Снять выбор</button></div></div>` : ""}
  </div>`;
}

function dailyTable(daily) {
  const s = series();
  return `<div class="table-wrap"><table class="table rating-table">
    <thead><tr><th>Дата</th><th>Начальник смены</th><th>Всего</th>${s.map((x) => `<th>${esc(x.title)}</th>`).join("")}<th>Подтверждено</th><th>Ложных</th></tr></thead>
    <tbody>${daily.map((d) => `<tr><td>${d.date.split("-").reverse().join(".")}</td><td>${esc(d.supervisors.join(", ") || "—")}</td>
      <td><b>${d.total}</b></td>${s.map((x) => `<td>${d.by_scenario[x.key] || 0}</td>`).join("")}<td>${d.confirmed}</td><td>${d.false_positive}</td></tr>`).join("")}</tbody></table></div>`;
}

function drawChart() {
  const box = $("#st-chart");
  if (!box || !st.data) return;
  stackedBars(box, st.data.daily, series(), {
    selected: st.selectedDay,
    onHover: (d, x, y) => showTip(dayDetails(d), x, y),
    onLeave: hideTip,
    onSelect: (d) => { st.selectedDay = st.selectedDay === d.date ? null : d.date; hideTip(); render(); },
  });
}

// --- Блок 2: начальники смен --------------------------------------------------------------------

function indexBar(v) {
  const cls = v >= 70 ? "good" : v >= 35 ? "warn" : "bad";
  return `<div class="index-bar" title="Индекс безопасности ${v} из 100"><div class="index-track"><div class="index-fill ${cls}" style="width:${v}%"></div></div><b>${v}</b></div>`;
}

function supervisorsBlock() {
  const rows = st.data?.supervisors;
  const head = `<div class="metrics-head"><div><h2 class="section-title">${icon("trophy")}Рейтинг начальников смен
      ${helpIcon("Нарушения смены — инциденты за смену на её участке, кроме ложных. Индекс безопасности 0–100: насколько смены чище средней по команде (средний ≈ 50, без нарушений — 100). Тренд — сравнение первой и второй половины периода.")}</h2></div>
    <button class="btn btn-sm no-print" id="st-shifts">${icon("clock")}Смены</button></div>`;
  if (!rows) return `<div class="panel metrics">${head}<div class="skeleton" style="height:180px"></div></div>`;
  if (!rows.length) {
    return `<div class="panel metrics">${head}<div class="empty">${icon("user")}<b>Смен в периоде нет</b>
      <span>Отметьте смены кнопкой «Смены» или сгенерируйте демо-историю.</span></div></div>`;
  }
  return `<div class="panel metrics">${head}<div class="table-wrap"><table class="table rating-table">
    <thead><tr><th>Место</th><th>Начальник смены</th><th>Смен</th><th>Нарушений на смену</th><th>Подтверждено</th><th>Тренд</th><th>Статус</th><th>Индекс безопасности</th></tr></thead>
    <tbody>${rows.map((r) => {
      const [tcls, tic, ttext] = TREND[r.trend];
      return `<tr>
      <td class="rank">${r.rank ?? "—"}</td>
      <td><b>${esc(r.supervisor)}</b> ${r.active_now ? `<span class="pill pill-blue pill-sm">на смене</span>` : ""}</td>
      <td>${r.shifts}</td><td>${fmtNum(r.per_shift, 1)}</td><td>${r.confirmed}</td>
      <td><span class="trend ${tcls}" title="${r.trend_change == null ? "" : `${r.trend_change > 0 ? "+" : ""}${Math.round(r.trend_change * 100)}% ко второй половине периода`}">${icon(tic)}${ttext}</span></td>
      <td>${r.badge === "leader" ? `<span class="pill pill-green pill-sm">Лидер безопасности</span>` : r.badge === "risk" ? `<span class="pill pill-red pill-sm">Зона риска</span>`
        : r.rank == null ? `<span class="faint" title="Для места в рейтинге нужно от 3 смен в периоде">мало смен</span>` : `<span class="faint">—</span>`}</td>
      <td>${indexBar(r.safety_index)}</td></tr>`;
    }).join("")}</tbody></table></div></div>`;
}

// --- Блок 3: горячие точки ------------------------------------------------------------------------

function spotList(title, rows, sub) {
  if (!rows.length) return `<div class="spots"><h3>${title}</h3><div class="empty" style="padding:18px">Нарушений нет</div></div>`;
  const max = Math.max(...rows.map((r) => r.share));
  return `<div class="spots"><h3>${title}</h3>${rows.slice(0, 6).map((r, i) => {
    const [rcls, rtext] = RISK[r.risk];
    return `<div class="spot">
      <span class="spot-rank">${i + 1}</span>
      <div class="spot-main"><div class="spot-name ellipsis" title="${esc(r.name)}">${esc(r.name)}</div>
        <div class="spot-sub ellipsis">${esc(sub(r))} · чаще всего: ${esc(label("scenario", r.top_scenario))}</div>
        <div class="share-bar"><div style="width:${max ? (r.share / max) * 100 : 0}%"></div></div></div>
      <div class="spot-num"><b>${pctText(r.share)}</b><span>${r.count} ${plural(r.count, "нарушение", "нарушения", "нарушений")}</span></div>
      <span class="pill ${rcls} pill-sm" title="Уровень риска">${rtext}</span></div>`;
  }).join("")}</div>`;
}

function hotspotsBlock() {
  const h = st.data?.hotspots;
  return `<div class="panel metrics">
    <div class="metrics-head"><div><h2 class="section-title">${icon("target")}Горячие точки
      ${helpIcon("Доля — от всех нарушений за период (без ложных). Риск: высокий — от 25% нарушений или от 2 в день; средний — от 10% или от 0,7 в день.")}</h2></div></div>
    ${!h ? `<div class="skeleton" style="height:200px"></div>` : `<div class="spots-grid">
      ${spotList("Зоны", h.zones, (r) => [r.source_name, r.site].filter(Boolean).join(" · ") || "—")}
      ${spotList("Камеры", h.cameras, (r) => r.site || "участок не указан")}</div>`}
  </div>`;
}

// --- Замеры детектора ----------------------------------------------------------------------------

const STATE_TEXT = {
  not_started: "Анализ не запускался", pending: "Замер выполняется", running: "Замер выполняется",
  stale: "Замер устарел", finished: "Анализ завершён — последний замер",
};

function metricCards(m) {
  const replay = isReplay(m?.detector_kind);
  const none = m?.measurement_state === "not_started" ? "Анализ не запускался" : "Нет замера";
  const ms = (x) => (x == null ? "—" : `${fmtNum(x, 1)} мс`);
  const s = (x) => (x == null ? "—" : `${fmtNum(x, 2)} с`);
  const lat = m?.latency;
  const card = (title, value, sub, tip) => ({ title, value, sub: value === "—" ? none : sub, tip });
  return [
    card(replay ? "Текущая скорость воспроизведения" : "Скорость обработки", m?.fps == null ? "—" : `${fmtNum(m.fps, 1)} кадр/с`,
      replay ? "чтение записи + трекер + правила" : "кадров в секунду",
      replay ? "Скорость проигрывания записи на этом сервере. Это не скорость LinQ." : "Сколько кадров в секунду обрабатывает анализ этой камеры."),
    card(replay ? "Историческое время инференса на LinQ" : "Инференс, среднее", ms(replay ? m?.recorded_inference_ms_avg : m?.inference_ms_avg),
      replay ? `медиана ${ms(m?.recorded_inference_ms_p50)}` : "время нейросети на кадр",
      replay ? "Записано при прогоне на LinQ: вызов runtime с ожиданием результата. Это не замер текущего сервера." : "Среднее время работы нейросети на кадр."),
    card("Инференс, p95", ms(replay ? m?.recorded_inference_ms_p95 : m?.inference_ms_p95), replay ? "из записи LinQ" : "95% кадров не дольше",
      "95-й процентиль: 95% кадров обрабатываются нейросетью не дольше этого времени (показывает «хвост» задержек)."),
    card("Пре- / постпроцессинг", m?.preprocess_ms_avg == null && m?.postprocess_ms_avg == null ? "—" : `${ms(m?.preprocess_ms_avg)} / ${ms(m?.postprocess_ms_avg)}`,
      "подготовка входа / разбор выхода и NMS",
      "Препроцессинг — подготовка кадра для сети (масштаб, поля). Постпроцессинг — разбор выхода сети и подавление дублей рамок (NMS)."),
    card("Кадров обработано", m?.frames_processed == null || !m?.ts ? "—" : String(m.frames_processed),
      m?.frames_unanalyzed ? `без записи: ${m.frames_unanalyzed}` : "за текущий запуск", "Кадры, прошедшие анализ за последний запуск камеры."),
    card("Обнаружение (видео)", s(lat?.detection_delay_video_ts_s_avg), `p95 ${s(lat?.detection_delay_video_ts_s_p95)}`,
      "От начала нарушения до его подтверждения — по времени видео. Включает окно подтверждения правила."),
    card("Обнаружение (реально)", s(lat?.detection_delay_real_s_avg), `p95 ${s(lat?.detection_delay_real_s_p95)}`,
      "То же по реальным часам: зависит от скорости обработки (при ускоренном проигрывании файла может быть меньше видео)."),
    card("Задержка уведомления", lat?.notify_delay_ms_p95 == null ? "—" : `${fmtNum(lat.notify_delay_ms_p95, 0)} мс`,
      lat?.notify_delay_ms_avg == null ? "" : `p95; среднее ${fmtNum(lat.notify_delay_ms_avg, 0)} мс`,
      "От подтверждения эпизода до отправки тревоги в браузер (p95 по событиям запуска)."),
    card("Тревог на эпизод", lat?.alerts_per_episode == null ? "—" : fmtNum(lat.alerts_per_episode, 1),
      `событий запуска: ${lat?.events_count ?? 0}`, "По событиям текущего запуска камеры. Цель — 1."),
    { title: "Обновлено", value: m?.ts ? fmtHM(m.ts) : "—", sub: STATE_TEXT[m?.measurement_state] || "Ожидание замера",
      tip: "Время последнего замера от анализа этой камеры." },
  ];
}

function metricsBlock() {
  const sources = (st.metrics?.sources || []).filter((x) => !state.sources.find((s) => s.id === x.source_id)?.is_demo);
  if (!st.metricsSource || !sources.some((x) => x.source_id === st.metricsSource)) st.metricsSource = sources[0]?.source_id || null;
  const m = sources.find((x) => x.source_id === st.metricsSource) || null;
  const env = envLabel(m?.detector_kind);
  return `<div class="panel metrics">
      <div class="metrics-head">
        <div><h2 class="section-title">${icon("cpu")}Замеры скорости детектора</h2></div>
        ${m?.detector_kind ? `<span class="badge badge-env ${env.cls}">${esc(env.text)}</span>` : ""}
      </div>
      ${sources.length > 1 ? `<div class="src-tabs no-print">${sources.map((x) => `<button class="btn btn-sm ${x.source_id === st.metricsSource ? "btn-outline-primary" : ""}" data-msrc="${x.source_id}">${esc(x.source_name || "камера")}</button>`).join("")}</div>` : ""}
      ${m?.detector_kind && !isLinq(m.detector_kind) && !isReplay(m.detector_kind) ? `<div class="notice notice-warn">${icon("info")}<span>Обработка на CPU. Это <b>не замер на ускорителе LinQ</b>.</span></div>` : ""}
      <div class="mgrid">${metricCards(m).map((c) => `
        <div class="mcard"><div class="m-title">${esc(c.title)} ${helpIcon(c.tip)}</div>
          <div class="m-value">${esc(c.value)}</div><div class="m-sub">${esc(c.sub)}</div></div>`).join("")}</div>
    </div>`;
}

// --- Страница ---------------------------------------------------------------------------------------

function render() {
  const root = $("#screen-stats");
  if (!root || !st.visible) return;
  hideTip();
  const s = st.data?.summary;
  const supervisors = [...new Set([...(st.data?.supervisors || []).map((r) => r.supervisor), st.supervisor].filter(Boolean))].sort();
  const periodText = st.data ? `${fmtDateTime(st.data.from)} — ${fmtDateTime(st.data.to)}` : "";
  const scroll = $("#content").scrollTop;

  root.innerHTML = `
    <div class="panel page-head">
      <div><h1>Статистика</h1><p>Инциденты, безопасность смен и горячие точки</p></div>
      <div class="head-actions">
        ${s?.demo ? `<span class="badge badge-demo" title="В периоде есть сгенерированная демо-история">Демонстрационные данные</span>` : ""}
        <button class="btn no-print" id="st-demo">${icon("database")}Демо-история${icon("chevron-down")}</button>
      </div>
    </div>
    <div class="print-only"><b>Отчёт: статистика безопасности</b><br>Период: ${esc(periodText)}${st.supervisor ? ` · Начальник смены: ${esc(st.supervisor)}` : ""} ·
      Сформирован: ${esc(fmtDateTime(new Date().toISOString()))}${s?.demo ? " · Демонстрационные данные" : ""}</div>
    <div class="panel toolbar">
      <div class="seg">${PERIODS.map((d) => `<button data-days="${d}" class="${d === st.days ? "active" : ""}">${d} ${plural(d, "день", "дня", "дней")}</button>`).join("")}</div>
      <select class="select" id="st-supervisor" style="width:260px" aria-label="Начальник смены">
        <option value="">Все начальники смен</option>${supervisors.map((n) => `<option value="${esc(n)}" ${n === st.supervisor ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
      <span class="spacer"></span>
      <button class="btn ${st.loading ? "loading" : ""}" id="st-refresh">${icon("refresh")}Обновить</button>
      <button class="btn" id="st-journal">${icon("list")}Открыть в журнале</button>
      <button class="btn" id="st-print">${icon("printer")}Печать отчёта</button>
    </div>
    ${st.error ? `<div class="notice notice-error">${icon("alert-triangle")}<span>Не удалось обновить: ${esc(st.error)}. ${st.data ? "Показаны последние полученные значения." : ""}</span></div>` : ""}
    <div class="kpis kpis-4">${kpiCards(s).map((k) => `
      <div class="panel kpi"><div class="kpi-icon">${kpiIcon(k.ic)}</div>
        <div class="kpi-body"><div class="kpi-title">${esc(k.title)} ${helpIcon(k.tip)}</div>
        <div class="kpi-value ${k.value && k.value.length > 9 ? "kpi-value-text" : ""}" title="${esc(k.value || "")}">${k.value == null ? `<span class="skeleton" style="display:inline-block;width:90px;height:36px"></span>` : esc(k.value)}</div>
        <div class="kpi-sub ${k.cls || ""}">${esc(k.sub)}</div></div></div>`).join("")}</div>
    ${dynamicsBlock()}
    ${supervisorsBlock()}
    ${hotspotsBlock()}
    ${metricsBlock()}
    ${s?.demo ? `<div class="screen-footnote">В периоде есть сгенерированная демо-история — показатели приведены для примера</div>` : ""}`;
  hydrateIcons(root);
  drawChart();
  $("#content").scrollTop = scroll;

  for (const b of $$("[data-days]", root)) b.onclick = () => { st.days = Number(b.dataset.days); st.selectedDay = null; save(); load(); };
  $("#st-supervisor").onchange = (e) => { st.supervisor = e.target.value; st.selectedDay = null; save(); load(); };
  $("#st-refresh").onclick = () => load();
  $("#st-journal").onclick = () => st.data && emit("open-journal", { from: st.data.from, to: st.data.to });
  $("#st-print").onclick = () => window.print();
  $("#st-demo").onclick = (e) => openMenu(e.currentTarget, [
    { label: "Сгенерировать историю за 30 дней", icon: "database", run: generateDemo },
    { label: "Удалить демо-историю", icon: "trash", run: clearDemo },
  ]);
  const table = $("#st-table");
  if (table) table.onclick = () => { st.table = !st.table; render(); };
  const dayJournal = $("#st-day-journal");
  if (dayJournal) dayJournal.onclick = () => {
    const d = st.data.daily.find((x) => x.date === st.selectedDay);
    emit("open-journal", { from: d.from, to: d.to });
  };
  const dayClose = $("#st-day-close");
  if (dayClose) dayClose.onclick = () => { st.selectedDay = null; render(); };
  for (const b of $$("[data-msrc]", root)) b.onclick = () => { st.metricsSource = b.dataset.msrc; render(); };
  $("#st-shifts").onclick = openShifts;
}

async function generateDemo() {
  const ok = await confirmModal("Сгенерировать демо-историю?",
    "Будут созданы 4 демо-камеры, смены 5 начальников и инциденты за 30 дней. Прежняя демо-история заменится; реальные события и смены не изменятся. Удалить её можно тем же меню.",
    { okLabel: "Сгенерировать", danger: false });
  if (!ok) return;
  try {
    const r = await api.generateDemo({ days: 30, tz_offset_min: tz() });
    await refreshSources();
    toast(`Демо-история: ${r.events} инцидентов, ${r.shifts} смен`, "success", { timeout: 3500 });
    st.days = 30;
    save();
    load();
  } catch (err) { toastError(err); }
}

async function clearDemo() {
  const ok = await confirmModal("Удалить демо-историю?", "Удалятся демо-камеры, демо-смены и их инциденты. Реальные данные останутся.");
  if (!ok) return;
  try {
    const r = await api.clearDemo();
    await refreshSources();
    toast(`Удалено: ${r.events} инцидентов, ${r.shifts} смен, ${r.sources} камер`, "success", { timeout: 3500 });
    load();
  } catch (err) { toastError(err); }
}

async function openShifts() {
  let shifts = [];
  const { root, close } = openModal(`<h3 class="modal-title">${icon("clock")}Смены начальников</h3><div data-body></div>`, {
    wide: true, onClose: () => load(),
  });
  const body = $("[data-body]", root);
  const allSites = [...new Set(state.sources.map((s) => s.site).filter(Boolean))].sort();
  const draw = () => {
    body.innerHTML = `
      <div class="filters" style="padding:0 0 14px">
        <label class="field"><span>Начальник смены</span><input class="input" name="supervisor" maxlength="100" placeholder="Фамилия И.О." /></label>
        <label class="field"><span>Участок</span><select class="select" name="site"><option value="">Все участки</option>
          ${allSites.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}</select></label>
        <label class="field"><span>Начало</span><input class="input" type="datetime-local" name="start" value="${toLocalInput(new Date().toISOString())}" /></label>
        <label class="field"><span>Конец (пусто — смена идёт)</span><input class="input" type="datetime-local" name="end" /></label>
      </div>
      <div style="display:flex;justify-content:flex-end"><button class="btn btn-primary" data-add>${icon("plus")}Добавить смену</button></div>
      <div class="table-wrap" style="margin-top:14px;max-height:420px"><table class="table rating-table">
        <thead><tr><th>Начальник</th><th>Участок</th><th>Начало</th><th>Конец</th><th></th></tr></thead>
        <tbody>${shifts.length ? shifts.map((s) => `<tr><td>${esc(s.supervisor)}${s.is_demo ? ` <span class="faint">(демо)</span>` : ""}</td><td>${esc(s.site || "Все участки")}</td>
          <td>${fmtDateTime(s.started_at)}</td><td>${s.ended_at ? fmtDateTime(s.ended_at) : `<span class="pill pill-blue pill-sm">идёт</span>`}</td>
          <td class="nowrap">${s.ended_at ? "" : `<button class="btn btn-sm" data-end="${s.id}">Завершить</button>`}
            <button class="btn btn-sm btn-ghost" data-del="${s.id}" title="Удалить">${icon("trash")}</button></td></tr>`).join("")
          : `<tr><td colspan="5"><div class="empty">Смен в выбранном периоде нет</div></td></tr>`}</tbody></table></div>
      <div class="modal-actions"><button class="btn" data-close>Готово</button></div>`;
    $("[data-close]", body).onclick = close;
    $("[data-add]", body).onclick = async () => {
      const supervisor = $("input[name=supervisor]", body).value.trim();
      if (!supervisor) return toast("Укажите начальника смены", "info");
      try {
        await api.createShift({
          supervisor,
          site: $("select[name=site]", body).value || null,
          started_at: fromLocalInput($("input[name=start]", body).value) || undefined,
          ended_at: fromLocalInput($("input[name=end]", body).value) || null,
        });
        await reload();
        toast("Смена добавлена", "success", { timeout: 2000 });
      } catch (err) { toastError(err); }
    };
    for (const b of $$("[data-end]", body)) b.onclick = async () => { try { await api.endShift(b.dataset.end); await reload(); } catch (err) { toastError(err); } };
    for (const b of $$("[data-del]", body)) {
      b.onclick = async () => {
        if (!(await confirmModal("Удалить смену?", "Смена пропадёт из рейтинга. Нарушения в журнале останутся."))) return;
        try { await api.deleteShift(b.dataset.del); await reload(); } catch (err) { toastError(err); }
      };
    }
  };
  const reload = async () => {
    try {
      shifts = await api.shifts({ from: st.data?.from, to: st.data?.to });
    } catch (err) { toastError(err); }
    draw();
  };
  await reload();
}

export function initStatistics() {
  on("loaded", () => st.visible && load());
  on("events-cleared", () => st.visible && load());
  window.addEventListener("resize", () => st.visible && drawChart());
  // Тихое обновление, пока экран открыт: новые инциденты и замеры (без сброса выбора и прокрутки).
  setInterval(() => { if (st.visible && !document.hidden) load({ quiet: true }); }, 20000);
}

export function setStatisticsVisible(visible) {
  st.visible = visible;
  if (!visible) return hideTip();
  render();
  load();
}
