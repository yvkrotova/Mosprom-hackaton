// «Мониторинг»: сетка камер 1–9 (живой кадр + разметка на canvas в координатах кадра) и панель
// событий участка. Переключение сетки, раскрытие камеры и переходы не останавливают анализ —
// он идёт в воркерах backend; здесь только просмотр.
import { api } from "./api.js";
import { mediaUrl } from "./config.js";
import { openEmergency } from "./emergency.js";
import { openEvent } from "./event-modal.js";
import { hydrateIcons, icon } from "./icons.js";
import { Socket } from "./sockets.js";
import { sound } from "./sound.js";
import { drawTrack, drawZone, Stage, zoneColor } from "./stage.js";
import { emit, liveSources, on, patchSource, refreshSources, refreshUnviewed, sites, sourceById, state } from "./state.js";
import { $, $$, envLabel, esc, fmtHM, isReplay, label, openMenu, plural, scenarioInfo, stored, store, toast, toastError } from "./util.js";

const INTERP_MS = 120; // рамка плавно переезжает к новому положению — без рывков
const TRAIL_LEN = 30;
const FRESH_MS = 3000; // кадр старше — считаем, что живого потока нет
const MAX_CELLS = 9;

const view = {
  root: null,
  visible: false,
  n: Math.min(MAX_CELLS, Math.max(1, stored("gridN", 4))),
  assign: stored("gridCells", null), // source_id по ячейкам (null — пусто)
  cells: [],
  expanded: null, // индекс раскрытой ячейки
  filter: "all", // панель событий: all | new
  events: new Map(),
  selected: null,
  eventsError: null,
  eventsLoaded: false,
  // Лента событий: свёрнута ли и с какого момента «очищена» (старые карточки скрыты, события — в журнале).
  eventsHidden: Boolean(stored("eventsHidden", false)),
  clearedAt: stored("eventsClearedAt", null),
};

// --- Раскладка сетки ---------------------------------------------------------------------------

// Колонки и строки для N ячеек (по ТЗ); для 3 — 3×1 или 2+1, что крупнее при текущей области.
function layoutFor(n, width, height) {
  if (n === 1) return { cols: 1, rows: 1 };
  if (n === 2) return { cols: 2, rows: 1 };
  if (n === 3) {
    const cell = (cols, rows) => Math.min(width / cols, (height / rows) * 1.6);
    return cell(3, 1) >= cell(2, 2) ? { cols: 3, rows: 1 } : { cols: 2, rows: 2, twoPlusOne: true };
  }
  if (n === 4) return { cols: 2, rows: 2 };
  if (n <= 6) return { cols: 3, rows: 2 };
  return { cols: 3, rows: 3 };
}

function applyLayout() {
  const grid = $("#cam-grid");
  if (!grid) return;
  const count = view.expanded != null ? 1 : view.n;
  const box = grid.parentElement.getBoundingClientRect();
  const top = grid.getBoundingClientRect().top - box.top;
  const width = box.width, height = Math.max(240, box.height - top);
  const { cols, rows, twoPlusOne } = layoutFor(count, width, height);
  const gap = 14;
  // Пропорции кадра 16:10: ячейка — наибольшая, что помещается в область по ширине и высоте.
  const cellW = Math.max(160, Math.min((width - gap * (cols - 1)) / cols, ((height - gap * (rows - 1)) / rows) * 1.6));
  grid.style.width = `${cellW * cols + gap * (cols - 1)}px`;
  grid.style.margin = "0 auto";
  if (twoPlusOne) {
    grid.style.gridTemplateColumns = "repeat(4, minmax(0, 1fr))";
    $$(".cam-cell", grid).forEach((el, i) => (el.style.gridColumn = i < 2 ? "span 2" : "2 / span 2"));
  } else {
    grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
    $$(".cam-cell", grid).forEach((el) => (el.style.gridColumn = ""));
  }
}

// Источники участка (выбранного в «Мониторинге»); "" — все.
const siteSources = () => liveSources().filter((s) => !state.site || s.site === state.site);

