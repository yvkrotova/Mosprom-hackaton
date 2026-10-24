// Вкладка «Наблюдение»: живой кадр с зонами, рамками, скелетом и следом; лента тревог.
import { api } from "./api.js";
import { mediaUrl } from "./config.js";
import { act, openEvent, priorityBadge, reviewBadge, stateBadge } from "./event-modal.js";
import { hydrateIcons, icon } from "./icons.js";
import { Socket } from "./sockets.js";
import { sound } from "./sound.js";
import { drawTrack, drawZone, Stage, trackColor, zoneColor } from "./stage.js";
import { currentSource, emit, on, patchSource, selectSource, state } from "./state.js";
import {
  $, $$, canDo, episodeSeconds, esc, fmtDuration, fmtNum, fmtSpeed, fmtTime, isLinq, label, SCENARIO_ICONS, stored, store, toastError,
} from "./util.js";

const TRAIL_LEN = 30;
const INTERP_MS = 120; // рамка плавно переезжает к новому положению за это время — без рывков и мерцания

const view = {
  stage: null,
  socket: null,
  frame: null, // последний live.frame
  frameAt: 0,
  trails: new Map(), // track_id -> [[x,y], ...]
  trackStates: new Map(),
  anim: new Map(), // track_id -> { from, to, t0 } — интерполяция рамки между снимками
  highlight: null, // track_id, выбранный в панели треков
  panelAt: 0,
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
              <label><input type="checkbox" data-toggle="boxes" />Треки</label>
              <label><input type="checkbox" data-toggle="skeleton" />Скелет (поза)</label>
              <label><input type="checkbox" data-toggle="trail" />След движения</label>
            </div>
            <div>Территория: <span class="zone-status safe" id="zone-status">—</span></div>
          </div>
        </div>

        <div class="tracks-panel" id="tracks-panel" aria-label="Треки в кадре"></div>
        <div class="cam-foot">
          <div class="legend-inline">
            <span><i class="sw" style="background:linear-gradient(90deg,#38bdf8,#34d399,#a78bfa)"></i>Человек (свой цвет у каждого трека)</span>
            <span><i class="sw" style="background:#f59e0b"></i>Кандидат: в зоне или быстро — идёт подтверждение</span>
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
          <button class="btn btn-sm" data-filter="camera" title="Только выбранная камера">Эта камера</button>
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
  on("server", () => renderOverlay());
  on("loaded", () => renderOverlay());
  on("source-selected", () => { connectLive(); if (view.feedFilter === "camera") renderFeed(); });
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
    const now = performance.now();
    const seen = new Set();
    for (const t of data.tracks) {
      seen.add(t.track_id);
      const prev = view.anim.get(t.track_id);
      view.anim.set(t.track_id, { from: prev ? lerpBox(prev, now) : t.bbox, to: t.bbox, t0: now });
    }
    for (const id of view.anim.keys()) if (!seen.has(id)) view.anim.delete(id);
    view.frame = data;
    view.frameAt = now;
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

function lerpBox({ from, to, t0 }, now) {
  const k = Math.min(1, (now - t0) / INTERP_MS);
  return to.map((v, i) => from[i] + (v - from[i]) * k);
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
  if (toggles.boxes) {
    for (const t of tracks) {
      const anim = view.anim.get(t.track_id);
      drawTrack(stage, anim ? { ...t, bbox: lerpBox(anim, now) } : t, {
        boxes: true,
        skeleton: toggles.skeleton,
        trail: toggles.trail ? view.trails.get(t.track_id) : null,
        highlight: view.highlight === t.track_id,
        dim: view.highlight != null && view.highlight !== t.track_id,
      });
    }
  }
  if (now - view.panelAt > 300) { view.panelAt = now; renderTracksPanel(fresh ? tracks : []); }

  const visible = tracks.filter((t) => t.track_state !== "lost");
  const alerting = visible.filter((t) => t.state === "alert");
  const inzone = visible.filter((t) => t.state === "in_zone");
  const stageEl = $("#live-stage");
  stageEl.classList.toggle("alarm", alerting.length > 0);
  stageEl.classList.toggle("inzone", !alerting.length && inzone.length > 0);
  const status = $("#zone-status");
  if (alerting.length) {
    setIf(status, "className", "zone-status alarm");
    setIf(status, "textContent", `⚠ ТРЕВОГА: ${alerting.length} чел. — ${whereText(alerting)}`);
  } else if (inzone.length) {
    setIf(status, "className", "zone-status inzone");
    setIf(status, "textContent", `${whereText(inzone)} — подтверждение…`);
  } else {
    setIf(status, "className", "zone-status safe");
    setIf(status, "textContent", fresh ? `Анализ идёт · нарушений нет · людей в кадре: ${visible.length}` : "—");
  }

  const ts = $("#ov-ts");
  const tsHtml = fresh
    ? `<span class="badge badge-red">● LIVE</span><span>${fmtNum(frame.video_ts, 1)} с</span>`
    : `<span class="badge badge-gray">ПРЕВЬЮ</span>`;
  if (ts.dataset.html !== tsHtml) { ts.dataset.html = tsHtml; ts.innerHTML = tsHtml; }
}

// Панель треков в кадре: клик подсвечивает трек на кадре (повторный — снимает подсветку).
function renderTracksPanel(tracks) {
  const panel = $("#tracks-panel");
  if (!panel) return;
  const items = tracks.filter((t) => t.track_state !== "lost" || view.highlight === t.track_id);
  const html = items.length
    ? items.map((t) => {
        const flags = t.flags || [];
        const status = flags.includes("violation") ? "нарушение" : flags.includes("candidate") ? "кандидат"
          : t.track_state === "lost" ? "потерян" : "норма";
        const cls = flags.includes("violation") ? "bad" : flags.includes("candidate") ? "warn" : "";
        return `<button class="track-chip ${cls} ${view.highlight === t.track_id ? "active" : ""}" data-track="${t.track_id}">
          <i class="sw" style="background:${trackColor(t)}"></i>#${t.track_id}
          <span>${t.speed != null ? fmtSpeed(t.speed) : ""}</span><b>${status}</b></button>`;
      }).join("")
    : `<span class="faint">Людей в кадре нет</span>`;
  if (panel.dataset.html === html) return;
  panel.dataset.html = html;
  panel.innerHTML = html;
  for (const b of $$("[data-track]", panel)) {
    b.onclick = () => {
      const id = Number(b.dataset.track);
      view.highlight = view.highlight === id ? null : id;
      view.panelAt = 0;
    };
  }
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

// «в зоне «Пресс»», «бег / резкое перемещение» или оба.
function whereText(tracks) {
  const parts = [];
  const zones = zoneNames(tracks);
  if (zones) parts.push(`в зоне ${zones}`);
  const fast = tracks.some((t) => (t.scenario_types || []).includes("run_fast_movement")
    || ((t.flags || []).includes("candidate") && !(t.flags || []).includes("in_zone")));
  if (fast) parts.push("бег / резкое перемещение");
  return parts.join(" · ") || "нарушение";
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
  const kind = (fresh && view.frame.detector_kind) || det?.detector_kind || state.health?.detector_kind;
  const fps = fresh ? view.frame.fps : det?.fps;
  const infoHtml = `
    <div style="color:${isLinq(kind) ? "#34d399" : "#fbbf24"}">${icon("cpu")} ${esc(label("detector", kind))}</div>
    <div class="muted">Модель: <span style="color:var(--text)">${esc(det?.model_version || "—")}</span></div>
    <div class="muted">FPS: <span style="color:var(--text)">${fmtNum(fps, 1)}</span> · инференс: <span style="color:var(--text)">${fmtNum(det?.inference_ms_avg, 0, " мс")}</span></div>
    <div class="muted">Зон: <span style="color:var(--text)">${zones.length}</span> · людей: <span style="color:var(--text)">${fresh ? view.frame.tracks.filter((t) => t.track_state !== "lost").length : "—"}</span></div>`;
  if ($("#ov-info").dataset.html !== infoHtml) { $("#ov-info").dataset.html = infoHtml; $("#ov-info").innerHTML = infoHtml; }

  let html = "", cls = "";
  if (state.serverOnline === false) {
    cls = "err";
    html = `${icon("server-off")}<b>Нет связи с сервером</b>
      <span>Состояние камеры неизвестно. Нарушения могут не отображаться.</span><span class="muted">Переподключаемся автоматически…</span>`;
  } else if (!state.loaded) {
    html = `${icon("refresh")}<b>Загрузка…</b><span class="muted">Получаем список камер и зон</span>`;
  } else if (!state.sources.length) {
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
    html = `${icon(src.status === "stopped" ? "stop" : "play")}<b>${src.status === "stopped" ? "Обработка остановлена" : "Анализ не запущен"}</b>
      <span class="muted">${src.status === "stopped" ? "Видео обработано или анализ остановлен. Нарушения сейчас НЕ контролируются." : "Запустите анализ, чтобы видеть людей и тревоги."}</span>
      <button class="btn btn-success" data-go="start">${icon("play")}Запустить анализ</button>`;
  }
  // Предупреждаем, только если правил нет совсем: правило без зоны (например «бег») смотрит весь кадр.
  const frameRules = (state.rules[state.sourceId] || []).some((r) => r.enabled && !r.zone_id);
  if (src && !zones.length && !frameRules && !html) {
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
  if (view.feedFilter === "camera") items = items.filter((e) => e.source_id === state.sourceId);

  if (!items.length) {
    // «Тревог нет» показываем, только если сервер на связи — иначе это было бы неправдой.
    feed.innerHTML = state.serverOnline === false
      ? `<div class="empty err">${icon("server-off")}<div>Нет связи с сервером</div><div class="faint">Лента не обновляется</div></div>`
      : `<div class="empty">${icon("check-circle")}<div>Тревог нет</div>
      <div class="faint">Новые нарушения появятся здесь мгновенно</div></div>`;
    return;
  }
  feed.innerHTML = items.map((ev) => {
    const hot = ev.review_status === "unreviewed";
    const dur = ev.state === "active"
      ? `<span class="inc-dur" data-start="${esc(ev.started_at)}">идёт ${fmtDuration((Date.now() - new Date(ev.started_at)) / 1000)}</span>`
      : `<span>${fmtDuration(episodeSeconds(ev))}</span>`;
    const crit = ev.priority === "critical";
    return `<div class="inc ${hot ? "hot" : "done"} ${crit ? "crit" : ""}" data-ev="${ev.id}">
      ${ev.thumbnail_url ? `<img class="inc-thumb" src="${esc(mediaUrl(ev.thumbnail_url))}" alt="" loading="lazy" />` : `<div class="inc-thumb ph">${icon("film")}</div>`}
      <div class="inc-body">
        <div class="inc-top"><span class="inc-title">${icon(SCENARIO_ICONS[ev.scenario_type] || "alert-triangle")}${crit ? "⚠ " : ""}${esc(label("scenario", ev.scenario_type))}</span><span class="inc-time">${fmtTime(ev.started_at)}</span></div>
        <div class="inc-where">${esc(ev.source_name || "—")} • ${esc(ev.zone_name || "без зоны")}</div>
        <div class="inc-meta">${stateBadge(ev)}${reviewBadge(ev.review_status)}${priorityBadge(ev.priority)}
          ${dur}<span>· ${Math.round(ev.confidence * 100)}%</span>${ev.details?.speed_max != null ? `<span title="ростов человека в секунду, не м/с">· макс ${fmtSpeed(ev.details.speed_max)}</span>` : ""}</div>
      </div>
      ${hot ? `<div class="inc-actions">
        <button class="btn btn-sm btn-success" data-act="confirm">${icon("check")}Подтвердить</button>
        <button class="btn btn-sm" data-act="mark_false">${icon("x")}Ложное</button>
        <button class="btn btn-sm" data-act="escalate" title="Передать мастеру смены" ${canDo("escalate", ev.review_status) ? "" : "disabled"}>${icon("arrow-up")}Мастеру</button>
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
