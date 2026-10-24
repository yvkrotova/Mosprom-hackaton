// «Статистика»: шесть показателей за период (по началу эпизода; инвариант ждут + подтверждено +
// ложные = всего) и отдельный блок технических замеров — по камере и её запуску, с явной средой
// выполнения (CPU / LinQ / replay с LinQ). Ноль — «данные есть, их ноль»; прочерк — «нет замера».
import { api } from "./api.js";
import { hydrateIcons, icon } from "./icons.js";
import { emit, on, sourceById, state } from "./state.js";
import {
  $, $$, envLabel, esc, fmtDateTime, fmtHM, fmtNum, helpIcon, isLinq, isReplay, stored, store, toastError,
} from "./util.js";

const PERIODS = { today: "Сегодня", day: "24 часа", week: "7 дней", all: "Всё время" };

const st = {
  period: stored("statsFilters", {}).period || "today",
  sourceId: stored("statsFilters", {}).sourceId || "",
  metricsSource: null, // какая камера показана в блоке замеров при «Все камеры»
  stats: null,
  metrics: null,
  error: null,
  loading: false,
  updatedAt: null,
  range: { from: "", to: "" },
  visible: false,
};

function rangeOf(period) {
  const now = new Date();
  if (period === "today") return { from: new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString(), to: now.toISOString() };
  if (period === "day") return { from: new Date(now - 864e5).toISOString(), to: now.toISOString() };
  if (period === "week") return { from: new Date(now - 7 * 864e5).toISOString(), to: now.toISOString() };
  return { from: "", to: "" };
}

async function load() {
  if (st.loading) return; // защита от повторного нажатия «Обновить»
  st.loading = true;
  render();
  st.range = rangeOf(st.period);
  try {
    const [stats, metrics] = await Promise.all([
      api.stats({ from: st.range.from || undefined, to: st.range.to || undefined, source_id: st.sourceId || undefined }),
      api.metrics(st.sourceId || undefined),
    ]);
    st.stats = stats;
    st.metrics = metrics;
    st.error = null;
    st.updatedAt = new Date();
  } catch (err) {
    st.error = err.message; // последние значения остаются на экране
  }
  st.loading = false;
  render();
}

// --- Показатели ------------------------------------------------------------------------------

function kpis(s) {
  const v = (x) => (x == null ? "—" : String(x));
  return [
    { cls: "ic-red", ic: "siren", title: "Нарушений за период", value: v(s?.total), sub: s ? `активных: ${s.active}` : "",
      tip: "Эпизоды, начавшиеся в выбранный период. Один эпизод — одно нарушение от начала до конца." },
    { cls: "ic-amber", ic: "bell", title: "Ждут реакции оператора", value: v(s?.unreviewed), sub: "нужно проверить",
      tip: "Эпизоды без результата проверки. Ждут + подтверждено + ложные = всего." },
    { cls: "ic-green", ic: "check", title: "Подтверждено", value: v(s?.confirmed), sub: s ? `передано мастеру: ${s.escalated}` : "",
      tip: "Подтверждённые оператором нарушения, включая переданные мастеру смены." },
    { cls: "ic-gray", ic: "x", title: "Ложных срабатываний", value: v(s?.false_positive), sub: "по результатам проверки",
      tip: "Эпизоды, которые оператор отметил как ложное срабатывание." },
    { cls: "ic-purple", ic: "clock", title: "Среднее время в зоне", value: s?.avg_zone_time_s == null ? "—" : `${fmtNum(s.avg_zone_time_s, 0)} с`,
      sub: s?.avg_zone_time_s == null ? "нет завершённых эпизодов в зоне" : "завершённые эпизоды",
      tip: "Среднее по времени видео для завершённых эпизодов «человек в опасной зоне». Идущие не учитываются." },
    { cls: "ic-blue", ic: "shield", title: "Тревог на эпизод", value: s?.alerts_per_episode == null ? "—" : fmtNum(s.alerts_per_episode, 1),
      sub: "за выбранный период", tip: "Сколько раз в среднем оповещали об одном эпизоде. Цель — 1: одно нарушение = одна тревога." },
  ];
}

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

// --- Отрисовка --------------------------------------------------------------------------------