// Назначение ячеек: сохранённое; новые ячейки заполняются камерами участка без повторов.
function ensureAssignment() {
  let assign = Array.isArray(view.assign) ? view.assign.slice(0, MAX_CELLS) : [];
  const known = new Set(liveSources().map((s) => s.id));
  assign = assign.map((id) => (id && known.has(id) ? id : null));
  while (assign.length < MAX_CELLS) assign.push(null);
  if (!Array.isArray(view.assign)) {
    siteSources().slice(0, view.n).forEach((s, i) => (assign[i] = s.id));
  }
  view.assign = assign;
  store("gridCells", assign);
}

// --- Ячейка камеры -----------------------------------------------------------------------------

class Cell {
  constructor(index) {
    this.index = index;
    this.el = document.createElement("div");
    this.el.className = "cam-cell";
    this.stage = new Stage(this.el);
    this.sourceId = null;
    this.socket = null;
    this.frame = null;
    this.frameAt = 0;
    this.anim = new Map();
    this.trails = new Map();
    this.trackStates = new Map();
    this.overlay = document.createElement("div");
    this.el.append(this.overlay);
  }

  setSource(id) {
    if (id === this.sourceId) return this.renderOverlay();
    this.socket?.close();
    this.socket = null;
    this.sourceId = id;
    this.frame = null;
    this.anim.clear();
    this.trails.clear();
    this.trackStates.clear();
    this.stage.setImage(null);
    if (id) {
      this.loadPreview();
      this.socket = new Socket(`/ws/live/${id}`, { onMessage: (msg) => this.onMessage(msg) });
    }
    this.renderOverlay();
  }

  reconnect() {
    const id = this.sourceId;
    this.sourceId = null;
    this.setSource(id);
    refreshSources().catch(() => {});
  }

  loadPreview() {
    const id = this.sourceId;
    const img = new Image();
    img.onload = () => { if (!this.frame && this.sourceId === id) this.stage.setImage(img); };
    img.src = api.previewUrl(id);
  }

  onMessage(msg) {
    if (msg.data?.source_id !== this.sourceId) return;
    if (msg.type === "live.ended") {
      this.frame = null;
      this.trails.clear();
      this.loadPreview();
      this.renderOverlay();
      return;
    }
    if (msg.type !== "live.frame") return;
    const data = msg.data;
    const apply = () => {
      const now = performance.now();
      const seen = new Set();
      for (const t of data.tracks) {
        seen.add(t.track_id);
        const prev = this.anim.get(t.track_id);
        this.anim.set(t.track_id, { from: prev ? lerpBox(prev, now) : t.bbox, to: t.bbox, t0: now });
        const trail = this.trails.get(t.track_id) || [];
        trail.push([(t.bbox[0] + t.bbox[2]) / 2, t.bbox[3]]);
        if (trail.length > TRAIL_LEN) trail.shift();
        this.trails.set(t.track_id, trail);
        const prevState = this.trackStates.get(t.track_id) || "normal";
        if (prevState === "normal" && t.state !== "normal") sound.chime();
        this.trackStates.set(t.track_id, t.state);
      }
      for (const id of [...this.anim.keys()]) if (!seen.has(id)) { this.anim.delete(id); this.trails.delete(id); }
      const kindChanged = this.frame?.detector_kind !== data.detector_kind;
      this.frame = data;
      this.frameAt = now;
      if (kindChanged) this.renderOverlay();
    };
    if (data.image) {
      const img = new Image();
      img.onload = () => { this.stage.setImage(img, data.width, data.height); apply(); };
      img.src = data.image;
    } else {
      apply();
    }
  }

  kindLabel(src) {
    const kind = this.frame?.detector_kind;
    if (isReplay(kind) || (!kind && src.replay_path)) {
      return { text: kind ? envLabel(kind).text : "Replay — предзаписанные результаты", cls: "replay" };
    }
    if (["error", "disconnected"].includes(src.status)) return { text: "Нет сигнала", cls: "bad" };
    return { text: src.kind === "rtsp" ? "Прямой эфир" : "Запись", cls: "" };
  }

