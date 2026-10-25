// Точка входа: левая панель (разделы, сворачивание, настройки, звук, оператор), канал событий,
// добавление камеры. Переходы между разделами и сворачивание меню не останавливают анализ.
import { api } from "./api.js";
import { openEvent } from "./event-modal.js";
import { hydrateIcons, icon } from "./icons.js";
import { initJournal, setJournalVisible } from "./journal.js";
import { initMonitoring, setMonitoringVisible } from "./monitoring.js";
import { Socket } from "./sockets.js";
import { sound } from "./sound.js";
import {
  emit, loadZonesAndRules, on, patchSource, refreshSources, refreshUnviewed, selectSource, setOperator, setServerOnline, sites, state,
} from "./state.js";
import { initStatistics, setStatisticsVisible } from "./statistics.js";
import { $, $$, esc, label, openMenu, openModal, promptModal, setScenarios, store, stored, toast, toastError } from "./util.js";
import { confirmLeave, initZones, setZonesVisible } from "./zones.js";

const TABS = { live: setMonitoringVisible, journal: setJournalVisible, zones: setZonesVisible, stats: setStatisticsVisible };
let activeTab = null;

const hashTab = () => location.hash.slice(1).split("?")[0];

async function showTab(name, params) {
  if (!TABS[name]) name = "live";
  if (activeTab === "zones" && name !== "zones" && !(await confirmLeave())) return;
  activeTab = name;
  if (hashTab() !== name) history.replaceState(null, "", "#" + name);
  for (const btn of $$(".nav-item")) btn.classList.toggle("active", btn.dataset.tab === name);
  for (const [tab, setVisible] of Object.entries(TABS)) {
    $(`#screen-${tab}`).hidden = tab !== name;
    setVisible(tab === name, tab === name ? params : undefined);
  }
  $("#content").scrollTop = 0;
}

// --- Левая панель ------------------------------------------------------------------------------

function applyCollapsed(collapsed) {
  document.documentElement.classList.toggle("sidebar-collapsed", collapsed);
  $("#collapse-btn").innerHTML = icon(collapsed ? "chevrons-right" : "chevrons-left");
  $("#collapse-btn").title = collapsed ? "Развернуть меню" : "Свернуть меню";
  store("sidebarCollapsed", collapsed);
  // Содержимое плавно расширяется; ячейки камер сами пересчитают размеры (ResizeObserver).
}

function initials(name) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] || "О") + (parts[1]?.[0] || parts[0]?.[1] || "")).toUpperCase();
}

function renderUser() {
  $("#user-avatar").textContent = initials(state.operator);
  $("#user-btn .user-name").textContent = state.operator;
}

function openSettings() {
  const s = sound.settings;
  const { root, close } = openModal(`
    <h3 class="modal-title">${icon("settings")}Настройки</h3>
    <div class="stack">
      <label class="field"><span>Имя оператора (подписывает действия проверки)</span><input class="input" name="operator" maxlength="100" value="${esc(state.operator)}" /></label>
      <label class="field"><span>Громкость уведомлений</span><input type="range" min="0" max="1" step="0.05" name="volume" value="${s.volume}" /></label>
      <label class="status-line"><span>Сигнал при входе человека в зону (до подтверждения)</span><span class="switch"><input type="checkbox" name="entryChime" ${s.entryChime ? "checked" : ""} /><span class="knob"></span></span></label>
      <label class="status-line"><span>Голосовое сообщение о нарушении</span><span class="switch"><input type="checkbox" name="voice" ${s.voice ? "checked" : ""} /><span class="knob"></span></span></label>
      <label class="status-line"><span>Повторять сигнал, пока нет реакции</span><span class="switch"><input type="checkbox" name="repeat" ${s.repeat ? "checked" : ""} /><span class="knob"></span></span></label>
      <button class="btn" data-test>${icon("volume-2")}Проверить звук</button>
    </div>
    <div class="modal-actions"><button class="btn btn-primary" data-ok>Готово</button></div>`);
  $("input[name=volume]", root).oninput = (e) => sound.set({ volume: Number(e.target.value) });
  for (const key of ["entryChime", "voice", "repeat"]) $(`input[name=${key}]`, root).onchange = (e) => sound.set({ [key]: e.target.checked });
  $("[data-test]", root).onclick = () => sound.test();
  $("[data-ok]", root).onclick = () => {
    const name = $("input[name=operator]", root).value.trim();
    if (name) setOperator(name);
    close();
  };
}

