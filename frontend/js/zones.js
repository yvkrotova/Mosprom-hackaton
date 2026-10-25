// «Камеры и зоны»: черновик разметки выбранной камеры (зоны: контур, название, правило, активность;
// правила на весь кадр). «Сохранить изменения» проверяет все контуры и поля и отправляет черновик
// одной операцией (PUT /sources/{id}/layout); «Отмена» возвращает последнее сохранённое.
// Названия, описания и назначения — из GET /scenarios; цвет зоны — по назначению (scenario.color).
import { api } from "./api.js";
import { mediaUrl } from "./config.js";
import { polygonProblems } from "./geometry.js";
import { hydrateIcons, icon } from "./icons.js";
import { Socket } from "./sockets.js";
import { drawTrack, drawZone, Stage } from "./stage.js";
import { currentSource, emit, liveSources, loadZonesAndRules, on, patchSource, refreshSources, selectSource, state } from "./state.js";
import { $, $$, confirmModal, esc, fmtNum, helpIcon, label, openModal, scenarioIcon, scenarioInfo, toast, toastError } from "./util.js";

const HANDLE = 7; // радиус точки, px
const FALLBACK_COLORS = ["#2b67ff", "#22c55e", "#ec4899", "#14b8a6"];

const ed = {
  stage: null,
  visible: false,
  sourceId: null,
  saved: null, // { zones, frame } — последнее сохранённое
  draft: null, // то же, редактируемое
  sel: -1, // индекс выбранной зоны в draft.zones
  mode: "points", // points | move
  drawing: null, // [[x,y], ...] — новый контур в процессе
  history: [], // предыдущие контуры выбранной зоны (для «Отменить точку»)
  drag: null,
  cursor: null,
  errors: {}, // key зоны -> { name, polygon, scenario_types } (тексты)
  saving: false,
  image: null,
  live: null, // сокет /ws/live выбранной камеры: идущий анализ показывается прямо в редакторе
  liveFrame: null,
  liveAt: 0,
};

const LIVE_FRESH_MS = 3000;
const liveOn = () => Boolean(ed.liveFrame) && performance.now() - ed.liveAt < LIVE_FRESH_MS;

const zoneScenarios = () => state.scenarios.filter((s) => s.requires_zone);
const frameScenarios = () => state.scenarios.filter((s) => !s.requires_zone);
const colorOf = (zone) => scenarioInfo(zone.scenario_types[0])?.color || FALLBACK_COLORS[0];
const clone = (v) => structuredClone(v);
let keySeq = 0;

// --- Модель -------------------------------------------------------------------------------

function snapshot(sourceId) {
  const zones = (state.zones[sourceId] || []).map((z) => ({
    key: `z${++keySeq}`,
    id: z.id,
    name: z.name,
    polygon: z.polygon.map((p) => [...p]),
    scenario_types: [...(z.scenario_types || [])],
    enabled: z.enabled ?? true,
    reference_url: z.reference_url || null,
    removed: false,
  }));
  const frame = {};
  for (const s of frameScenarios()) {
    const rule = (state.rules[sourceId] || []).find((r) => !r.zone_id && r.scenario_type === s.scenario_type);
    frame[s.scenario_type] = { enabled: rule ? rule.enabled : false, exists: Boolean(rule), auto: Boolean(rule?.auto_created) };
  }
  return { zones, frame };
}

function reset() {
  ed.sourceId = state.sourceId;
  ed.saved = snapshot(ed.sourceId);
  ed.draft = clone(ed.saved);
  ed.sel = ed.draft.zones.length ? 0 : -1;
  ed.drawing = null;
  ed.history = [];
  ed.errors = {};
}

const comparable = (m) => JSON.stringify({
  zones: m.zones.map(({ key, ...z }) => z),
  frame: Object.fromEntries(Object.entries(m.frame).map(([k, v]) => [k, v.enabled])),
});
export const isDirty = () => Boolean(ed.draft) && (Boolean(ed.drawing?.length) || comparable(ed.draft) !== comparable(ed.saved));

const selected = () => ed.draft?.zones[ed.sel] || null;

// --- Редактор: мышь ---------------------------------------------------------------------------

const pxDist = (a, b) => { const [ax, ay] = ed.stage.px(a); const [bx, by] = ed.stage.px(b); return Math.hypot(ax - bx, ay - by); };
const clamp01 = (v) => Math.min(1, Math.max(0, v));
const midpoints = (poly) => poly.map((p, i) => { const q = poly[(i + 1) % poly.length]; return [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2]; });