  renderOverlay() {
    const src = this.sourceId ? sourceById(this.sourceId) : null;
    const expanded = view.expanded === this.index;
    if (!src) {
      this.el.classList.remove("alarm");
      this.overlay.innerHTML = `<div class="cam-empty">${icon("monitor")}
        <div style="display:flex;gap:10px;flex-wrap:wrap;justify-content:center">
          <button class="btn btn-sm" data-pick>${icon("grid")}Выбрать камеру</button>
          <button class="btn btn-sm" data-add>${icon("plus")}Добавить камеру</button></div></div>`;
      $("[data-pick]", this.overlay).onclick = (e) => pickCamera(this.index, e.currentTarget);
      $("[data-add]", this.overlay).onclick = () => emit("add-camera");
      return;
    }
    const bad = ["error", "disconnected"].includes(src.status);
    const running = src.status === "running";
    const kind = this.kindLabel(src);
    const dot = src.status === "running" ? "on" : bad ? "bad" : "";
    this.overlay.innerHTML = `
      <div class="cam-label" title="${esc(src.name)}${src.site ? " · " + esc(src.site) : ""}">
        <span class="dot ${dot}"></span><span class="ellipsis">${esc(src.name)}${src.site ? ` · ${esc(src.site)}` : ""}</span></div>
      <div class="cam-tools">
        ${expanded ? `<button class="icon-btn" data-back title="Назад к сетке">${icon("arrow-left")}</button>` : ""}
        <button class="icon-btn" data-menu title="Камера в ячейке">${icon("more-vertical")}</button>
        <button class="icon-btn" data-expand title="${expanded ? "Полный экран" : "Раскрыть камеру"}">${icon(expanded ? "maximize" : "expand")}</button>
      </div>
      <div class="cam-kind ${kind.cls}">${esc(kind.text)}</div>
      ${!running && !bad ? `<button class="btn btn-primary cam-start" data-start title="Запустить анализ этой камеры здесь же">${icon("play")}Запустить</button>` : ""}
      ${bad ? `<div class="cam-nosignal">${icon("wifi-off")}<b>Нет сигнала</b>
        <span>${esc(label("status", src.status))}${src.last_error ? ": " + esc(src.last_error) : ""}</span>
        <div style="display:flex;gap:8px"><button class="btn btn-sm" data-retry>${icon("refresh")}Переподключиться</button>
        ${src.status === "error" ? `<button class="btn btn-sm" data-restart>${icon("play")}Запустить анализ</button>` : ""}</div></div>` : ""}`;
    $("[data-expand]", this.overlay).onclick = () => (expanded ? this.el.requestFullscreen?.() : expand(this.index));
    const back = $("[data-back]", this.overlay);
    if (back) back.onclick = () => expand(null);
    $("[data-menu]", this.overlay).onclick = (e) => openMenu(e.currentTarget, [
      { label: "Сменить камеру", icon: "grid", run: () => pickCamera(this.index, e.target.closest("button")) },
      { label: "Развернуть на весь экран", icon: "maximize", run: () => this.el.requestFullscreen?.() },
      running
        ? { label: "Остановить анализ", icon: "stop", run: () => this.stop() }
        : { label: "Запустить анализ", icon: "play", run: () => this.start() },
      { label: "Убрать из ячейки", icon: "x", run: () => assignCell(this.index, null) },
    ]);
    const startBtn = $("[data-start]", this.overlay);
    if (startBtn) startBtn.onclick = () => this.start(startBtn);
    const retry = $("[data-retry]", this.overlay);
    if (retry) retry.onclick = () => this.reconnect();
    const restart = $("[data-restart]", this.overlay);
    if (restart) restart.onclick = () => this.start(restart);
  }

  // Запуск анализа прямо в ячейке: остаёмся на «Мониторинге», живой кадр придёт в эту же ячейку
  // (сокет /ws/live этой камеры уже открыт и переподключается сам).
  async start(btn) {
    if (btn) { btn.disabled = true; btn.innerHTML = `${icon("refresh")}Запускаем…`; }
    try {
      patchSource(await api.startSource(this.sourceId, true));
      toast(`Анализ «${sourceById(this.sourceId)?.name || "камеры"}» запущен`, "success", { timeout: 2500 });
    } catch (err) {
      toastError(err);
      this.renderOverlay();
    }
  }

  async stop() {
    try {
      patchSource(await api.stopSource(this.sourceId));
    } catch (err) {
      toastError(err);
    }
  }

