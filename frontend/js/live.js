// Вкладка «Наблюдение»: живой кадр с зонами, рамками, скелетом и следом; лента тревог.
import { api } from "./api.js";
import { mediaUrl } from "./config.js";
import { act, openEvent, reviewBadge, stateBadge } from "./event-modal.js";
import { hydrateIcons, icon } from "./icons.js";
import { Socket } from "./sockets.js";
import { sound } from "./sound.js";
import { drawTrack, drawZone, Stage, zoneColor } from "./stage.js";
import { currentSource, emit, on, patchSource, selectSource, state } from "./state.js";
import { $, $$, canDo, episodeSeconds, esc, fmtDuration, fmtNum, fmtTime, label, stored, store, toastError } from "./util.js";

const TRAIL_LEN = 30;

const view = {
  stage: null,
  socket: null,
  frame: null, // последний live.frame
  frameAt: 0,
  trails: new Map(), // track_id -> [[x,y], ...]
  trackStates: new Map(),
  toggles: stored("liveToggles", { zones: true, boxes: true, skeleton: true, trail: true }),
  realtime: stored("realtime", true),
  feedFilter: "all",
  visible: true,
};

export function initLive(root) {
  root.innerHTML = `
    <div class="live-grid">
      <div class="card">
        <div class="cam-head">
          <div class="cam-title"><span class="rec" id="rec"></span><span id="cam-title">Камера</span></div>
          <select class="select" id="cam-select" aria-label="Камера"></select>
          <span id="cam-status"></span>
          <label class="chip chip-btn" title="Файл проигрывается в реальном темпе — тревоги приходят «вживую». Выключите для замера максимального FPS.">
            <input type="checkbox" id="realtime" /> Реальное время</label>
          <button class="btn btn-sm btn-success" id="start-btn"><i data-icon="play"></i>Запустить</button>
          <button class="btn btn-sm btn-danger" id="stop-btn"><i data-icon="stop"></i>Остановить</button>
        </div>

        <div class="stage" id="live-stage">
          <div class="overlay overlay-tl" id="ov-info"></div>
          <div class="overlay overlay-tr" id="ov-ts"></div>
          <div class="overlay overlay-center" id="ov-center" hidden></div>
          <div class="overlay overlay-bottom">
            <div class="toggles">
              <label><input type="checkbox" data-toggle="zones" />Зоны</label>
              <label><input type="checkbox" data-toggle="boxes" />Рамки</label>
              <label><input type="checkbox" data-toggle="skeleton" />Скелет (поза)</label>
              <label><input type="checkbox" data-toggle="trail" />След движения</label>
            </div>
            <div>Территория: <span class="zone-status safe" id="zone-status">—</span></div>
          </div>
        </div>

        <div class="cam-foot">
          <div class="legend-inline">
            <span><i class="sw" style="background:#38bdf8"></i>Человек вне зоны</span>
            <span><i class="sw" style="background:#f59e0b"></i>Вошёл в зону — идёт подтверждение</span>
            <span><i class="sw" style="background:#ef4444"></i>Тревога (эпизод подтверждён)</span>
            <span><i class="sw" style="background:#fff;border-radius:50%"></i>Точка контроля — ноги</span>
          </div>
          <button class="btn btn-sm" id="sound-check"><i data-icon="volume-2"></i>Проверить тревогу</button>
        </div>
        <div class="cams-strip" id="cams-strip"></div>
      </div>

      <div class="card">
        <div class="card-head" style="margin-bottom:10px">
          <h2 class="card-title"><i data-icon="bell"></i>Лента тревог</h2>
          <span class="badge badge-red" id="feed-count">0 без реакции</span>
        </div>
        <div class="feed-filters">
          <button class="btn btn-sm active" data-filter="all">Все</button>
          <button class="btn btn-sm" data-filter="unreviewed">Без реакции</button>
          <button class="btn btn-sm" data-filter="active">Идут сейчас</button>
        </div>
        <div class="feed" id="feed"></div>
      </div>
    </div>`;
  hydrateIcons(root);

  view.stage = new Stage($("#live-stage"));
  $("#realtime").checked = view.realtime;
  $("#realtime").onchange = (e) => { view.realtime = e.target.checked; store("realtime", view.realtime); };
  for (const box of $$("[data-toggle]", root)) {
    box.checked = view.toggles[box.dataset.toggle];
    box.onchange = () => { view.toggles[box.dataset.toggle] = box.checked; store("liveToggles", view.toggles); };
  }
  $("#cam-select").onchange = (e) => selectSource(e.target.value);
  $("#start-btn").onclick = () => start();
  $("#stop-btn").onclick = () => stop();
  $("#sound-check").onclick = () => sound.test();
  for (const btn of $$("[data-filter]", root)) {
    btn.onclick = () => {
      view.feedFilter = btn.dataset.filter;
      $$("[data-filter]", root).forEach((b) => b.classList.toggle("active", b === btn));
      renderFeed();
    };
  }

  on("sources", () => { renderCameraControls(); renderOverlay(); });
  on("source-selected", () => connectLive());
  on("zones", () => renderOverlay());
  on("event", () => scheduleFeed());
  on("feed-loaded", () => renderFeed());

  setInterval(() => { tickDurations(); renderOverlay(); }, 1000);
  requestAnimationFrame(loop);
}