function renderSound() {
  const on = sound.settings.enabled;
  $("#sound-icon").innerHTML = icon(on ? "volume-2" : "volume-x");
  $("#sound-text").textContent = on ? "Звук включён" : "Звук выключен";
  $("#sound-toggle").checked = on;
  $("#sound-unlock").hidden = !on || sound.unlocked;
}

// --- Тревоги ---------------------------------------------------------------------------------

function onAlarm(ev) {
  const where = ev.zone_name ? ` в зоне «${ev.zone_name}»` : "";
  sound.siren(`Внимание! ${label("scenario", ev.scenario_type)}${where}. ${ev.source_name || ""}.`);
  toast(`${label("scenario", ev.scenario_type)}: ${ev.source_name || ""}${ev.zone_name ? " · " + ev.zone_name : ""}`, "alarm", {
    timeout: 10000,
    action: { label: "Смотреть эпизод", run: () => openEvent(ev.id) },
  });
}

function updateTitle() {
  const n = state.unviewed.total;
  document.title = n ? `(${n}) LinQ Видеоконтроль` : "LinQ Видеоконтроль";
}

function onEventsMessage(msg) {
  const { type, data } = msg;
  if (type === "event.deleted") {
    emit("event-deleted", data.id);
    refreshUnviewed();
  } else if (type.startsWith("event.")) {
    emit("event", data);
    if (type === "event.created") { onAlarm(data); refreshUnviewed(); }
    if (type === "event.emergency") {
      // Все операторы видят, что службы уже вызваны (не звонят повторно).
      toast(`Вызваны экстренные службы: ${data.emergency_service} · ${data.source_name || ""} · ${data.emergency_by || ""}`, "alarm", {
        timeout: 12000, action: { label: "Открыть событие", run: () => openEvent(data.id) },
      });
    }
  } else if (type === "events.cleared") {
    emit("events-cleared", data);
    refreshUnviewed();
  } else if (type === "events.viewed") {
    refreshUnviewed();
  } else if (type === "source.status") {
    patchSource({ id: data.id, status: data.status, last_error: data.last_error, last_heartbeat_at: data.last_heartbeat_at });
    if (data.status === "error") toast(`${data.name}: ошибка анализа — ${data.last_error || "без описания"}`, "error", { timeout: 12000 });
    if (data.status === "disconnected") toast(`${data.name}: нет связи с модулем анализа`, "error", { timeout: 10000 });
  }
}

async function pollHealth() {
  try {
    state.health = await api.health();
    setServerOnline(true);
  } catch {
    setServerOnline(false);
  }
}

// --- Добавление камеры --------------------------------------------------------------------------