  draw(now) {
    if (!this.stage.width) return;
    const { stage } = this;
    stage.drawBackground();
    if (!this.sourceId) return;
    const fresh = this.frame && now - this.frameAt < FRESH_MS;
    const tracks = fresh ? this.frame.tracks : [];
    const zoneStates = fresh ? this.frame.zone_states || [] : [];
    const pulse = (Math.sin(now / 180) + 1) / 2;
    (state.zones[this.sourceId] || []).forEach((z, i) => {
      const { state: zstate, reason } = zoneView(z, tracks, zoneStates);
      // Цвет зоны — по назначению (scenario.color из GET /scenarios).
      const color = scenarioInfo(z.scenario_types?.[0])?.color || zoneColor(i);
      drawZone(stage, z, color, { state: zstate, pulse, reason });
    });
    for (const t of tracks) {
      const anim = this.anim.get(t.track_id);
      const flags = t.flags || [];
      drawTrack(stage, anim ? { ...t, bbox: lerpBox(anim, now) } : t, {
        boxes: true,
        skeleton: false,
        trail: flags.includes("trail") || t.state === "alert" ? this.trails.get(t.track_id) : null,
      });
    }
    const alarm = tracks.some((t) => t.state === "alert") || zoneStates.some((z) => z.state === "violation");
    if (alarm !== this.el.classList.contains("alarm")) this.el.classList.toggle("alarm", alarm);
  }

  destroy() {
    this.socket?.close();
    this.el.remove();
  }
}

function lerpBox({ from, to, t0 }, now) {
  const k = Math.min(1, (now - t0) / INTERP_MS);
  return to.map((v, i) => from[i] + (v - from[i]) * k);
}

// Вид зоны по zone_states кадра (одинаково для всех сценариев).
function zoneView(zone, tracks, zoneStates) {
  const mine = zoneStates.filter((s) => s.zone_id === zone.id);
  const has = (st) => mine.find((s) => s.state === st);
  if (has("violation") || tracks.some((t) => t.zone_ids.includes(zone.id) && t.state === "alert")) return { state: "alert" };
  if (has("candidate")) return { state: "candidate" };
  if (tracks.some((t) => t.zone_ids.includes(zone.id))) return { state: "in_zone" };
  const unknown = has("unknown");
  if (unknown) return { state: "unknown", reason: unknown.reason };
  return { state: "normal" };
}

function assignCell(index, sourceId) {
  view.assign[index] = sourceId;
  store("gridCells", view.assign);
  view.cells[index]?.setSource(sourceId);
}

function pickCamera(index, anchor) {
  const used = new Set(view.assign.slice(0, view.n).filter(Boolean));
  const list = siteSources();
  if (!list.length) return emit("add-camera");
  openMenu(anchor, list.map((s) => ({
    label: `${s.name}${s.site ? " · " + s.site : ""}${used.has(s.id) && view.assign[index] !== s.id ? " (уже в другой ячейке)" : ""}`,
    icon: "video",
    run: () => assignCell(index, s.id),
  })));
}

function expand(index) {
  view.expanded = index;
  renderGrid();
}

// --- Сетка ------------------------------------------------------------------------------------

function renderGrid() {
  const grid = $("#cam-grid");
  if (!grid) return;
  ensureAssignment();
  const wanted = view.expanded != null ? [view.expanded] : [...Array(view.n).keys()];
  // Ячейки переиспользуются: их сокеты и кадры не теряются при смене сетки.
  for (const [i, cell] of view.cells.entries()) {
    if (cell && i >= view.n) { cell.destroy(); view.cells[i] = null; }
  }
  grid.innerHTML = "";
  if (!liveSources().length) {
    grid.style.width = "";
    grid.innerHTML = `<div class="panel empty" style="grid-column:1/-1">${icon("video")}<b>Камер пока нет</b>
      <span>Добавьте видео или поток камеры — анализ начнётся автоматически.</span>
      <button class="btn btn-primary" data-add>${icon("plus")}Добавить камеру</button></div>`;
    $("[data-add]", grid).onclick = () => emit("add-camera");
    return;
  }
  for (const i of wanted) {
    let cell = view.cells[i];
    if (!cell) cell = view.cells[i] = new Cell(i);
    grid.append(cell.el);
    cell.setSource(view.assign[i]);
    cell.renderOverlay();
  }
  applyLayout();
  for (const i of wanted) view.cells[i].stage.resize();
}