export function setLiveVisible(visible) {
  view.visible = visible;
  if (visible) view.stage?.resize();
}

// --- Живой поток ------------------------------------------------------------------------------

export function connectLive() {
  view.socket?.close();
  view.frame = null;
  view.trails.clear();
  view.trackStates.clear();
  renderCameraControls();
  const src = currentSource();
  if (!src) return renderOverlay();
  loadPreview(src.id);
  view.socket = new Socket(`/ws/live/${src.id}`, { onMessage: onLiveMessage });
  renderOverlay();
}

function loadPreview(sourceId) {
  const img = new Image();
  img.onload = () => { if (!view.frame && state.sourceId === sourceId) view.stage.setImage(img); };
  img.onerror = () => { if (!view.frame) view.stage.setImage(null); };
  img.src = api.previewUrl(sourceId);
}

function onLiveMessage(msg) {
  if (msg.data?.source_id !== state.sourceId) return;
  if (msg.type === "live.ended") {
    view.frame = null;
    view.trails.clear();
    loadPreview(state.sourceId);
    renderOverlay();
    return;
  }
  if (msg.type !== "live.frame") return;
  const data = msg.data;
  const apply = () => {
    view.frame = data;
    view.frameAt = performance.now();
    updateTrails(data.tracks);
    detectEntries(data.tracks);
    emit("live-stats", { fps: data.fps });
  };
  if (data.image) {
    const img = new Image();
    img.onload = () => { view.stage.setImage(img, data.width, data.height); apply(); };
    img.src = data.image;
  } else {
    apply();
  }
}

function updateTrails(tracks) {
  const seen = new Set();
  for (const t of tracks) {
    seen.add(t.track_id);
    const trail = view.trails.get(t.track_id) || [];
    trail.push([(t.bbox[0] + t.bbox[2]) / 2, t.bbox[3]]);
    if (trail.length > TRAIL_LEN) trail.shift();
    view.trails.set(t.track_id, trail);
  }
  for (const id of view.trails.keys()) if (!seen.has(id)) view.trails.delete(id);
}

// Сигнал при входе человека в зону (ещё до подтверждения тревоги).
function detectEntries(tracks) {
  for (const t of tracks) {
    const prev = view.trackStates.get(t.track_id) || "normal";
    if (prev === "normal" && t.state !== "normal") sound.chime();
    view.trackStates.set(t.track_id, t.state);
  }
}

// --- Отрисовка ------------------------------------------------------------------------------

function loop(now) {
  requestAnimationFrame(loop);
  if (!view.visible || document.hidden || !view.stage?.width) return;
  const { stage, frame, toggles } = view;
  stage.drawBackground();
  const zones = state.zones[state.sourceId] || [];
  const fresh = frame && now - view.frameAt < 3000;
  const tracks = fresh ? frame.tracks : [];
  const pulse = (Math.sin(now / 180) + 1) / 2;

  if (toggles.zones) {
    zones.forEach((z, i) => {
      const inZone = tracks.filter((t) => t.zone_ids.includes(z.id));
      const zstate = inZone.some((t) => t.state === "alert") ? "alert" : inZone.length ? "in_zone" : "normal";
      drawZone(stage, z, zoneColor(i), { state: zstate, pulse });
    });
  }
  for (const t of tracks) {
    drawTrack(stage, t, {
      boxes: toggles.boxes,
      skeleton: toggles.skeleton,
      trail: toggles.trail ? view.trails.get(t.track_id) : null,
    });
  }

  const alerting = tracks.filter((t) => t.state === "alert");
  const inzone = tracks.filter((t) => t.state === "in_zone");
  const stageEl = $("#live-stage");
  stageEl.classList.toggle("alarm", alerting.length > 0);
  stageEl.classList.toggle("inzone", !alerting.length && inzone.length > 0);
  const status = $("#zone-status");
  if (alerting.length) {
    setIf(status, "className", "zone-status alarm");
    setIf(status, "textContent", `⚠ ТРЕВОГА: ${alerting.length} чел. в зоне ${zoneNames(alerting)}`);
  } else if (inzone.length) {
    setIf(status, "className", "zone-status inzone");
    setIf(status, "textContent", `В зоне ${zoneNames(inzone)} — подтверждение…`);
  } else {
    setIf(status, "className", "zone-status safe");
    setIf(status, "textContent", fresh ? `БЕЗОПАСНО · людей в кадре: ${tracks.length}` : "—");
  }

  const ts = $("#ov-ts");
  const tsHtml = fresh
    ? `<span class="badge badge-red">● LIVE</span><span>${fmtNum(frame.video_ts, 1)} с</span>`
    : `<span class="badge badge-gray">ПРЕВЬЮ</span>`;
  if (ts.dataset.html !== tsHtml) { ts.dataset.html = tsHtml; ts.innerHTML = tsHtml; }
}