function render() {
  const root = $("#screen-stats");
  if (!root || !st.visible) return;
  const s = st.stats?.summary;
  const demo = st.stats?.demo?.is_demo;
  const sources = st.metrics?.sources || [];
  if (!st.metricsSource || !sources.some((x) => x.source_id === st.metricsSource)) st.metricsSource = sources[0]?.source_id || null;
  const m = sources.find((x) => x.source_id === st.metricsSource) || null;
  const env = envLabel(m?.detector_kind);
  const cam = st.sourceId ? sourceById(st.sourceId)?.name || "камера" : "Все камеры";
  const periodText = st.period === "all" ? "всё время" : `${fmtDateTime(st.range.from)} — ${fmtDateTime(st.range.to)}`;

  root.innerHTML = `
    <div class="panel page-head">
      <div><h1>Статистика</h1><p>Нарушения и производительность системы</p></div>
      ${demo ? `<span class="badge badge-demo" title="${esc(st.stats.demo.reason || "")}">Демонстрационные данные</span>` : ""}
    </div>
    <div class="print-only"><b>Отчёт: статистика нарушений</b><br>Период: ${esc(periodText)} · Камера: ${esc(cam)} ·
      Сформирован: ${esc(fmtDateTime(new Date().toISOString()))}${demo ? " · Демонстрационные данные" : ""}</div>
    <div class="panel toolbar">
      <div class="seg">${Object.entries(PERIODS).map(([k, v]) => `<button data-period="${k}" class="${k === st.period ? "active" : ""}">${v}</button>`).join("")}</div>
      <select class="select" id="st-source" style="width:240px" aria-label="Камера">
        <option value="">Все камеры</option>${state.sources.map((x) => `<option value="${x.id}" ${x.id === st.sourceId ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
      <span class="spacer"></span>
      <button class="btn ${st.loading ? "loading" : ""}" id="st-refresh">${icon("refresh")}Обновить</button>
      <button class="btn" id="st-journal">${icon("list")}Открыть в журнале</button>
      <button class="btn" id="st-print">${icon("printer")}Печать отчёта</button>
    </div>
    ${st.error ? `<div class="notice notice-error">${icon("alert-triangle")}<span>Не удалось обновить: ${esc(st.error)}. ${st.stats ? "Показаны последние полученные значения." : ""}</span></div>` : ""}
    <div class="kpis">${kpis(s).map((k) => `
      <div class="panel kpi"><div class="kpi-icon ${k.cls}">${icon(k.ic)}</div>
        <div><div class="kpi-title">${esc(k.title)} ${helpIcon(k.tip)}</div>
        <div class="kpi-value">${!st.stats && st.loading ? `<span class="skeleton" style="display:inline-block;width:70px;height:36px"></span>` : esc(k.value)}</div>
        <div class="kpi-sub">${esc(k.sub)}</div></div></div>`).join("")}</div>
    <div class="panel metrics">
      <div class="metrics-head">
        <div><h2 class="section-title">${icon("cpu")}Замеры скорости детектора</h2>
          <div class="sub">${m ? `Последний замер · камера «${esc(m.source_name || "—")}»${m.run_started_at ? ` · запуск ${esc(fmtDateTime(m.run_started_at))}` : ""} · ${m.ts ? `замер ${esc(fmtDateTime(m.ts))}` : "замера нет"}` : "Камер нет"}
            · Не зависит от периода. Для теста максимальной скорости отключите «Реальное время» (REALTIME_PLAYBACK=false).</div></div>
        ${m?.detector_kind ? `<span class="badge badge-env ${env.cls}">${esc(env.text)}</span>` : ""}
      </div>
      ${!st.sourceId && sources.length > 1 ? `<div class="src-tabs no-print">${sources.map((x) => `<button class="btn btn-sm ${x.source_id === st.metricsSource ? "btn-outline-primary" : ""}" data-msrc="${x.source_id}">${esc(x.source_name || "камера")}</button>`).join("")}
        <span class="sub">Показана одна камера из ${sources.length} — это не общий показатель.</span></div>` : ""}
      ${m?.detector_kind && !isLinq(m.detector_kind) && !isReplay(m.detector_kind) ? `<div class="notice notice-warn">${icon("info")}<span>Обработка на CPU. Это <b>не замер на ускорителе LinQ</b>.</span></div>` : ""}
      ${isReplay(m?.detector_kind) ? `<div class="notice notice-info">${icon("info")}<span>${isLinq(m.detector_kind) ? "Replay — предзаписанные результаты LinQ" : "Replay — предзаписанные результаты"}: время инференса — историческое из записи${m.replay_file ? ` (${esc(m.replay_file)})` : ""}; скорость — текущее воспроизведение на этом сервере.</span></div>` : ""}
      <div class="mgrid">${metricCards(m).map((c) => `
        <div class="mcard"><div class="m-title">${esc(c.title)} ${helpIcon(c.tip)}</div>
          <div class="m-value">${esc(c.value)}</div><div class="m-sub">${esc(c.sub)}</div></div>`).join("")}</div>
    </div>
    ${demo ? `<div class="screen-footnote">Показатели событий приведены для примера</div>` : ""}`;
  hydrateIcons(root);

  for (const b of $$("[data-period]", root)) b.onclick = () => { st.period = b.dataset.period; save(); load(); };
  $("#st-source").onchange = (e) => { st.sourceId = e.target.value; save(); load(); };
  $("#st-refresh").onclick = load;
  $("#st-journal").onclick = () => emit("open-journal", { from: st.range.from, to: st.range.to, source_id: st.sourceId });
  $("#st-print").onclick = () => window.print();
  for (const b of $$("[data-msrc]", root)) b.onclick = () => { st.metricsSource = b.dataset.msrc; render(); };
}

function save() {
  store("statsFilters", { period: st.period, sourceId: st.sourceId });
}

export function initStatistics() {
  on("loaded", () => st.visible && load());
  on("sources", () => st.visible && render());
  // Замер обновляется сам, пока экран открыт (без сброса выбранного периода).
  setInterval(() => { if (st.visible && !document.hidden) load(); }, 10000);
}

export function setStatisticsVisible(visible) {
  st.visible = visible;
  if (visible) {
    render();
    load().catch(toastError);
  }
}