function renderBar() {
  const bar = $("#monitor-bar");
  if (!bar) return;
  const list = siteSources();
  const online = list.filter((s) => s.status === "running").length;
  const allSites = sites();
  bar.innerHTML = `
    <label class="site-picker">${icon("building")}
      <select class="select" id="site-select" aria-label="Участок">
        <option value="">Все участки · Все камеры</option>
        ${allSites.map((s) => `<option value="${esc(s)}" ${s === state.site ? "selected" : ""}>${esc(s)} · Все камеры</option>`).join("")}
      </select></label>
    <div class="grid-picker">
      <button class="btn" id="grid-btn" aria-haspopup="true">${icon("grid")}Камер: ${view.n}${icon("chevron-down")}</button>
      <div class="grid-pop" id="grid-pop" hidden><div class="sub">Количество камер</div>
        <div class="nums">${[...Array(MAX_CELLS).keys()].map((i) => `<button data-n="${i + 1}" class="${i + 1 === view.n ? "active" : ""}">${i + 1}</button>`).join("")}</div></div>
    </div>
    <button class="icon-btn" id="grid-full" title="Сетка на весь экран">${icon("expand")}</button>
    <button class="btn btn-primary" id="add-cam">${icon("plus")}Добавить камеру</button>
    <button class="btn btn-emergency" id="sos-btn" title="Вызов экстренных служб при происшествии">${icon("phone")}Экстренный вызов</button>
    <span class="online-note"><span class="dot ${online ? "on" : ""}"></span>${online} ${plural(online, "камера", "камеры", "камер")} онлайн</span>`;
  $("#site-select").onchange = (e) => {
    state.site = e.target.value;
    store("monitorSite", state.site);
    view.assign = null; // участок сменился — заполняем ячейки его камерами
    renderBar();
    renderGrid();
    loadEvents();
    refreshUnviewed();
  };
  const pop = $("#grid-pop");
  $("#grid-btn").onclick = (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; };
  for (const b of $$("[data-n]", pop)) {
    b.onclick = () => {
      view.n = Number(b.dataset.n);
      store("gridN", view.n);
      view.expanded = null;
      pop.hidden = true;
      // Новые ячейки — свободными камерами участка (без повторов).
      const used = new Set(view.assign.slice(0, view.n).filter(Boolean));
      const free = siteSources().filter((s) => !used.has(s.id));
      for (let i = 0; i < view.n; i++) if (!view.assign[i] && free.length) view.assign[i] = free.shift().id;
      store("gridCells", view.assign);
      renderBar();
      renderGrid();
    };
  }
  $("#grid-full").onclick = () => $("#cam-area").requestFullscreen?.();
  $("#add-cam").onclick = () => emit("add-camera");
  // Выбрано событие в панели — вызов записывается в его историю; иначе только номера служб.
  $("#sos-btn").onclick = () => openEmergency(view.selected ? view.events.get(view.selected) : null);
}

// --- Панель событий -----------------------------------------------------------------------------

async function loadEvents() {
  try {
    const page = await api.events({ limit: 50, site: state.site || undefined, viewed: view.filter === "new" ? false : undefined });
    view.events.clear();
    for (const ev of page.items) view.events.set(ev.id, ev);
    view.eventsError = null;
  } catch (err) {
    view.eventsError = err.message;
  }
  view.eventsLoaded = true;
  renderEvents();
}

function eventMatches(ev) {
  const src = sourceById(ev.source_id);
  if (state.site && src?.site !== state.site) return false;
  if (view.clearedAt && new Date(ev.started_at) <= new Date(view.clearedAt)) return false;
  return view.filter !== "new" || !ev.viewed_at;
}