// Цикл отрисовки идёт 60 раз в секунду — DOM трогаем, только если значение изменилось.
function setIf(el, prop, value) {
  if (el[prop] !== value) el[prop] = value;
}

function zoneNames(tracks) {
  const zones = state.zones[state.sourceId] || [];
  const ids = new Set(tracks.flatMap((t) => t.zone_ids));
  return zones.filter((z) => ids.has(z.id)).map((z) => `«${z.name}»`).join(", ");
}

// --- Шапка камеры, статусы, подсказки -------------------------------------------------------

function renderCameraControls() {
  const select = $("#cam-select");
  if (!select) return;
  select.innerHTML = state.sources.map((s) => `<option value="${s.id}">${esc(s.name)}${s.site ? " · " + esc(s.site) : ""}</option>`).join("")
    || `<option value="">Нет источников</option>`;
  select.value = state.sourceId || "";
  const src = currentSource();
  $("#cam-title").textContent = src ? `${src.name}${src.site ? " — " + src.site : ""}` : "Нет источников";
  const running = src && ["running", "disconnected"].includes(src.status);
  $("#rec").classList.toggle("on", !!running);
  $("#start-btn").disabled = !src || running;
  $("#stop-btn").disabled = !src || !running;
  const cls = { running: "badge-green", error: "badge-red", disconnected: "badge-amber" }[src?.status] || "badge-gray";
  $("#cam-status").innerHTML = src ? `<span class="badge ${cls}">${esc(label("status", src.status))}</span>` : "";

  const alarmed = new Set([...state.feed.values()].filter((e) => e.state === "active").map((e) => e.source_id));
  $("#cams-strip").innerHTML = state.sources.map((s) => `
    <button class="cam-tile ${s.id === state.sourceId ? "active" : ""} ${alarmed.has(s.id) ? "alarm" : ""}" data-src="${s.id}">
      ${icon(alarmed.has(s.id) ? "siren" : "video")}
      <div style="min-width:0"><div class="t-name">${esc(s.name)}</div>
      <div class="t-status">${esc(label("status", s.status))}${alarmed.has(s.id) ? " · ТРЕВОГА" : ""}</div></div>
    </button>`).join("");
  for (const tile of $$("[data-src]", $("#cams-strip"))) tile.onclick = () => selectSource(tile.dataset.src);
}

function renderOverlay() {
  const src = currentSource();
  const center = $("#ov-center");
  if (!center) return;
  const fresh = view.frame && performance.now() - view.frameAt < 3000;
  const zones = state.zones[state.sourceId] || [];
  const det = state.metrics?.sources?.[0];
  $("#ov-info").innerHTML = `
    <div style="color:var(--green)">${icon("cpu")} ${esc(det?.model_version || "модель —")}</div>
    <div class="muted">Детектор: <span style="color:var(--text)">${esc(label("detector", state.health?.detector_kind))}</span></div>
    <div class="muted">Зон: <span style="color:var(--text)">${zones.length}</span> · людей: <span style="color:var(--text)">${fresh ? view.frame.tracks.length : "—"}</span></div>`;

  let html = "", cls = "";
  if (!state.sources.length) {
    html = `${icon("upload")}<b>Нет источников видео</b><span class="muted">Загрузите запись смены, чтобы начать</span>
      <button class="btn btn-primary" data-go="upload">${icon("upload")}Добавить видео</button>`;
  } else if (src?.status === "error") {
    cls = "err";
    html = `${icon("alert-triangle")}<b>Анализ остановлен из-за ошибки</b>
      <div class="err-text">${esc(src.last_error || "Причина неизвестна")}</div>
      <span>Нарушения сейчас НЕ контролируются</span>
      <button class="btn btn-danger" data-go="start">${icon("refresh")}Перезапустить</button>`;
  } else if (src?.status === "disconnected") {
    cls = "warn";
    html = `${icon("wifi-off")}<b>Нет связи с модулем анализа</b>
      <span class="muted">Последний сигнал: ${src.last_heartbeat_at ? fmtTime(src.last_heartbeat_at) : "—"}. Нарушения сейчас НЕ контролируются.</span>`;
  } else if (src?.status === "running" && !fresh) {
    html = `${icon("activity")}<b>Запуск анализа…</b><span class="muted">Ждём первые кадры от нейросети</span>`;
  } else if (src && !fresh) {
    html = `${icon("play")}<b>Анализ не запущен</b><span class="muted">${src.status === "stopped" ? "Видео обработано или анализ остановлен." : "Запустите анализ, чтобы видеть людей и тревоги."}</span>
      <button class="btn btn-success" data-go="start">${icon("play")}Запустить анализ</button>`;
  }
  if (src && !zones.length && !html) {
    cls = "warn";
    html = `${icon("pen")}<b>У камеры нет опасных зон</b><span class="muted">Нарушения не ищутся. Разметьте территорию.</span>
      <button class="btn btn-warn" data-go="territory">${icon("pen")}Разметить зону</button>`;
  }
  center.hidden = !html;
  center.className = `overlay overlay-center ${cls}`;
  if (center.dataset.html !== html) {
    center.dataset.html = html;
    center.innerHTML = html;
    for (const btn of $$("[data-go]", center)) {
      btn.onclick = () => {
        const go = btn.dataset.go;
        if (go === "start") start();
        else if (go === "upload") $("#upload-btn").click();
        else emit("go-tab", go);
      };
    }
  }
}

