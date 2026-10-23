// Точка входа: вкладки, шапка с телеметрией, канал тревог, звук, загрузка видео.
import { initAnalytics, setAnalyticsVisible } from "./analytics.js";
import { api } from "./api.js";
import { openEvent } from "./event-modal.js";
import { hydrateIcons, icon } from "./icons.js";
import { connectLive, initLive, setLiveVisible } from "./live.js";
import { Socket } from "./sockets.js";
import { sound } from "./sound.js";
import {
  currentSource, emit, loadZonesAndRules, on, patchSource, refreshSources, selectSource, setOperator, state,
} from "./state.js";
import { initTerritory, setTerritoryVisible } from "./territory.js";
import { $, $$, esc, fmtNum, label, openModal, promptModal, toast, toastError } from "./util.js";

const TABS = { live: setLiveVisible, territory: setTerritoryVisible, analytics: setAnalyticsVisible };
let activeTab = "live";

function showTab(name) {
  if (!TABS[name]) name = "live";
  activeTab = name;
  for (const btn of $$(".tab")) btn.classList.toggle("active", btn.dataset.tab === name);
  for (const [tab, setVisible] of Object.entries(TABS)) {
    $(`#screen-${tab}`).hidden = tab !== name;
    setVisible(tab === name);
  }
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  if (name === "live") hideAlarmBanner();
}

// --- Тревоги ---------------------------------------------------------------------------------

function alarmText(ev) {
  const zone = ev.zone_name ? ` в зоне ${ev.zone_name}` : "";
  return `Внимание! ${label("scenario", ev.scenario_type)}${zone}. ${ev.source_name || ""}.`;
}

function onAlarm(ev) {
  sound.siren(alarmText(ev));
  const elsewhere = ev.source_id !== state.sourceId || activeTab !== "live";
  toast(`${label("scenario", ev.scenario_type)}: ${ev.source_name} • ${ev.zone_name || "без зоны"}`, "alarm", {
    timeout: 10000,
    action: { label: "Открыть", run: () => openEvent(ev.id) },
  });
  if (elsewhere) showAlarmBanner(ev);
}

function showAlarmBanner(ev) {
  const banner = $("#alarm-banner");
  banner.innerHTML = `${icon("siren")}<span>ТРЕВОГА: ${esc(label("scenario", ev.scenario_type))} — ${esc(ev.source_name || "")} • ${esc(ev.zone_name || "")}</span>
    <button class="btn btn-sm btn-danger">Показать камеру</button>
    <button class="btn btn-sm btn-ghost" data-close>${icon("x")}</button>`;
  banner.hidden = false;
  $("button.btn-danger", banner).onclick = () => { selectSource(ev.source_id); showTab("live"); };
  $("[data-close]", banner).onclick = hideAlarmBanner;
}

function hideAlarmBanner() {
  $("#alarm-banner").hidden = true;
}

function updateTitle() {
  const n = [...state.feed.values()].filter((e) => e.review_status === "unreviewed").length;
  document.title = n ? `(${n}) ⚠ LinQ Safety` : "LinQ Safety";
}

function onEventsMessage(msg) {
  const { type, data } = msg;
  if (type.startsWith("event.")) {
    state.feed.set(data.id, data);
    emit("event", data);
    if (type === "event.created") onAlarm(data);
  } else if (type === "source.status") {
    patchSource({ id: data.id, status: data.status, last_error: data.last_error, last_heartbeat_at: data.last_heartbeat_at });
    if (data.status === "error") toast(`${data.name}: ошибка анализа — ${data.last_error || "без описания"}`, "error", { timeout: 12000 });
    if (data.status === "disconnected") toast(`${data.name}: нет связи с модулем анализа`, "error", { timeout: 10000 });
  }
}

async function loadFeed() {
  try {
    const page = await api.events({ limit: 40 });
    state.feed.clear();
    for (const ev of page.items) state.feed.set(ev.id, ev);
    emit("feed-loaded");
  } catch (err) {
    toastError(err);
  }
}

// --- Телеметрия в шапке ------------------------------------------------------------------------

function setChip(id, cls, value) {
  const chip = $(id);
  chip.classList.remove("ok", "warn", "bad");
  if (cls) chip.classList.add(cls);
  if (value !== undefined) $("b", chip).textContent = value;
}