function renderEvents() {
  const list = $("#events-list");
  if (!list) return;
  const unviewed = state.unviewed.total;
  $("#events-new").textContent = `${unviewed} ${plural(unviewed, "новое", "новых", "новых")}`;
  $("#events-new").hidden = !unviewed;
  $("#events-rail-count").textContent = unviewed;
  $("#events-rail-count").hidden = !unviewed;
  for (const b of $$("[data-filter]")) b.classList.toggle("active", b.dataset.filter === view.filter);
  if (!view.eventsLoaded) {
    list.innerHTML = [1, 2, 3].map(() => `<div class="skeleton" style="height:210px"></div>`).join("");
    return;
  }
  if (view.eventsError) {
    list.innerHTML = `<div class="empty">${icon("alert-triangle")}<b>События не загружены</b><span>${esc(view.eventsError)}</span>
      <button class="btn btn-sm" data-retry>${icon("refresh")}Повторить</button></div>`;
    $("[data-retry]", list).onclick = loadEvents;
    return;
  }
  const items = [...view.events.values()].filter(eventMatches)
    .sort((a, b) => new Date(b.started_at) - new Date(a.started_at));
  if (!items.length) {
    list.innerHTML = view.clearedAt
      ? `<div class="empty">${icon("check-circle")}<b>Лента очищена</b><span>Новые события появятся здесь. Все события — в журнале.</span>
          <button class="btn btn-sm" data-unclear>${icon("undo")}Показать скрытые</button></div>`
      : `<div class="empty">${icon("check-circle")}<b>${view.filter === "new" ? "Новых событий нет" : "Событий пока нет"}</b></div>`;
    const unclear = $("[data-unclear]", list);
    if (unclear) unclear.onclick = () => setCleared(null);
    return;
  }
  list.innerHTML = items.map((ev) => {
    const src = sourceById(ev.source_id);
    const sel = ev.id === view.selected;
    return `<div class="ev-card ${sel ? "selected" : ""}" data-id="${ev.id}">
      <div class="ev-top"><span class="ev-time">${fmtHM(ev.started_at)}</span>
        ${ev.viewed_at ? `<span class="pill pill-gray pill-sm">Просмотрено</span>` : `<span class="pill pill-blue pill-sm">Новое</span>`}
        <button class="menu-btn ev-menu" data-menu title="Действия">${icon("more-vertical")}</button></div>
      <div class="ev-thumb">${ev.thumbnail_url ? `<img src="${esc(mediaUrl(ev.thumbnail_url))}" alt="Кадр нарушения" loading="lazy" />`
        : `<div class="no-frame">Кадр недоступен</div>`}</div>
      <div class="ev-title">${esc(label("scenario", ev.scenario_type))}</div>
      <div class="ev-meta">${esc(ev.source_name || "удалённая камера")}${src?.site ? ` · ${esc(src.site)}` : ""}${ev.state === "active" ? " · идёт" : ""}</div>
      ${ev.emergency_at ? `<div class="ev-meta" style="color:#ff8585">${icon("phone")} Службы вызваны: ${esc(ev.emergency_service)}</div>` : ""}
      ${sel ? `<button class="btn btn-primary btn-block ev-watch" data-watch>${icon("play-circle")}Смотреть эпизод</button>
        <button class="btn btn-emergency btn-block ev-watch" data-sos>${icon("phone")}Вызвать экстренные службы</button>` : ""}
    </div>`;
  }).join("");
  for (const card of $$(".ev-card", list)) {
    const id = card.dataset.id;
    card.onclick = (e) => {
      if (e.target.closest("[data-menu]") || e.target.closest("[data-watch]") || e.target.closest("[data-sos]")) return;
      view.selected = id;
      renderEvents();
    };
    const watch = $("[data-watch]", card);
    if (watch) watch.onclick = () => openEvent(id);
    const sos = $("[data-sos]", card);
    if (sos) sos.onclick = () => openEmergency(view.events.get(id));
    $("[data-menu]", card).onclick = (e) => {
      e.stopPropagation();
      const ev = view.events.get(id);
      openMenu(e.currentTarget, [
        { label: "Открыть в журнале", icon: "journal", run: () => emit("open-journal", { event: id }) },
        { label: "Перейти к камере", icon: "video", run: () => showCamera(ev.source_id) },
      ]);
    };
  }
  hydrateIcons(list);
}

function setCleared(value) {
  view.clearedAt = value;
  store("eventsClearedAt", value);
  renderEvents();
}