function pointIn([x, y], poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function pushHistory(zone) {
  ed.history.push({ key: zone.key, polygon: zone.polygon.map((p) => [...p]) });
  if (ed.history.length > 50) ed.history.shift();
}

function onDown(e) {
  if (e.button !== 0 || !ed.draft || !ed.stage.inside(e.clientX, e.clientY)) return;
  const p = ed.stage.rel(e.clientX, e.clientY); // точки — только внутри изображения
  if (ed.drawing) {
    if (ed.drawing.length >= 3 && pxDist(p, ed.drawing[0]) <= HANDLE + 5) return finishContour(); // замыкание
    ed.drawing.push(p);
    return renderAll();
  }
  const zone = selected();
  if (zone && !zone.removed) {
    if (ed.mode === "points") {
      const v = zone.polygon.findIndex((q) => pxDist(p, q) <= HANDLE + 3);
      if (v >= 0) {
        pushHistory(zone);
        ed.drag = { vertex: v };
        return ed.stage.canvas.setPointerCapture(e.pointerId);
      }
      const m = midpoints(zone.polygon).findIndex((q) => pxDist(p, q) <= HANDLE);
      if (m >= 0) {
        pushHistory(zone);
        zone.polygon.splice(m + 1, 0, p);
        ed.drag = { vertex: m + 1 };
        ed.stage.canvas.setPointerCapture(e.pointerId);
        return renderAll();
      }
    } else if (pointIn(p, zone.polygon)) {
      pushHistory(zone);
      ed.drag = { move: p, start: zone.polygon.map((q) => [...q]) };
      return ed.stage.canvas.setPointerCapture(e.pointerId);
    }
  }
  // Выбор зоны кликом по ней (сверху — последние).
  const idx = ed.draft.zones.map((_, i) => i).reverse().find((i) => !ed.draft.zones[i].removed && pointIn(p, ed.draft.zones[i].polygon));
  if (idx !== undefined) selectZone(idx);
}

function onMove(e) {
  if (!ed.draft) return;
  ed.cursor = ed.stage.inside(e.clientX, e.clientY) ? ed.stage.rel(e.clientX, e.clientY) : null;
  const zone = selected();
  if (ed.drag && zone) {
    const p = ed.stage.rel(e.clientX, e.clientY);
    if (ed.drag.vertex !== undefined) {
      zone.polygon[ed.drag.vertex] = p;
    } else {
      // Перемещение целиком без выхода за границы кадра.
      const xs = ed.drag.start.map((q) => q[0]), ys = ed.drag.start.map((q) => q[1]);
      const dx = Math.min(1 - Math.max(...xs), Math.max(-Math.min(...xs), p[0] - ed.drag.move[0]));
      const dy = Math.min(1 - Math.max(...ys), Math.max(-Math.min(...ys), p[1] - ed.drag.move[1]));
      zone.polygon = ed.drag.start.map(([x, y]) => [clamp01(x + dx), clamp01(y + dy)]);
    }
  }
  const canvas = ed.stage.canvas;
  canvas.style.cursor = ed.drawing ? "crosshair" : ed.drag ? "grabbing" : ed.mode === "move" && zone && ed.cursor && pointIn(ed.cursor, zone.polygon) ? "move" : "default";
  draw();
}

function onUp() {
  if (!ed.drag) return;
  ed.drag = null;
  validateZone(selected());
  renderAll();
}

function onContext(e) {
  e.preventDefault();
  if (ed.drawing) { ed.drawing.pop(); return renderAll(); }
  const zone = selected();
  if (!zone || ed.mode !== "points") return;
  const p = ed.stage.rel(e.clientX, e.clientY);
  const v = zone.polygon.findIndex((q) => pxDist(p, q) <= HANDLE + 3);
  if (v < 0) return;
  if (zone.polygon.length <= 3) return toast("У зоны должно остаться минимум 3 точки", "info", { timeout: 2500 });
  pushHistory(zone);
  zone.polygon.splice(v, 1);
  validateZone(zone);
  renderAll();
}

function onKey(e) {
  if (!ed.visible || e.target.closest?.("input, textarea, select")) return;
  if (ed.drawing) {
    if (e.key === "Enter") finishContour();
    else if (e.key === "Escape") { ed.drawing = null; renderAll(); }
    else if (e.key === "Backspace") { ed.drawing.pop(); renderAll(); }
  } else if ((e.key === "z" || e.key === "я") && (e.ctrlKey || e.metaKey)) {
    undoPoint();
  }
}

function startDrawing() {
  ed.drawing = [];
  ed.mode = "points";
  renderAll();
}

function finishContour() {
  if (!ed.drawing || ed.drawing.length < 3) return;
  const polygon = ed.drawing;
  const problems = polygonProblems(polygon);
  if (problems.length) return toast(`Контур: ${problems.join("; ")}`, "error", { timeout: 5000 });
  const first = zoneScenarios().find((s) => s.zone_default) || zoneScenarios()[0];
  const n = ed.draft.zones.filter((z) => !z.removed).length + 1;
  ed.draft.zones.push({
    key: `z${++keySeq}`, id: null, name: `Зона ${n}`, polygon, scenario_types: first ? [first.scenario_type] : [],
    enabled: true, reference_url: null, removed: false,
  });
  ed.drawing = null;
  ed.sel = ed.draft.zones.length - 1;
  ed.history = [];
  renderAll();
  $("#zone-name")?.select();
}

function undoPoint() {
  if (ed.drawing?.length) { ed.drawing.pop(); return renderAll(); }
  const last = ed.history.pop();
  if (!last) return;
  const zone = ed.draft.zones.find((z) => z.key === last.key);
  if (zone) { zone.polygon = last.polygon; validateZone(zone); }
  renderAll();
}

function selectZone(index) {
  ed.sel = index;
  ed.history = [];
  renderAll();
}

// --- Проверка -------------------------------------------------------------------------------

function validateZone(zone) {
  if (!zone) return;
  const err = {};
  if (!zone.name.trim()) err.name = "Введите название зоны";
  const problems = polygonProblems(zone.polygon);
  if (problems.length) err.polygon = `Контур: ${problems.join("; ")}`;
  if (!zone.scenario_types.length) err.scenario_types = "Выберите правило";
  if (Object.keys(err).length) ed.errors[zone.key] = err; else delete ed.errors[zone.key];
}

function validateAll() {
  ed.errors = {};
  for (const z of ed.draft.zones) if (!z.removed) validateZone(z);
  return !Object.keys(ed.errors).length;
}

// --- Сохранение -----------------------------------------------------------------------------

async function saveDraft() {
  if (ed.saving || !isDirty()) return;
  if (ed.drawing?.length) return toast("Завершите контур новой зоны или отмените его", "info");
  if (!validateAll()) {
    const bad = ed.draft.zones.findIndex((z) => ed.errors[z.key]);
    if (bad >= 0) ed.sel = bad;
    renderAll();
    return toast("Исправьте ошибки в отмеченных зонах", "error");
  }
  ed.saving = true;
  renderFoot();
  const live = ed.draft.zones.filter((z) => !z.removed);
  const body = {
    zones: live.map((z) => ({ id: z.id, name: z.name.trim(), polygon: z.polygon.map(([x, y]) => [round4(x), round4(y)]), scenario_types: z.scenario_types, enabled: z.enabled })),
    frame_rules: Object.entries(ed.draft.frame)
      .filter(([type, v]) => v.exists || v.enabled || ed.saved.frame[type]?.enabled !== v.enabled)
      .map(([scenario_type, v]) => ({ scenario_type, enabled: v.enabled })),
  };
  try {
    await api.saveLayout(ed.sourceId, body);
    await loadZonesAndRules(ed.sourceId);
    const keepSel = selected()?.id;
    const keepIndex = live.indexOf(selected());
    reset();
    const i = ed.draft.zones.findIndex((z) => z.id && z.id === keepSel);
    // Новая зона получила id только сейчас: порядок зон тот же, что в черновике.
    ed.sel = i >= 0 ? i : keepIndex >= 0 && keepIndex < ed.draft.zones.length ? keepIndex : ed.sel;
    const running = currentSource()?.status === "running";
    toast(running ? "Разметка сохранена — анализ применит её через пару секунд" : "Разметка сохранена", "success", { timeout: 3500 });
  } catch (err) {
    // Черновик сохраняется; ошибки — у проблемной зоны или поля.
    for (const e of err.details?.errors || []) {
      const zone = e.zone_index != null ? live[e.zone_index] : null;
      if (zone) (ed.errors[zone.key] ||= {})[e.field === "id" ? "name" : e.field] = e.message;
    }
    const bad = ed.draft.zones.findIndex((z) => ed.errors[z.key]);
    if (bad >= 0) ed.sel = bad;
    toastError(err);
  } finally {
    ed.saving = false;
    renderAll();
  }
}

const round4 = (v) => Math.round(v * 10000) / 10000;

function cancelDraft() {
  ed.draft = clone(ed.saved);
  ed.drawing = null;
  ed.history = [];
  ed.errors = {};
  if (ed.sel >= ed.draft.zones.length) ed.sel = ed.draft.zones.length - 1;
  renderAll();
}

// Подтверждение при уходе с несохранёнными изменениями. true — можно уходить.
export async function confirmLeave() {
  if (!isDirty()) return true;
  const ok = await confirmModal("Есть несохранённые изменения", "Разметка камеры не сохранена. Уйти и потерять изменения?", { okLabel: "Уйти без сохранения" });
  if (ok) cancelDraft();
  return ok;
}

// --- Отрисовка -------------------------------------------------------------------------------

function draw() {
  const { stage } = ed;
  if (!stage?.width || !ed.draft) return;
  const { ctx } = stage;
  stage.drawBackground();
  // Идущий анализ: люди и треки поверх живого кадра (под контурами зон и их точками).
  if (liveOn()) for (const t of ed.liveFrame.tracks) drawTrack(stage, t, { boxes: true, skeleton: false });
  ed.draft.zones.forEach((z, i) => {
    if (z.removed) return;
    const sel = i === ed.sel;
    drawZone(stage, z, colorOf(z), { dashed: Boolean(ed.errors[z.key]), state: sel ? "normal" : "normal" });
    if (!sel) return;
    for (const p of z.polygon) handle(ctx, stage.px(p), colorOf(z), "#fff");
    if (ed.mode === "points" && !ed.drawing) {
      for (const m of midpoints(z.polygon)) {
        const [x, y] = stage.px(m);
        ctx.fillStyle = "rgba(255,255,255,0.65)";
        ctx.fillRect(x - 4, y - 4, 8, 8);
      }
    }
  });
  if (ed.drawing) {
    const pts = ed.drawing.map((p) => stage.px(p));
    ctx.strokeStyle = "#4b7dff";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (ed.cursor) ctx.lineTo(...stage.px(ed.cursor));
    ctx.stroke();
    ctx.setLineDash([]);
    pts.forEach((p, i) => handle(ctx, p, "#4b7dff", i === 0 && pts.length >= 3 ? "#4b7dff" : "#fff"));
  }
}

function handle(ctx, [x, y], stroke, fill) {
  ctx.beginPath();
  ctx.arc(x, y, HANDLE, 0, Math.PI * 2);
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.lineWidth = 3;
  ctx.strokeStyle = stroke;
  ctx.stroke();
}

function renderShell(root) {
  root.innerHTML = `
    <div class="panel page-head">
      <div><h1>Камеры и зоны</h1><p>Настройка камер и правил контроля</p></div>
      <div class="head-actions"><button class="btn btn-primary" id="zn-add-cam">${icon("plus")}Добавить камеру</button></div>
    </div>
    <div class="zones-layout">
      <div class="zones-main">
        <div class="panel editor">
          <div class="editor-head">
            <h2 class="section-title">${icon("pen")}Редактор зон</h2><span class="spacer"></span>
            <button class="btn btn-primary" id="zn-new">${icon("plus")}Новая зона</button>
            <button class="btn btn-ghost" id="zn-frame">${icon("refresh")}Обновить кадр</button>
          </div>
          <div class="editor-stage" id="zn-stage"><span class="cam-label" id="zn-live">Кадр камеры</span></div>
          <div class="editor-tools">
            <button class="btn" data-mode="points">${icon("route")}Точки</button>
            <button class="btn" data-mode="move">${icon("move")}Переместить</button>
            <button class="btn" id="zn-undo">${icon("undo")}Отменить точку</button>
            <button class="btn" id="zn-finish">${icon("check")}Завершить контур</button>
            <span class="tip" id="zn-tip"></span>
          </div>
          <div class="editor-hint" id="zn-hint"></div>
          <div class="editor-foot" id="zn-foot"></div>
        </div>
      </div>
      <div class="side-col" id="zn-side"></div>
    </div>`;
  hydrateIcons(root);
  ed.stage = new Stage($("#zn-stage"));
  ed.stage.onResize = draw;
  const canvas = ed.stage.canvas;
  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerup", onUp);
  canvas.addEventListener("pointerleave", () => { ed.cursor = null; draw(); });
  canvas.addEventListener("contextmenu", onContext);
  document.addEventListener("keydown", onKey);
  $("#zn-new").onclick = startDrawing;
  $("#zn-frame").onclick = () => loadFrame(true);
  $("#zn-add-cam").onclick = () => emit("add-camera", { stay: true });
  for (const b of $$("[data-mode]", root)) b.onclick = () => { ed.mode = b.dataset.mode; renderAll(); };
  $("#zn-undo").onclick = undoPoint;
  $("#zn-finish").onclick = finishContour;
}

function renderAll() {
  if (!ed.draft) return;
  renderTools();
  renderSide();
  renderFoot();
  draw();
}

function renderTools() {
  for (const b of $$("[data-mode]")) b.classList.toggle("active", b.dataset.mode === ed.mode && !ed.drawing);
  $("#zn-undo").disabled = !(ed.drawing?.length || ed.history.length);
  $("#zn-finish").disabled = !(ed.drawing && ed.drawing.length >= 3);
  $("#zn-new").disabled = !ed.sourceId || Boolean(ed.drawing);
  const zone = selected();
  const info = zone ? scenarioInfo(zone.scenario_types[0]) : null;
  $("#zn-tip").innerHTML = info?.description ? `${icon("info")}<span>${esc(info.description)}</span>` : "";
  $("#zn-hint").textContent = ed.drawing
    ? "Кликайте по кадру, чтобы поставить точки. Замкните контур кликом по первой точке или кнопкой «Завершить контур»."
    : ed.mode === "move" ? "Перетащите выбранную зону, чтобы сдвинуть её целиком."
      : "Обведите участок по точкам. Перетаскивайте точки, чтобы изменить границы; квадратик на ребре — добавить точку, правая кнопка — удалить.";
}

function renderFoot() {
  const dirty = isDirty();
  $("#zn-foot").innerHTML = `
    <span class="dirty">${dirty ? `<span class="dot"></span>Есть несохранённые изменения` : ""}</span>
    <button class="btn btn-primary" id="zn-save" ${dirty && !ed.saving ? "" : "disabled"}>${ed.saving ? "Сохраняем…" : "Сохранить изменения"}</button>
    <button class="btn" id="zn-cancel" ${dirty && !ed.saving ? "" : "disabled"}>Отмена</button>`;
  $("#zn-save").onclick = saveDraft;
  $("#zn-cancel").onclick = cancelDraft;
}

function renderSide() {
  const side = $("#zn-side");
  const src = currentSource();
  if (!src) {
    side.innerHTML = `<div class="panel side-card"><h3>Камера</h3><div class="empty">${icon("video")}<b>Камер нет</b>
      <button class="btn btn-primary" data-add>${icon("plus")}Добавить камеру</button></div></div>`;
    $("[data-add]", side).onclick = () => emit("add-camera", { stay: true });
    return;
  }
  const stCls = src.status === "running" ? "on" : ["error", "disconnected"].includes(src.status) ? "bad" : "off";
  const zones = ed.draft.zones;
  const visibleCount = zones.filter((z) => !z.removed).length;
  const zone = selected();
  const err = zone ? ed.errors[zone.key] || {} : {};
  side.innerHTML = `
    <div class="panel side-card">
      <h3>Камера</h3>
      <select class="select" id="zn-cam" aria-label="Камера">${liveSources().map((s) => `<option value="${s.id}" ${s.id === src.id ? "selected" : ""}>${esc(s.name)}${s.site ? " · " + esc(s.site) : ""}</option>`).join("")}</select>
      <div class="status-line"><span class="st ${stCls}"><span class="dot ${stCls === "on" ? "on" : stCls === "bad" ? "bad" : ""}"></span>${esc(label("status", src.status))}</span>
        <span class="muted">${esc(src.site || "участок не указан")} · ${fmtNum(src.analysis_fps, 0)} кадр/с</span></div>
      ${src.status === "error" && src.last_error ? `<div class="notice notice-error">${icon("alert-triangle")}<span>${esc(src.last_error)}</span></div>` : ""}
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn" id="zn-edit">${icon("settings")}Изменить</button>
        <button class="icon-btn" id="zn-del" title="Удалить камеру" style="border-radius:10px">${icon("trash")}</button>
        ${["running", "disconnected"].includes(src.status)
          ? `<button class="btn btn-ghost" id="zn-stop">${icon("stop")}Остановить анализ</button>`
          : `<button class="btn btn-primary" id="zn-start">${icon("play")}Начать анализ</button>`}
      </div>
    </div>
    <div class="panel side-card">
      <h3>Правила на весь кадр ${helpIcon("Работают на всём кадре камеры, без выделения зоны. Изменения сохраняются кнопкой «Сохранить изменения».")}</h3>
      ${frameScenarios().length ? frameScenarios().map((s) => `
        <div class="rule-row">${icon(scenarioIcon(s.scenario_type))}<span class="grow">${esc(s.title)}</span>
          <label class="switch"><input type="checkbox" data-frame="${s.scenario_type}" ${ed.draft.frame[s.scenario_type]?.enabled ? "checked" : ""} aria-label="${esc(s.title)}" /><span class="knob"></span></label></div>`).join("")
        + `<span class="sub">Работает без выделения зоны</span>`
        : `<span class="muted">Сценариев без зоны нет</span>`}
    </div>
    <div class="panel side-card">
      <h3>Зоны камеры <span class="count">${visibleCount}</span></h3>
      ${zones.length ? zones.map((z, i) => `
        <div class="zone-row ${i === ed.sel ? "active" : ""} ${z.removed ? "removed" : ""} ${ed.errors[z.key] ? "invalid" : ""}" data-zone="${i}">
          <span class="zdot" style="background:${colorOf(z)}"></span>
          <span class="grow"><div class="zname ellipsis" title="${esc(z.name)}">${esc(z.name || "Без названия")}</div>
            <div class="zsub ellipsis">${z.removed ? "будет удалена при сохранении" : esc(z.scenario_types.map((t) => label("scenario", t)).join(", ") || "правило не выбрано")}</div></span>
          ${z.removed ? `<button class="btn btn-sm" data-restore="${i}">Вернуть</button>`
            : `<label class="switch" title="Контроль включён"><input type="checkbox" data-enable="${i}" ${z.enabled ? "checked" : ""} aria-label="Контроль зоны «${esc(z.name)}»" /><span class="knob"></span></label>`}
          ${icon("chevron-right")}</div>`).join("")
        : `<div class="empty" style="padding:16px">${icon("pen")}<b>Зон нет</b><span>Нажмите «Новая зона» и обведите участок на кадре</span></div>`}
    </div>
    ${zone && !zone.removed ? `
    <div class="panel side-card">
      <h3>Настройки зоны</h3>
      <div class="form-row"><span>Название</span><div class="field"><input class="input ${err.name ? "invalid" : ""}" id="zone-name" maxlength="200" value="${esc(zone.name)}" />${err.name ? `<span class="error-text">${esc(err.name)}</span>` : ""}</div></div>
      <div class="form-row"><span>Правило</span><div class="field"><select class="select ${err.scenario_types ? "invalid" : ""}" id="zone-rule">
        ${zone.scenario_types.length ? "" : `<option value="">Выберите правило</option>`}
        ${zoneScenarios().map((s) => `<option value="${s.scenario_type}" ${zone.scenario_types[0] === s.scenario_type ? "selected" : ""}>${esc(s.title)}</option>`).join("")}
      </select>${err.scenario_types ? `<span class="error-text">${esc(err.scenario_types)}</span>` : ""}</div></div>
      <div class="status-line"><span>Контроль включён</span><label class="switch"><input type="checkbox" id="zone-enabled" ${zone.enabled ? "checked" : ""} /><span class="knob"></span></label></div>
      ${err.polygon ? `<div class="notice notice-error">${icon("alert-triangle")}<span>${esc(err.polygon)}</span></div>` : ""}
      ${referenceBlock(zone)}
      <div class="form-actions">
        <button class="btn" id="zone-contour">${icon("pen")}Изменить контур</button>
        <button class="btn btn-danger" id="zone-delete">${icon("trash")}Удалить зону</button>
      </div>
    </div>` : ""}`;
  hydrateIcons(side);
  bindSide(src);
}

// Эталон «пустой зоны» — для сценариев с анализом сцены (например, загромождение прохода).
function referenceBlock(zone) {
  const info = scenarioInfo(zone.scenario_types[0]);
  if (!info?.requires_scene_analysis) return "";
  if (!zone.id) return `<span class="sub">Сохраните зону, затем запомните кадр, где проход пуст.</span>`;
  return `<div class="rule-row" style="flex-wrap:wrap">${zone.reference_url ? `<img src="${esc(mediaUrl(zone.reference_url))}?t=${Date.now()}" alt="Эталон" style="width:100%;border-radius:8px" />` : ""}
    <span class="grow sub">${zone.reference_url ? "Эталон пустого прохода сохранён" : "Эталона нет — соберётся сам по первым секундам анализа"}</span>
    <button class="btn btn-sm" id="zone-ref">${icon("save")}Запомнить пустой проход</button></div>`;
}

function bindSide(src) {
  $("#zn-cam").onchange = async (e) => {
    const next = e.target.value;
    if (!(await confirmLeave())) { e.target.value = src.id; return; }
    selectSource(next);
  };
  $("#zn-edit").onclick = () => editSource(src);
  $("#zn-del").onclick = () => deleteSource(src);
  const start = $("#zn-start"), stop = $("#zn-stop");
  // Анализ запускается и показывается здесь же — в редакторе (без перехода на «Мониторинг»).
  if (start) start.onclick = async () => {
    start.disabled = true;
    try {
      patchSource(await api.startSource(src.id, true));
      toast("Анализ запущен — живой кадр появится в редакторе через пару секунд", "success", { timeout: 3000 });
    } catch (err) {
      toastError(err);
      start.disabled = false;
    }
  };
  if (stop) stop.onclick = async () => { try { patchSource(await api.stopSource(src.id)); } catch (err) { toastError(err); } };
  for (const input of $$("[data-frame]")) {
    input.onchange = () => { ed.draft.frame[input.dataset.frame].enabled = input.checked; renderFoot(); };
  }
  for (const row of $$("[data-zone]")) {
    row.onclick = (e) => { if (!e.target.closest("label, button")) selectZone(Number(row.dataset.zone)); };
  }
  for (const input of $$("[data-enable]")) {
    input.onchange = () => { ed.draft.zones[Number(input.dataset.enable)].enabled = input.checked; renderAll(); };
  }
  for (const b of $$("[data-restore]")) b.onclick = () => { ed.draft.zones[Number(b.dataset.restore)].removed = false; renderAll(); };
  const zone = selected();
  if (!zone || zone.removed) return;
  $("#zone-name").oninput = (e) => {
    zone.name = e.target.value;
    if (ed.errors[zone.key]?.name && zone.name.trim()) validateZone(zone);
    renderFoot();
    const row = $(`[data-zone="${ed.sel}"] .zname`);
    if (row) row.textContent = zone.name || "Без названия";
  };
  $("#zone-name").onblur = () => { validateZone(zone); renderSide(); };
  $("#zone-rule").onchange = (e) => { zone.scenario_types = e.target.value ? [e.target.value] : []; validateZone(zone); renderAll(); };
  $("#zone-enabled").onchange = (e) => { zone.enabled = e.target.checked; renderAll(); };
  $("#zone-contour").onclick = () => { ed.mode = "points"; renderAll(); toast("Перетаскивайте точки контура на кадре", "info", { timeout: 2500 }); };
  $("#zone-delete").onclick = () => {
    if (!zone.id) ed.draft.zones.splice(ed.sel, 1);
    else zone.removed = true; // удаление — при сохранении; «Отмена» вернёт зону
    delete ed.errors[zone.key];
    ed.sel = ed.draft.zones.findIndex((z) => !z.removed);
    renderAll();
  };
  const ref = $("#zone-ref");
  if (ref) ref.onclick = async () => {
    try {
      await api.setZoneReference(zone.id);
      await loadZonesAndRules(ed.sourceId);
      const fresh = (state.zones[ed.sourceId] || []).find((z) => z.id === zone.id);
      zone.reference_url = fresh?.reference_url || null;
      const saved = ed.saved.zones.find((z) => z.id === zone.id);
      if (saved) saved.reference_url = zone.reference_url;
      renderAll();
      toast("Текущий кадр сохранён как пустой проход", "success", { timeout: 3000 });
    } catch (err) { toastError(err); }
  };
}

// --- Источник: изменить / удалить ------------------------------------------------------------

function editSource(src) {
  const { root, close } = openModal(`
    <h3 class="modal-title">${icon("settings")}Камера «${esc(src.name)}»</h3>
    <div class="stack">
      <label class="field"><span>Название</span><input class="input" name="name" value="${esc(src.name)}" maxlength="200" /></label>
      <label class="field"><span>Участок</span><input class="input" name="site" value="${esc(src.site || "")}" maxlength="200" placeholder="Например, Цех №2" /></label>
      <label class="field"><span>Адрес потока или путь к файлу</span><input class="input" name="uri" value="${esc(src.uri)}" maxlength="2000" /></label>
      <div class="notice notice-warn" data-uri-warn hidden>${icon("alert-triangle")}<span>Изображение камеры изменится — проверьте разметку зон после сохранения.</span></div>
      <label class="field"><span>Частота анализа, кадр/с</span><input class="input" type="number" name="fps" min="1" max="60" step="1" value="${src.analysis_fps}" /></label>
      <div class="field"><span>Предзаписанные детекции (replay)</span>
        <span class="hint">${src.replay_path ? `Привязана запись: <b>${esc(src.replay_path)}</b>` : "Запись не привязана — камера анализируется детектором сервера."}</span>
        <input class="input" type="file" name="replay" accept=".jsonl" style="padding-top:10px" />
        ${src.replay_path ? `<label class="status-line"><span>Отвязать запись</span><input type="checkbox" name="unbind" /></label>` : ""}
      </div>
    </div>
    <div class="modal-actions"><button class="btn" data-cancel>Отмена</button><button class="btn btn-primary" data-ok>${icon("save")}Сохранить</button></div>`);
  const uri = $("input[name=uri]", root);
  uri.oninput = () => ($("[data-uri-warn]", root).hidden = uri.value.trim() === src.uri);
  $("[data-cancel]", root).onclick = close;
  $("[data-ok]", root).onclick = async () => {
    const body = {
      name: $("input[name=name]", root).value.trim(),
      site: $("input[name=site]", root).value.trim() || null,
      analysis_fps: Number($("input[name=fps]", root).value) || src.analysis_fps,
    };
    if (uri.value.trim() !== src.uri) body.uri = uri.value.trim();
    if ($("input[name=unbind]", root)?.checked) body.replay_path = "";
    if (!body.name) return toast("Введите название камеры", "info");
    $("[data-ok]", root).disabled = true;
    try {
      patchSource(await api.updateSource(src.id, body));
      const file = $("input[name=replay]", root).files[0];
      if (file) {
        const form = new FormData();
        form.append("file", file);
        patchSource(await api.uploadReplay(src.id, form));
      }
      await refreshSources();
      close();
      toast(body.uri ? "Камера сохранена. Проверьте разметку зон на новом кадре." : "Камера сохранена", "success", { timeout: 4000 });
    } catch (err) {
      toastError(err);
      $("[data-ok]", root).disabled = false;
    }
  };
}

async function deleteSource(src) {
  const ok = await confirmModal(`Удалить камеру «${src.name}»?`, "Её зоны и правила удалятся. История нарушений в журнале сохранится.");
  if (!ok) return;
  try {
    await api.deleteSource(src.id);
    await refreshSources();
    toast("Камера удалена", "success", { timeout: 2500 });
  } catch (err) {
    toastError(err);
  }
}

// --- Кадр камеры -----------------------------------------------------------------------------

// --- Живой кадр идущего анализа ---------------------------------------------------------------

function liveConnect() {
  ed.live?.close();
  ed.live = null;
  ed.liveFrame = null;
  renderLiveLabel();
  const id = state.sourceId;
  if (!id || !ed.visible) return;
  ed.live = new Socket(`/ws/live/${id}`, {
    onMessage: (msg) => {
      if (msg.data?.source_id !== state.sourceId) return;
      if (msg.type === "live.ended") {
        ed.liveFrame = null;
        loadFrame(); // анализ завершён — снова снимок камеры
        renderLiveLabel();
        return;
      }
      if (msg.type !== "live.frame") return;
      const data = msg.data;
      const apply = () => {
        ed.liveFrame = data;
        ed.liveAt = performance.now();
        renderLiveLabel();
        draw();
      };
      if (!data.image) return apply();
      const img = new Image();
      img.onload = () => {
        if (state.sourceId !== id) return;
        ed.stage.setImage(img, data.width, data.height);
        apply();
      };
      img.src = data.image;
    },
  });
}

function renderLiveLabel() {
  const el = $("#zn-live");
  if (!el) return;
  const text = liveOn() ? "● Анализ идёт · живой кадр" : "Кадр камеры";
  if (el.textContent !== text) el.textContent = text;
}

function loadFrame(manual = false) {
  const id = state.sourceId;
  if (!id) { ed.stage.setImage(null); return draw(); }
  const img = new Image();
  img.onload = () => {
    if (state.sourceId !== id) return;
    ed.stage.setImage(img);
    draw();
    if (manual) toast("Кадр обновлён", "success", { timeout: 1500 });
  };
  img.onerror = () => {
    // При ошибке остаётся прежний снимок.
    if (manual) toast("Не удалось получить кадр камеры — показан прежний снимок", "error", { timeout: 4000 });
    draw();
  };
  img.src = api.previewUrl(id);
}

export function initZones(root) {
  renderShell(root);
  on("loaded", () => { reset(); loadFrame(); liveConnect(); renderAll(); });
  on("source-selected", () => { reset(); loadFrame(); liveConnect(); renderAll(); });
  on("sources", () => {
    if (ed.sourceId !== state.sourceId) { reset(); loadFrame(); liveConnect(); }
    renderAll();
  });
  // Живой кадр перестал приходить — подпись возвращается к «Кадр камеры».
  setInterval(() => { if (ed.visible && ed.liveFrame) { renderLiveLabel(); if (!liveOn()) draw(); } }, 1000);
  on("zones", (sourceId) => {
    // Данные камеры обновились извне: черновик без правок — перечитываем; с правками — не трогаем.
    if (sourceId === ed.sourceId && !isDirty()) { reset(); renderAll(); }
  });
  window.addEventListener("beforeunload", (e) => {
    if (isDirty()) { e.preventDefault(); e.returnValue = ""; }
  });
}

export function setZonesVisible(visible) {
  ed.visible = visible;
  if (!visible) {
    ed.live?.close(); // анализ продолжается в воркере; закрываем только просмотр
    ed.live = null;
    return;
  }
  if (!ed.draft || ed.sourceId !== state.sourceId) { reset(); loadFrame(); }
  liveConnect();
  ed.stage?.resize();
  renderAll();
}