// stay — добавили из «Камер и зон»: остаёмся там (анализ новой камеры виден в редакторе).
function openAddCamera(opts = {}) {
  const { root, close } = openModal(`
    <h3 class="modal-title">${icon("plus")}Добавить камеру</h3>
    <div class="stack">
      <label class="field"><span>Название камеры</span><input class="input" name="name" placeholder="Например, Камера 03" maxlength="200" /></label>
      <label class="field"><span>Участок</span><input class="input" name="site" list="site-list" placeholder="Например, Цех №2" maxlength="200" />
        <datalist id="site-list">${sites().map((s) => `<option value="${esc(s)}"></option>`).join("")}</datalist></label>
      <label class="field"><span>Видеофайл (mp4, avi, mov, mkv, webm)</span><input class="input" type="file" name="file" accept=".mp4,.avi,.mov,.mkv,.webm,video/*" style="padding-top:10px" /></label>
      <label class="field"><span>Частота анализа, кадр/с</span><input class="input" type="number" name="fps" value="10" min="1" max="60" step="1" /></label>
      <label class="field"><span>Предзаписанные детекции LinQ (необязательно, .jsonl)</span>
        <input class="input" type="file" name="replay" accept=".jsonl" style="padding-top:10px" />
        <span class="hint">Файл из linq/run_linq_detections.py для этого же видео: анализ пойдёт по записи с LinQ (replay).</span></label>
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
    if (!file || !name) return toast("Укажите название и выберите видеофайл", "info", { timeout: 2500 });
    const form = new FormData();
    form.append("file", file);
    form.append("name", name);
    const site = $("input[name=site]", root).value.trim();
    if (site) form.append("site", site);
    form.append("analysis_fps", $("input[name=fps]", root).value || "10");
    const replay = $("input[name=replay]", root).files[0];
    if (replay) form.append("replay", replay);
    const bar = $(".progress", root);
    bar.hidden = false;
    $("[data-ok]", root).disabled = true;
    try {
      const src = await api.uploadSource(form, (p) => ($(".progress div", root).style.width = `${Math.round(p * 100)}%`));
      close();
      await refreshSources();
      await loadZonesAndRules(src.id);
      selectSource(src.id);
      const running = src.status === "running";
      toast(`Камера «${src.name}» добавлена${running ? ", анализ запущен" : ""}. Зоны настраиваются в разделе «Камеры и зоны».`, "success", { timeout: 6000 });
      if (!opts?.stay) {
        emit("show-camera", src.id);
        showTab("live");
      }
    } catch (err) {
      toastError(err);
      $("[data-ok]", root).disabled = false;
      bar.hidden = true;
    }
  };
}

// --- Запуск -----------------------------------------------------------------------------------

let bootstrapped = false;
async function bootstrap() {
  try {
    [state.scenarios] = await Promise.all([api.scenarios(), refreshSources()]);
    setScenarios(state.scenarios);
    await Promise.all(state.sources.map((s) => loadZonesAndRules(s.id)));
    bootstrapped = true;
    state.loaded = true;
    emit("loaded");
    refreshUnviewed();
  } catch (err) {
    if (err.code !== "NETWORK") toastError(err);
  }
}

async function main() {
  hydrateIcons(document);
  applyCollapsed(Boolean(stored("sidebarCollapsed", false)));
  $("#collapse-btn").onclick = () => applyCollapsed(!document.documentElement.classList.contains("sidebar-collapsed"));
  for (const btn of $$(".nav-item")) btn.onclick = () => showTab(btn.dataset.tab);
  $("#settings-btn").onclick = openSettings;
  $("#sound-toggle").onchange = (e) => { sound.set({ enabled: e.target.checked }); if (e.target.checked) sound.unlock(); };
  $("#sound-unlock-btn").onclick = () => sound.test();
  sound.addEventListener("change", renderSound);
  renderSound();
  renderUser();
  on("operator", renderUser);
  $("#user-btn").onclick = (e) => openMenu(e.currentTarget, [
    { label: "Сменить оператора смены", icon: "user", run: async () => {
      const name = await promptModal("Кто дежурит?", { value: state.operator, placeholder: "Фамилия И.О." });
      if (name) setOperator(name);
    } },
    { label: "Настройки", icon: "settings", run: openSettings },
  ]);

  initMonitoring($("#screen-live"));
  initJournal($("#screen-journal"));
  initZones($("#screen-zones"));
  initStatistics($("#screen-stats"));

  on("add-camera", openAddCamera);
  on("open-journal", (params) => showTab("journal", params));
  on("unviewed", updateTitle);
  on("source-selected", (id) => loadZonesAndRules(id).catch(toastError));
  on("server", (online) => {
    $("#offline-banner").hidden = online;
    document.body.classList.toggle("offline", !online);
    if (online && !bootstrapped) bootstrap();
  });

  // «Повторять, пока нет реакции»: сигнал каждые 8 с, пока есть непросмотренные события.
  setInterval(() => { if (sound.settings.repeat && state.unviewed.total) sound.siren(); }, 8000);

  await bootstrap();
  // Ссылка на карточку: #journal?event=<id> (или любой раздел с ?event=).
  const linked = new URLSearchParams(location.hash.split("?")[1] || "").get("event");
  const tab = { territory: "zones", analytics: "stats" }[hashTab()] || hashTab();
  await showTab(tab || "live");
  if (linked) openEvent(linked);
  pollHealth();
  setInterval(pollHealth, 5000);
  setInterval(refreshUnviewed, 15000);

  new Socket("/ws/events", {
    onMessage: onEventsMessage,
    onStatus: (connected) => {
      state.wsConnected = connected;
      if (connected) { refreshSources().catch(() => {}); refreshUnviewed(); }
      pollHealth();
    },
  });
}

main();