// «Очистить» ленту: карточки уходят, события отмечаются просмотренными (счётчик «новых» = 0).
// Из журнала ничего не удаляется.
async function clearFeed() {
  try {
    await api.viewAll(state.site || undefined);
  } catch (err) {
    return toastError(err);
  }
  for (const ev of view.events.values()) ev.viewed_at ||= new Date().toISOString();
  setCleared(new Date().toISOString());
  refreshUnviewed();
  toast("Лента очищена — события остаются в журнале", "success", {
    timeout: 5000, action: { label: "Вернуть", run: () => setCleared(null) },
  });
}

// «Скрыть» ленту: панель сворачивается в узкую полосу, камеры занимают всю ширину.
function setEventsHidden(hidden) {
  view.eventsHidden = hidden;
  store("eventsHidden", hidden);
  $(".monitor")?.classList.toggle("events-hidden", hidden);
  requestAnimationFrame(() => {
    applyLayout();
    for (const cell of view.cells) cell?.stage.resize();
  });
}

// Камера события — в ячейку и раскрыть; если её нет в сетке — в первую ячейку.
function showCamera(sourceId) {
  let index = view.assign.slice(0, view.n).indexOf(sourceId);
  if (index < 0) {
    index = 0;
    assignCell(0, sourceId);
  }
  expand(index);
}

// --- Жизненный цикл -----------------------------------------------------------------------------

function loop(now) {
  requestAnimationFrame(loop);
  if (!view.visible || document.hidden) return;
  for (const cell of view.cells) if (cell?.el.isConnected) cell.draw(now);
}

export function initMonitoring(root) {
  view.root = root;
  root.innerHTML = `
    <div class="monitor ${view.eventsHidden ? "events-hidden" : ""}">
      <div class="monitor-main" id="cam-area">
        <div class="panel monitor-bar" id="monitor-bar"></div>
        <div class="cam-grid" id="cam-grid"></div>
      </div>
      <aside class="panel events-panel" aria-label="События">
        <button class="events-rail" id="events-show" title="Показать ленту событий">${icon("chevrons-left")}
          <span class="badge badge-count" id="events-rail-count" hidden></span><span class="rail-text">События</span></button>
        <div class="events-head"><h2>События</h2><span class="badge badge-count" id="events-new" hidden></span>
          <span class="events-tools">
            <button class="icon-btn" id="events-clear" title="Очистить ленту (события останутся в журнале)">${icon("trash")}</button>
            <button class="icon-btn" id="events-hide" title="Скрыть ленту событий">${icon("chevrons-right")}</button></span></div>
        <div class="events-tabs"><button data-filter="all">Все</button><button data-filter="new">Новые</button></div>
        <div class="events-list" id="events-list"></div>
        <button class="events-all" id="events-all">${icon("list")}Все события${icon("chevron-right")}</button>
      </aside>
    </div>`;
  for (const b of $$("[data-filter]", root)) b.onclick = () => { view.filter = b.dataset.filter; loadEvents(); };
  $("#events-all").onclick = () => emit("open-journal", { site: state.site });
  $("#events-clear").onclick = clearFeed;
  $("#events-hide").onclick = () => setEventsHidden(true);
  $("#events-show").onclick = () => setEventsHidden(false);
  on("events-cleared", () => { view.events.clear(); renderEvents(); });
  on("event-deleted", (id) => { view.events.delete(id); renderEvents(); });
  document.addEventListener("click", (e) => {
    const pop = $("#grid-pop");
    if (pop && !e.target.closest(".grid-picker")) pop.hidden = true;
  });
  new ResizeObserver(() => view.visible && applyLayout()).observe(root);

  on("loaded", () => { renderBar(); renderGrid(); loadEvents(); });
  on("sources", () => {
    renderBar();
    for (const cell of view.cells) cell?.renderOverlay();
    if (!$("#cam-grid .cam-cell") || !state.sources.length) renderGrid();
  });
  on("unviewed", renderEvents);
  on("event", (ev) => {
    const known = view.events.get(ev.id);
    view.events.set(ev.id, { ...known, ...ev }); // обновление эпизода — та же карточка
    renderEvents();
  });
  on("show-camera", showCamera);
  requestAnimationFrame(loop);
}

export function setMonitoringVisible(visible) {
  view.visible = visible;
  if (visible) {
    renderBar();
    renderGrid();
    if (view.eventsLoaded) loadEvents();
  }
}