async function pollHealth() {
  try {
    state.health = await api.health();
    const bad = state.health.sources.filter((s) => ["error", "disconnected"].includes(s.status));
    setChip("#chip-health", state.health.status === "ok" ? "ok" : "bad");
    $("#chip-health").title = state.health.db !== "ok" ? "База данных недоступна"
      : bad.length ? `Проблемы: ${bad.map((s) => `${s.name} — ${label("status", s.status)}`).join("; ")}` : "Все системы в норме";
    $("#chip-health span:last-child").textContent = state.health.db !== "ok" ? "БД недоступна" : bad.length ? `Сбой: ${bad.length}` : "Система";
    const kind = state.health.detector_kind;
    setChip("#chip-detector", kind === "linq" ? "ok" : "warn", label("detector", kind));
  } catch {
    setChip("#chip-health", "bad");
    $("#chip-health span:last-child").textContent = "Backend недоступен";
  }
}

async function pollMetrics() {
  if (!state.sourceId) return;
  try {
    state.metrics = await api.metrics(state.sourceId);
    const s = state.metrics.sources[0];
    const running = currentSource()?.status === "running";
    if (!running || !liveFps) setChip("#chip-fps", s?.fps ? "ok" : null, fmtNum(s?.fps, 1));
    setChip("#chip-infer", s?.inference_ms_avg ? "ok" : null,
      s?.inference_ms_avg ? `${fmtNum(s.inference_ms_avg, 1)} / ${fmtNum(s.inference_ms_p95, 1)} мс` : "—");
    const l = state.metrics.latency;
    setChip("#chip-delay", l.notify_delay_ms_avg != null ? "ok" : null, l.notify_delay_ms_avg != null ? `${fmtNum(l.notify_delay_ms_avg, 0)} мс` : "—");
  } catch { /* шапка просто не обновится */ }
}

let liveFps = null;
on("live-stats", ({ fps }) => {
  liveFps = fps;
  if (fps != null) setChip("#chip-fps", "ok", fmtNum(fps, 1));
});

// --- Звук ---------------------------------------------------------------------------------

function renderSoundButton() {
  const s = sound.settings;
  const audible = s.enabled && sound.unlocked;
  $("#sound-btn-icon").innerHTML = icon(audible ? "volume-2" : "volume-x");
  $("#sound-btn-text").textContent = !s.enabled ? "Звук: ВЫКЛ" : sound.unlocked ? "Звук: ВКЛ" : "Звук: заблокирован";
  $("#sound-btn").classList.toggle("ok", audible);
  $("#sound-btn").classList.toggle("bad", !audible);
  $("#sound-unlock").hidden = !s.enabled || sound.unlocked;
  for (const input of $$("[data-sound]")) {
    const key = input.dataset.sound;
    if (input.type === "checkbox") input.checked = s[key];
    else input.value = s[key];
  }
}

function initSound() {
  const pop = $("#sound-popover");
  $("#sound-btn").onclick = (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; };
  document.addEventListener("click", (e) => { if (!e.target.closest(".sound-wrap")) pop.hidden = true; });
  for (const input of $$("[data-sound]")) {
    input.onchange = input.oninput = () => sound.set({ [input.dataset.sound]: input.type === "checkbox" ? input.checked : Number(input.value) });
  }
  $("#sound-test").onclick = () => sound.test();
  $("#sound-unlock-btn").onclick = () => sound.test();
  sound.addEventListener("change", renderSoundButton);
  renderSoundButton();

  // «Повторять, пока нет реакции»: сирена каждые 8 с, пока есть идущие непроверенные тревоги.
  setInterval(() => {
    if (!sound.settings.repeat) return;
    const waiting = [...state.feed.values()].find((e) => e.state === "active" && e.review_status === "unreviewed");
    if (waiting) sound.siren();
  }, 8000);
}

// --- Загрузка видео -------------------------------------------------------------------------

