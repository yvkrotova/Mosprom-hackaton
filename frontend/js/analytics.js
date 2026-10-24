// Вкладка «Статистика»: показатели, замеры скорости детектора, графики по типам, камерам, зонам и часам.
import { api } from "./api.js";
import { barChart, donutChart, hbarChart } from "./charts.js";
import { hydrateIcons, icon } from "./icons.js";
import { emit, on, state } from "./state.js";
import { $, $$, esc, fmtDateTime, fmtDuration, fmtNum, isLinq, label, pct, episodeSeconds } from "./util.js";

const PERIODS = { today: "Сегодня", day: "24 часа", week: "7 дней", all: "Всё время" };
const REVIEW_COLORS = { unreviewed: "#f59e0b", confirmed: "#ef4444", false_positive: "#64748b", escalated: "#a78bfa" };

const an = {
  period: "today",
  sourceId: "",
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
      <button class="btn btn-sm" id="an-journal"><i data-icon="list"></i>Открыть в журнале</button>
      <button class="btn btn-sm" id="an-print"><i data-icon="printer"></i>Печать отчёта</button>
    </div>
    <div id="an-error"></div>

    <div class="kpis" id="kpis"></div>

    <div class="card" style="margin-bottom:18px">
      <div class="card-head">
        <div><h3 class="card-title"><i data-icon="cpu"></i>Замеры скорости детектора</h3>
          <p class="card-sub">Последний heartbeat модуля анализа. Для замера максимальной скорости запускайте анализ без режима «Реальное время».</p></div>
        <span id="det-kind"></span>
      </div>
      <div id="det-warning"></div>
      <div class="linq-panel" id="det-panel"></div>
    </div>

    <div class="charts">
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="activity"></i>Нарушения по часам (местное время)</h3><div id="ch-hours"></div></div>
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="check-circle"></i>Результаты проверки</h3><div id="ch-review"></div></div>
    </div>
    <div class="charts-3">
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="target"></i>По зонам</h3><div id="ch-zones"></div></div>
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="video"></i>По камерам</h3><div id="ch-sources"></div></div>
      <div class="card"><h3 class="card-title" style="margin-bottom:12px"><i data-icon="alert-triangle"></i>По типам нарушений</h3><div id="ch-types"></div></div>
    </div>`;
  hydrateIcons(root);

  for (const btn of $$("[data-period]", root)) {
    btn.onclick = () => { an.period = btn.dataset.period; refresh(); };
  }
  $("#an-source").onchange = (e) => { an.sourceId = e.target.value; refresh(); };
  $("#an-refresh").onclick = refresh;
  $("#an-print").onclick = printReport;
  $("#an-journal").onclick = () => {
    const params = new URLSearchParams({ ...periodRange(an.period), ...(an.sourceId ? { source_id: an.sourceId } : {}) });
    history.replaceState(null, "", "#journal?" + params);
    emit("go-tab", "journal");
  };

  on("sources", renderSourceFilter);
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

async function refresh() {
  an.stale = false;
  $$("[data-period]").forEach((b) => b.classList.toggle("active", b.dataset.period === an.period));
  renderSourceFilter();
  const q = { ...periodRange(an.period), source_id: an.sourceId || undefined };
  try {
    const [stats, metrics, sample] = await Promise.all([
      api.stats(q),
      api.metrics(an.sourceId || undefined),
      api.events({ ...q, limit: 500 }),
    ]);
    $("#an-error").innerHTML = "";
    renderKpis(stats, metrics, sample);
    renderDetector(metrics);
    renderCharts(stats);
  } catch (err) {
    // Ошибка не должна выглядеть как «нарушений нет»: показываем её на месте данных.
    $("#an-error").innerHTML = `<div class="notice notice-err" style="margin-bottom:16px">${icon("alert-triangle")}<span>Статистика не загружена: ${esc(err.message)}</span></div>`;
  }
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
    kpi("Нарушений за период", stats.total, active ? `${active} идёт прямо сейчас` : "активных нет", "siren", "red", active ? "var(--red-text)" : undefined),
    kpi("Ждут реакции оператора", rv.unreviewed, rv.unreviewed ? "требуется проверка" : "все проверены", "bell", "amber", rv.unreviewed ? "var(--amber-text)" : "var(--green-text)"),
    kpi("Подтверждено", rv.confirmed, `передано мастеру: ${rv.escalated}`, "check", "red"),
    kpi("Ложных срабатываний", rv.false_positive, reviewed ? `точность по проверке ${pct(precision)}` : "нет проверенных событий", "x", "green"),
    kpi("Среднее время в зоне", avgDur == null ? "—" : fmtDuration(avgDur), ended.length ? `по ${ended.length} завершённым эпизодам` : "нет завершённых эпизодов", "clock", "purple"),
    kpi("Тревог на эпизод", ape == null ? "—" : fmtNum(ape, 2), ape == null ? "нет данных" : ape <= 1.0001 ? "повторы подавлены" : "есть повторные тревоги", "shield", "blue", ape != null && ape <= 1.0001 ? "var(--green-text)" : undefined),
  ].join("");
}

function renderDetector(metrics) {
  const s = metrics.sources.find((x) => x.fps != null) || metrics.sources[0] || {};
  const l = metrics.latency;
  const kind = s.detector_kind || state.health?.detector_kind;
  $("#det-kind").innerHTML = kind
    ? `<span class="badge ${isLinq(kind) ? "badge-green" : "badge-amber"}">${esc(label("detector", kind))}</span>`
    : "";
  $("#det-warning").innerHTML = kind && !isLinq(kind)
    ? `<div class="notice notice-warn" style="margin-bottom:12px">${icon("info")}<span>Это <b>не замер на LinQ</b>: детектор «${esc(label("detector", kind))}». ${kind === "onnx_cpu" ? "YOLOv8s считается на процессоре этого компьютера." : "Нейросеть не работает, детекции подставлены."} Цифры для LinQ появятся при DETECTOR=linq.</span></div>`
    : "";
  const m = (title, value, note) => `<div class="metric"><div class="metric-label">${title}</div><div class="metric-value">${value}</div><div class="metric-note">${note}</div></div>`;
  $("#det-panel").innerHTML = [
    m("Скорость обработки", fmtNum(s.fps, 1, " FPS"), s.source_name ? esc(s.source_name) : "анализ ещё не запускался"),
    m("Инференс, среднее", fmtNum(s.inference_ms_avg, 1, " мс"), "только вызов нейросети"),
    m("Инференс, p95", fmtNum(s.inference_ms_p95, 1, " мс"), "95% кадров быстрее"),
    m("Пре- / постпроцессинг", s.preprocess_ms_avg == null ? "—" : `${fmtNum(s.preprocess_ms_avg, 1)} / ${fmtNum(s.postprocess_ms_avg, 1)} мс`, "letterbox · декодирование и NMS"),
    m("Кадров обработано", s.frames_processed ?? "—", esc(s.model_version || "модель —")),
    m("Обнаружение (видео)", fmtNum(l.detection_delay_video_ts_s_avg, 2, " с"), "от входа в зону до подтверждения"),
    m("Обнаружение (реально)", fmtNum(l.detection_delay_real_s_avg, 2, " с"), "честно в режиме реального времени"),
    m("Задержка уведомления", fmtNum(l.notify_delay_ms_avg, 1, " мс"), `p95 ${fmtNum(l.notify_delay_ms_p95, 1, " мс")} · до отправки тревоги`),
    m("Тревог на эпизод", fmtNum(l.alerts_per_episode, 2), `эпизодов в замере: ${l.events_count}`),
    m("Обновлено", s.ts ? fmtDateTime(s.ts) : "—", "время последнего замера"),
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

function renderSourceFilter() {
  const sel = $("#an-source");
  if (!sel) return;
  sel.innerHTML = `<option value="">Все камеры</option>` + state.sources.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  sel.value = an.sourceId;
}

function printReport() {
  const cam = an.sourceId ? state.sources.find((s) => s.id === an.sourceId)?.name : "все камеры";
  $("#print-meta").textContent = `Период: ${PERIODS[an.period]} · Камеры: ${cam} · Сформирован: ${fmtDateTime(new Date().toISOString())} · Оператор: ${state.operator}`;
  window.print();
}