async function start() {
  const src = currentSource();
  if (!src) return;
  try {
    patchSource(await api.startSource(src.id, view.realtime));
  } catch (err) {
    toastError(err);
  }
}

async function stop() {
  const src = currentSource();
  if (!src) return;
  try {
    patchSource(await api.stopSource(src.id));
  } catch (err) {
    toastError(err);
  }
}

// --- Лента тревог -------------------------------------------------------------------------------

let feedScheduled = false;
function scheduleFeed() {
  if (feedScheduled) return;
  feedScheduled = true;
  requestAnimationFrame(() => { feedScheduled = false; renderFeed(); renderCameraControls(); });
}

// Тикает только текст длительности активных эпизодов — без перерисовки карточек.
function tickDurations() {
  for (const el of $$(".inc-dur[data-start]")) {
    el.textContent = "идёт " + fmtDuration((Date.now() - new Date(el.dataset.start)) / 1000);
  }
}

function renderFeed() {
  const feed = $("#feed");
  if (!feed) return;
  let items = [...state.feed.values()].sort((a, b) => b.started_at.localeCompare(a.started_at));
  const unreviewed = items.filter((e) => e.review_status === "unreviewed").length;
  $("#feed-count").textContent = `${unreviewed} без реакции`;
  if (view.feedFilter === "unreviewed") items = items.filter((e) => e.review_status === "unreviewed");
  if (view.feedFilter === "active") items = items.filter((e) => e.state === "active");

  if (!items.length) {
    feed.innerHTML = `<div class="empty">${icon("check-circle")}<div>Тревог нет</div>
      <div class="faint">Новые нарушения появятся здесь мгновенно</div></div>`;
    return;
  }
  feed.innerHTML = items.map((ev) => {
    const hot = ev.review_status === "unreviewed";
    const dur = ev.state === "active"
      ? `<span class="inc-dur" data-start="${esc(ev.started_at)}">идёт ${fmtDuration((Date.now() - new Date(ev.started_at)) / 1000)}</span>`
      : `<span>${fmtDuration(episodeSeconds(ev))}</span>`;
    return `<div class="inc ${hot ? "hot" : "done"}" data-ev="${ev.id}">
      ${ev.thumbnail_url ? `<img class="inc-thumb" src="${esc(mediaUrl(ev.thumbnail_url))}" alt="" loading="lazy" />` : `<div class="inc-thumb ph">${icon("film")}</div>`}
      <div class="inc-body">
        <div class="inc-top"><span class="inc-title">${esc(label("scenario", ev.scenario_type))}</span><span class="inc-time">${fmtTime(ev.started_at)}</span></div>
        <div class="inc-where">${esc(ev.source_name || "—")} • ${esc(ev.zone_name || "без зоны")}</div>
        <div class="inc-meta">${stateBadge(ev)}${reviewBadge(ev.review_status)}
          ${dur}<span>· ${Math.round(ev.confidence * 100)}%</span></div>
      </div>
      ${hot ? `<div class="inc-actions">
        <button class="btn btn-sm btn-success" data-act="confirm">${icon("check")}Подтвердить</button>
        <button class="btn btn-sm" data-act="mark_false">${icon("x")}Ложное</button>
        <button class="btn btn-sm" data-act="escalate" ${canDo("escalate", ev.review_status) ? "" : "disabled"}>${icon("arrow-up")}</button>
      </div>` : ""}
    </div>`;
  }).join("");
  for (const card of $$("[data-ev]", feed)) {
    card.onclick = (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn) {
        e.stopPropagation();
        btn.disabled = true;
        act(card.dataset.ev, btn.dataset.act);
      } else {
        openEvent(card.dataset.ev);
      }
    };
  }
}