function openUpload() {
  const { root, close } = openModal(`
    <h3 class="modal-title">Добавить видео камеры</h3>
    <div style="display:flex;flex-direction:column;gap:10px">
      <label class="field"><span>Название камеры</span><input class="input" name="name" placeholder="Камера 3 — цех штамповки" maxlength="200" /></label>
      <label class="field"><span>Площадка</span><input class="input" name="site" placeholder="Цех №2" maxlength="200" /></label>
      <label class="field"><span>Видеофайл (mp4, avi, mov, mkv, webm)</span><input class="input" type="file" name="file" accept=".mp4,.avi,.mov,.mkv,.webm,video/*" /></label>
      <label class="field"><span>Частота анализа, кадр/с</span><input class="input" type="number" name="fps" value="10" min="1" max="60" step="1" /></label>
    </div>
    <div class="progress" hidden><div></div></div>
    <div class="modal-actions"><button class="btn" data-cancel>Отмена</button><button class="btn btn-primary" data-ok>${icon("upload")}Загрузить</button></div>`);
  $("[data-cancel]", root).onclick = close;
  $("input[name=file]", root).onchange = (e) => {
    const nameInput = $("input[name=name]", root);
    if (!nameInput.value && e.target.files[0]) nameInput.value = e.target.files[0].name.replace(/\.[^.]+$/, "");
  };
  $("[data-ok]", root).onclick = async () => {
    const file = $("input[name=file]", root).files[0];
    const name = $("input[name=name]", root).value.trim();
    if (!file || !name) return toast("Укажите название и выберите файл", "info", { timeout: 2500 });
    const form = new FormData();
    form.append("file", file);
    form.append("name", name);
    const site = $("input[name=site]", root).value.trim();
    if (site) form.append("site", site);
    form.append("analysis_fps", $("input[name=fps]", root).value || "10");
    const bar = $(".progress", root);
    bar.hidden = false;
    $("[data-ok]", root).disabled = true;
    try {
      const src = await api.uploadSource(form, (p) => ($(".progress div", root).style.width = `${Math.round(p * 100)}%`));
      close();
      await refreshSources();
      selectSource(src.id);
      showTab("territory");
      toast(`Камера «${src.name}» добавлена. Обведите опасную зону на кадре.`, "success", { timeout: 6000 });
    } catch (err) {
      toastError(err);
      $("[data-ok]", root).disabled = false;
      bar.hidden = true;
    }
  };
}

// --- Запуск -----------------------------------------------------------------------------------

async function main() {
  hydrateIcons(document);
  initSound();
  initLive($("#screen-live"));
  initTerritory($("#screen-territory"));
  initAnalytics($("#screen-analytics"));

  for (const btn of $$(".tab")) btn.onclick = () => showTab(btn.dataset.tab);
  on("go-tab", showTab);
  $("#upload-btn").onclick = openUpload;
  $("#operator-name").textContent = state.operator;
  $("#operator-btn").onclick = async () => {
    const name = await promptModal("Кто дежурит?", { value: state.operator, placeholder: "Фамилия И.О." });
    if (name) setOperator(name);
  };
  on("operator", () => ($("#operator-name").textContent = state.operator));
  on("event", updateTitle);
  on("feed-loaded", updateTitle);
  on("source-selected", (id) => { loadZonesAndRules(id).catch(toastError); pollMetrics(); });

  try {
    [state.scenarios] = await Promise.all([api.scenarios(), refreshSources()]);
    await Promise.all(state.sources.map((s) => loadZonesAndRules(s.id)));
  } catch (err) {
    toastError(err);
  }
  showTab(location.hash.slice(1) || "live");
  connectLive();
  loadFeed();
  pollHealth();
  pollMetrics();
  setInterval(pollHealth, 5000);
  setInterval(pollMetrics, 3000);

  new Socket("/ws/events", {
    onMessage: onEventsMessage,
    onStatus: (connected) => {
      state.wsConnected = connected;
      const chip = $("#chip-ws");
      chip.classList.toggle("ok", connected);
      chip.classList.toggle("bad", !connected);
      chip.innerHTML = `${icon(connected ? "wifi" : "wifi-off")}<span>${connected ? "Тревоги онлайн" : "Тревоги: переподключение…"}</span>`;
      // После переподключения догружаем то, что могли пропустить.
      if (connected) { loadFeed(); refreshSources().catch(() => {}); }
    },
  });
}

main();
