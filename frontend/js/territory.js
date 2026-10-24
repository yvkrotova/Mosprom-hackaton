// Вкладка «Территории и правила»: редактор полигонов зон поверх кадра камеры и параметры правил.
import { api } from "./api.js";
import { hydrateIcons, icon } from "./icons.js";
import { drawZone, Stage, zoneColor } from "./stage.js";
import { currentSource, loadZonesAndRules, on, patchSource, refreshSources, selectSource, state } from "./state.js";
import { $, $$, confirmModal, esc, fmtNum, label, openModal, toast, toastError } from "./util.js";

const HANDLE = 7; // радиус «ручки» вершины, px
const PRIMARY = "danger_zone"; // новой зоне этот сценарий включается сразу
// Ползунки удобнее с разумным потолком; схема backend допускает больше (до 60 с).
const UI_LIMITS = { confirm_window_s: [0, 10, 0.5], gap_tolerance_s: [0, 10, 0.5], min_confidence: [0.1, 0.95, 0.05] };
const PARAM_TITLES = {
  min_confidence: ["Мин. уверенность нейросети", "Детекции слабее порога игнорируются"],
  labels: ["Классы-нарушители", "Через запятую, имена классов модели: person, car, truck…"],
  confirm_window_s: ["Подтверждение нарушения", "Тревога, только если человек в зоне дольше этого времени — отсекает ложные кадры"],
  gap_tolerance_s: ["Допуск потери из вида", "Перекрытие короче этого не завершает эпизод и не создаёт повторную тревогу"],
};

const ed = {
  stage: null,
  zones: [], // { id|null, name, polygon: [[x,y]], dirty }
  sel: -1,
  mode: "edit", // edit | draw
  draft: [],
  drag: null, // { vertex }
  hover: null, // { kind: "vertex"|"mid", index }
  cursor: null, // [x, y] в долях кадра
  rules: {}, // scenario_type -> форма правила выбранной зоны
  visible: false,
};

export function initTerritory(root) {
  root.innerHTML = `
    <div class="terr-grid">
      <div class="card">
        <div class="card-head">
          <div>
            <h2 class="card-title"><i data-icon="pen"></i>Редактор опасных зон</h2>
            <p class="card-sub">Зона — многоугольник на кадре камеры. Нарушение: «ноги» человека (центр нижней кромки рамки) внутри зоны.</p>
          </div>
          <div class="editor-tools">
            <button class="btn btn-sm btn-primary" id="new-zone"><i data-icon="plus"></i>Новая зона</button>
            <button class="btn btn-sm" id="finish-zone" hidden><i data-icon="check"></i>Замкнуть</button>
            <button class="btn btn-sm" id="cancel-zone" hidden><i data-icon="x"></i>Отмена</button>
            <button class="btn btn-sm btn-ghost" id="reload-frame" title="Обновить кадр"><i data-icon="refresh"></i>Кадр</button>
          </div>
        </div>
        <div class="stage editor-stage" id="editor-stage">
          <div class="overlay overlay-tl" id="editor-info"></div>
          <div class="overlay overlay-center" id="editor-empty" hidden></div>
        </div>
        <div class="editor-hint" id="editor-hint"></div>
      </div>

      <div class="card">
        <div class="field" style="margin-bottom:8px"><span>Камера</span><select class="select" id="terr-source"></select></div>
        <div class="source-bar" id="source-bar"></div>
        <div class="card-head" style="margin-bottom:8px"><h3 class="card-title" style="font-size:14px"><i data-icon="layers"></i>Зоны камеры</h3></div>
        <div class="zone-list" id="zone-list"></div>
        <div id="zone-panel"></div>
      </div>
    </div>`;
  hydrateIcons(root);

  ed.stage = new Stage($("#editor-stage"));
  ed.stage.onResize = draw;
  const canvas = ed.stage.canvas;
  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerup", onUp);
  canvas.addEventListener("pointerleave", () => { ed.cursor = null; ed.hover = null; draw(); });
  canvas.addEventListener("dblclick", () => ed.mode === "draw" && finishDraft());
  canvas.addEventListener("contextmenu", onContext);
  document.addEventListener("keydown", onKey);

  $("#new-zone").onclick = startDraft;
  $("#finish-zone").onclick = finishDraft;
  $("#cancel-zone").onclick = cancelDraft;
  $("#reload-frame").onclick = loadFrame;
  $("#terr-source").onchange = (e) => {
    if (hasDirty() && !window.confirm("Есть несохранённые изменения. Перейти к другой камере без сохранения?")) {
      e.target.value = state.sourceId;
      return;
    }
    selectSource(e.target.value);
  };

  on("sources", () => { renderSourceSelect(); renderSourceBar(); });
  on("source-selected", () => { resetModel(); loadFrame(); });
  on("zones", (sourceId) => { if (sourceId === state.sourceId && !hasDirty()) resetModel(); });
}

export function setTerritoryVisible(visible) {
  ed.visible = visible;
  if (visible) {
    ed.stage.resize();
    loadFrame();
  }
}

// --- Модель ---------------------------------------------------------------------------------

const hasDirty = () => ed.zones.some((z) => z.dirty) || Object.values(ed.rules).some((r) => r.dirty);

function resetModel() {
  const zones = state.zones[state.sourceId] || [];
  const selectedId = ed.zones[ed.sel]?.id;
  ed.zones = zones.map((z) => ({ id: z.id, name: z.name, polygon: z.polygon.map((p) => [...p]), dirty: false }));
  ed.sel = Math.max(0, ed.zones.findIndex((z) => z.id === selectedId));
  if (!ed.zones.length) ed.sel = -1;
  ed.mode = "edit";
  ed.draft = [];
  loadRuleForm();
  renderAll();
}

function rulesOf(scenarioType) {
  return (state.rules[state.sourceId] || []).filter((r) => !scenarioType || r.scenario_type === scenarioType);
}

// Формы правил выбранной зоны — по одной на каждый сценарий из GET /scenarios.
function loadRuleForm() {
  const zone = ed.zones[ed.sel];
  ed.rules = {};
  if (!zone) return;
  for (const info of state.scenarios) {
    const existing = zone.id ? rulesOf(info.scenario_type).find((r) => r.zone_id === zone.id) : null;
    const isNew = !existing && info.scenario_type === PRIMARY;
    ed.rules[info.scenario_type] = {
      info,
      id: existing?.id || null,
      enabled: existing ? existing.enabled : isNew,
      priority: existing?.priority || "high",
      params: structuredClone({ ...(info.defaults || {}), ...(existing?.params || {}) }),
      version: existing?.version,
      dirty: isNew, // новой зоне основное правило создаётся при сохранении
    };
  }
}

function loadFrame() {
  const src = currentSource();
  if (!src) return draw();
  const img = new Image();
  img.onload = () => { ed.stage.setImage(img); draw(); };
  img.onerror = () => { ed.stage.setImage(null); draw(); };
  img.src = api.previewUrl(src.id);
}

// --- Рисование новой зоны -----------------------------------------------------------------

function startDraft() {
  if (!currentSource()) return toast("Сначала добавьте камеру", "info");
  ed.mode = "draw";
  ed.draft = [];
  ed.sel = -1;
  renderAll();
}

function cancelDraft() {
  ed.mode = "edit";
  ed.draft = [];
  if (ed.sel < 0 && ed.zones.length) ed.sel = 0;
  loadRuleForm();
  renderAll();
}

function finishDraft() {
  if (ed.draft.length < 3) return toast("Нужно минимум 3 точки", "info", { timeout: 2500 });
  ed.zones.push({ id: null, name: `Опасная зона ${ed.zones.length + 1}`, polygon: ed.draft, dirty: true });
  ed.sel = ed.zones.length - 1;
  ed.mode = "edit";
  ed.draft = [];
  loadRuleForm();
  renderAll();
  $("#zone-name")?.select();
}

// --- Мышь и клавиатура ----------------------------------------------------------------------

function pixelDist(a, b) {
  const [ax, ay] = ed.stage.px(a);
  const [bx, by] = ed.stage.px(b);
  return Math.hypot(ax - bx, ay - by);
}

function hitTest(p) {
  const zone = ed.zones[ed.sel];
  if (!zone || ed.mode !== "edit") return null;
  const v = zone.polygon.findIndex((q) => pixelDist(p, q) <= HANDLE + 3);
  if (v >= 0) return { kind: "vertex", index: v };
  const m = midpoints(zone.polygon).findIndex((q) => pixelDist(p, q) <= HANDLE);
  if (m >= 0) return { kind: "mid", index: m };
  return null;
}

const midpoints = (poly) => poly.map((p, i) => {
  const q = poly[(i + 1) % poly.length];
  return [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2];
});

function pointInPolygon([x, y], poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function onDown(e) {
  if (e.button !== 0 || !ed.stage.inside(e.clientX, e.clientY)) return;
  const p = ed.stage.rel(e.clientX, e.clientY);
  if (ed.mode === "draw") {
    if (ed.draft.length >= 3 && pixelDist(p, ed.draft[0]) <= HANDLE + 4) return finishDraft();
    ed.draft.push(p);
    return draw();
  }
  const hit = hitTest(p);
  if (hit?.kind === "vertex") {
    ed.drag = { vertex: hit.index };
    ed.stage.canvas.setPointerCapture(e.pointerId);
  } else if (hit?.kind === "mid") {
    const zone = ed.zones[ed.sel];
    zone.polygon.splice(hit.index + 1, 0, p);
    zone.dirty = true;
    ed.drag = { vertex: hit.index + 1 };
    ed.stage.canvas.setPointerCapture(e.pointerId);
    renderAll();
  } else {
    // Выбор зоны кликом; сверху — последние нарисованные.
    const idx = ed.zones.map((z, i) => i).reverse().find((i) => pointInPolygon(p, ed.zones[i].polygon));
    if (idx !== undefined && idx !== ed.sel) selectZone(idx);
  }
}

function onMove(e) {
  ed.cursor = ed.stage.inside(e.clientX, e.clientY) ? ed.stage.rel(e.clientX, e.clientY) : null;
  if (ed.drag) {
    const zone = ed.zones[ed.sel];
    zone.polygon[ed.drag.vertex] = ed.stage.rel(e.clientX, e.clientY);
    if (!zone.dirty) { zone.dirty = true; renderPanel(); renderList(); }
  } else if (ed.cursor) {
    ed.hover = hitTest(ed.cursor);
  }
  const canvas = ed.stage.canvas;
  canvas.style.cursor = ed.mode === "draw" ? "crosshair" : ed.drag ? "grabbing" : ed.hover ? (ed.hover.kind === "vertex" ? "grab" : "copy") : "default";
  draw();
}

function onUp() {
  if (ed.drag) {
    ed.drag = null;
    renderPanel();
  }
}

function onContext(e) {
  e.preventDefault();
  const p = ed.stage.rel(e.clientX, e.clientY);
  if (ed.mode === "draw") { ed.draft.pop(); return draw(); }
  const hit = hitTest(p);
  if (hit?.kind === "vertex") removeVertex(hit.index);
}

function removeVertex(index) {
  const zone = ed.zones[ed.sel];
  if (zone.polygon.length <= 3) return toast("У зоны должно остаться минимум 3 точки", "info", { timeout: 2500 });
  zone.polygon.splice(index, 1);
  zone.dirty = true;
  ed.hover = null;
  renderAll();
}

function onKey(e) {
  if (!ed.visible || e.target.closest("input, textarea, select")) return;
  if (ed.mode === "draw") {
    if (e.key === "Enter") finishDraft();
    else if (e.key === "Escape") cancelDraft();
    else if (e.key === "Backspace") { ed.draft.pop(); draw(); }
  } else if ((e.key === "Delete" || e.key === "Backspace") && ed.hover?.kind === "vertex") {
    removeVertex(ed.hover.index);
  }
}

function selectZone(index) {
  if (Object.values(ed.rules).some((r) => r.dirty) && ed.zones[ed.sel]?.id) toast("Параметры правила предыдущей зоны не сохранены", "info", { timeout: 3000 });
  ed.sel = index;
  loadRuleForm();
  renderAll();
}

// --- Отрисовка холста -----------------------------------------------------------------------

function draw() {
  const { stage } = ed;
  if (!stage?.width) return;
  stage.drawBackground();
  const ctx = stage.ctx;
  ed.zones.forEach((z, i) => {
    const selected = i === ed.sel;
    ctx.globalAlpha = selected || ed.sel < 0 ? 1 : 0.55;
    drawZone(stage, z, zoneColor(i), { dashed: z.dirty });
    ctx.globalAlpha = 1;
    if (selected && ed.mode === "edit") {
      midpoints(z.polygon).forEach((m, k) => {
        const [x, y] = stage.px(m);
        const hot = ed.hover?.kind === "mid" && ed.hover.index === k;
        ctx.fillStyle = hot ? "#fff" : "rgba(255,255,255,0.45)";
        ctx.fillRect(x - 4, y - 4, 8, 8);
      });
      z.polygon.forEach((p, k) => {
        const [x, y] = stage.px(p);
        const hot = (ed.hover?.kind === "vertex" && ed.hover.index === k) || ed.drag?.vertex === k;
        ctx.beginPath();
        ctx.arc(x, y, hot ? HANDLE + 2 : HANDLE, 0, Math.PI * 2);
        ctx.fillStyle = "#fff";
        ctx.fill();
        ctx.lineWidth = 3;
        ctx.strokeStyle = zoneColor(ed.sel);
        ctx.stroke();
      });
    }
  });

  if (ed.mode === "draw" && ed.draft.length) {
    const pts = ed.draft.map((p) => stage.px(p));
    ctx.setLineDash([6, 5]);
    ctx.strokeStyle = "#60a5fa";
    ctx.lineWidth = 2;
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (ed.cursor) ctx.lineTo(...stage.px(ed.cursor));
    ctx.stroke();
    ctx.setLineDash([]);
    pts.forEach(([x, y], i) => {
      ctx.beginPath();
      ctx.arc(x, y, i === 0 && ed.draft.length >= 3 ? HANDLE + 3 : 5, 0, Math.PI * 2);
      ctx.fillStyle = i === 0 ? "#60a5fa" : "#fff";
      ctx.fill();
    });
  }

  const info = $("#editor-info");
  info.innerHTML = ed.cursor
    ? `<span class="coords">x ${(ed.cursor[0] * 100).toFixed(1)}% · y ${(ed.cursor[1] * 100).toFixed(1)}%</span>`
    : `<span class="coords muted">${ed.mode === "draw" ? `точек: ${ed.draft.length}` : "кадр камеры"}</span>`;
}

// --- Панели -------------------------------------------------------------------------------

function renderAll() {
  renderSourceSelect();
  renderSourceBar();
  renderList();
  renderPanel();
  renderHint();
  draw();
}

function renderSourceSelect() {
  const sel = $("#terr-source");
  if (!sel) return;
  sel.innerHTML = state.sources.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("") || `<option>Нет камер</option>`;
  sel.value = state.sourceId || "";
  const empty = $("#editor-empty");
  empty.hidden = !!state.sources.length;
  empty.innerHTML = `${icon("upload")}<b>Нет камер</b><span class="muted">Добавьте видео — и разметьте опасную зону на его кадре</span>`;
}

// --- Камера: статус, переименование, удаление ------------------------------------------------

function renderSourceBar() {
  const bar = $("#source-bar");
  if (!bar) return;
  const src = currentSource();
  if (!src) { bar.innerHTML = ""; return; }
  const cls = { running: "badge-green", error: "badge-red", disconnected: "badge-amber" }[src.status] || "badge-gray";
  bar.innerHTML = `
    <div class="source-meta">
      <span class="badge ${cls}">${esc(label("status", src.status))}</span>
      <span class="muted">${esc(src.site || "участок не указан")} · ${fmtNum(src.analysis_fps, 0)} кадр/с · ${esc(src.kind)}</span>
    </div>
    <div style="display:flex;gap:6px">
      <button class="btn btn-sm" id="edit-source">${icon("settings")}Изменить</button>
      <button class="btn btn-sm btn-ghost" id="delete-source" title="Удалить камеру">${icon("trash")}</button>
    </div>
    ${src.last_error && src.status === "error" ? `<div class="notice notice-err" style="width:100%">${icon("alert-triangle")}<span>${esc(src.last_error)}</span></div>` : ""}`;
  $("#edit-source").onclick = () => editSource(src);
  $("#delete-source").onclick = () => deleteSource(src);
}

function editSource(src) {
  const { root, close } = openModal(`
    <h3 class="modal-title">Камера</h3>
    <div style="display:flex;flex-direction:column;gap:10px">
      <label class="field"><span>Название</span><input class="input" name="name" value="${esc(src.name)}" maxlength="200" /></label>
      <label class="field"><span>Участок / площадка</span><input class="input" name="site" value="${esc(src.site || "")}" maxlength="200" placeholder="Цех №2" /></label>
      <label class="field"><span>Частота анализа, кадр/с</span><input class="input" type="number" name="fps" value="${src.analysis_fps}" min="0.5" max="60" step="0.5" /></label>
    </div>
    <div class="modal-actions"><button class="btn" data-cancel>Отмена</button><button class="btn btn-primary" data-ok>${icon("save")}Сохранить</button></div>`);
  $("[data-cancel]", root).onclick = close;
  $("[data-ok]", root).onclick = async () => {
    const name = $("[name=name]", root).value.trim();
    if (!name) return toast("Введите название камеры", "info", { timeout: 2500 });
    const site = $("[name=site]", root).value.trim();
    try {
      patchSource(await api.updateSource(src.id, { name, site: site || null, analysis_fps: Number($("[name=fps]", root).value) }));
      close();
      toast("Камера сохранена", "success", { timeout: 2000 });
    } catch (err) {
      toastError(err);
    }
  };
}

async function deleteSource(src) {
  const ok = await confirmModal(`Удалить камеру «${src.name}»?`, "Зоны и правила камеры удалятся, загруженный видеофайл тоже. События в журнале сохранятся.");
  if (!ok) return;
  try {
    await api.deleteSource(src.id);
    await refreshSources();
    toast("Камера удалена", "success", { timeout: 2000 });
  } catch (err) {
    toastError(err); // SOURCE_RUNNING: «сначала остановите анализ»
  }
}

function renderHint() {
  const drawing = ed.mode === "draw";
  $("#new-zone").hidden = drawing;
  $("#finish-zone").hidden = !drawing;
  $("#cancel-zone").hidden = !drawing;
  ed.stage.container.classList.toggle("drawing", drawing);
  $("#editor-hint").innerHTML = drawing
    ? `<span>Клик — точка</span><span>Клик по первой точке, двойной клик или <kbd>Enter</kbd> — замкнуть</span>
       <span>Правая кнопка или <kbd>Backspace</kbd> — убрать точку</span><span><kbd>Esc</kbd> — отмена</span>`
    : `<span>Перетащите вершину</span><span>Квадратик на ребре — добавить вершину</span>
       <span>Правая кнопка или <kbd>Delete</kbd> на вершине — удалить</span><span>Клик по зоне — выбрать</span>`;
}

function renderList() {
  const list = $("#zone-list");
  if (!list) return;
  const rules = rulesOf();
  list.innerHTML = ed.zones.length
    ? ed.zones.map((z, i) => {
        const zoneRules = z.id ? rules.filter((r) => r.zone_id === z.id) : [];
        const enabled = zoneRules.filter((r) => r.enabled).length;
        const ruleBadge = !z.id ? `<span class="badge badge-amber">новая</span>`
          : enabled ? `<span class="badge badge-green">контроль вкл${enabled > 1 ? ` · ${enabled}` : ""}</span>`
          : zoneRules.length ? `<span class="badge badge-gray">выкл</span>`
          : `<span class="badge badge-gray">без правила</span>`;
        return `<div class="zone-item ${i === ed.sel ? "active" : ""}" data-zone="${i}">
          <span class="z-color" style="background:${zoneColor(i)}"></span>
          <span class="z-name">${esc(z.name)}</span>${z.dirty ? `<span class="dirty-dot" title="Не сохранено"></span>` : ""}
          ${ruleBadge}</div>`;
      }).join("")
    : `<div class="empty" style="padding:16px">${icon("pen")}<div>Зон нет — нарушения не ищутся</div>
        <div class="faint">Нажмите «Новая зона» и кликами обведите опасный участок на кадре</div></div>`;
  if (rules.some((r) => !r.zone_id)) {
    list.insertAdjacentHTML("beforeend", `<div class="notice notice-info">${icon("info")}<span>Есть правило «на все зоны камеры» — оно действует в каждой зоне.</span></div>`);
  }
  for (const item of $$("[data-zone]", list)) item.onclick = () => selectZone(Number(item.dataset.zone));
}

function renderPanel() {
  const panel = $("#zone-panel");
  if (!panel) return;
  const zone = ed.zones[ed.sel];
  const src = currentSource();
  if (!zone || ed.mode === "draw") {
    panel.innerHTML = ed.mode === "draw"
      ? `<div class="panel-section"><div class="notice notice-info">${icon("crosshair")}<span>Обведите опасный участок кликами по кадру. Важно: контролируется место, где стоят ноги человека — рисуйте зону по полу.</span></div></div>`
      : "";
    return;
  }
  const running = src && ["running", "disconnected"].includes(src.status);
  const forms = Object.values(ed.rules);
  const dirty = zone.dirty || forms.some((r) => r.dirty);

  panel.innerHTML = `
    <div class="panel-section">
      <h4>Зона</h4>
      <div class="field"><span>Название (видно в тревогах и журнале)</span><input class="input" id="zone-name" value="${esc(zone.name)}" maxlength="200" /></div>
      <div class="muted" style="font-size:12px">Вершин: <b class="mono">${zone.polygon.length}</b> · площадь кадра: <b class="mono">${(polyArea(zone.polygon) * 100).toFixed(1)}%</b></div>
    </div>
    ${forms.length ? forms.map(ruleSection).join("") : `<div class="panel-section"><div class="notice notice-warn">${icon("info")}<span>Сценарии не загружены — проверьте связь с сервером.</span></div></div>`}
    <div class="panel-section">
      ${running ? `<div class="notice notice-warn">${icon("info")}<span>Анализ этой камеры запущен. Новые границы и параметры применятся после перезапуска.</span></div>` : ""}
      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" id="save-zone" style="flex:1" ${dirty ? "" : "disabled"}>${icon("save")}Сохранить</button>
        <button class="btn" id="revert-zone" ${dirty ? "" : "disabled"} title="Отменить изменения">${icon("undo")}</button>
        <button class="btn btn-danger" id="delete-zone" title="Удалить зону">${icon("trash")}</button>
      </div>
      ${running ? `<button class="btn btn-block" id="restart-analysis">${icon("refresh")}Перезапустить анализ с новыми настройками</button>` : ""}
    </div>`;

  $("#zone-name").oninput = (e) => { zone.name = e.target.value; zone.dirty = true; markDirty(); renderList(); draw(); };
  for (const el of $$("[data-rule]", panel)) {
    const r = ed.rules[el.dataset.rule];
    const field = el.dataset.field;
    el.oninput = el.onchange = () => {
      if (field === "enabled") r.enabled = el.checked;
      else if (field === "priority") r.priority = el.value;
      else r.params[field] = readParam(el, r.info.params_schema.properties[field]);
      r.dirty = true;
      const out = $(`[data-out="${r.info.scenario_type}.${field}"]`, panel);
      if (out) out.textContent = paramText(field, r.params[field]);
      markDirty();
    };
  }
  $("#save-zone").onclick = save;
  $("#revert-zone").onclick = () => { resetModel(); toast("Изменения отменены", "info", { timeout: 2000 }); };
  $("#delete-zone").onclick = removeZone;
  const restart = $("#restart-analysis");
  if (restart) restart.onclick = restartAnalysis;
}

// --- Форма правила по JSON Schema из GET /scenarios ---------------------------------------

function ruleSection(r) {
  const type = r.info.scenario_type;
  const props = r.info.params_schema?.properties || {};
  const attrs = (field) => `data-rule="${type}" data-field="${field}"`;
  return `
    <div class="panel-section">
      <h4>Правило: ${esc(r.info.title)}</h4>
      <label class="rule-toggle"><span class="rt-text">${icon("siren")}<span><b>${r.enabled ? "Контроль включён" : "Контроль выключен"}</b><br>
        <span class="muted" style="font-size:12px">${esc(r.info.description || "тревога, звук и запись в журнал")}</span></span></span>
        <input type="checkbox" ${attrs("enabled")} ${r.enabled ? "checked" : ""} /></label>
      <div class="field"><span>Приоритет тревоги</span><select class="select" ${attrs("priority")}>
        ${["low", "medium", "high", "critical"].map((p) => `<option value="${p}" ${p === r.priority ? "selected" : ""}>${label("priority", p)}</option>`).join("")}
      </select></div>
      ${Object.entries(props).map(([key, schema]) => paramField(r, key, schema, attrs(key))).join("")}
      ${r.version ? `<div class="faint" style="font-size:11px">Версия правила: ${r.version} — сохраняется в каждом событии</div>` : ""}
    </div>`;
}

function paramField(r, key, schema, attrs) {
  const [title, desc] = PARAM_TITLES[key] || [schema.description || schema.title || key, ""];
  const value = r.params[key];
  const head = `<div class="param-head"><span>${esc(title)}</span><b data-out="${r.info.scenario_type}.${key}">${esc(paramText(key, value))}</b></div>`;
  const descHtml = desc ? `<div class="param-desc">${esc(desc)}</div>` : "";
  if (schema.type === "number" || schema.type === "integer") {
    const [min, max, step] = UI_LIMITS[key] || [schema.minimum ?? 0, schema.maximum ?? 100, schema.type === "integer" ? 1 : 0.1];
    return `<div class="param">${head}<input type="range" min="${min}" max="${max}" step="${step}" value="${value}" ${attrs} />${descHtml}</div>`;
  }
  if (schema.type === "boolean") {
    return `<label class="param switch-row"><span>${esc(title)}</span><input type="checkbox" ${value ? "checked" : ""} ${attrs} /></label>${descHtml}`;
  }
  if (schema.enum) {
    return `<div class="param">${head}<select class="select" ${attrs}>${schema.enum.map((v) => `<option ${v === value ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>${descHtml}</div>`;
  }
  const text = Array.isArray(value) ? value.join(", ") : value ?? "";
  return `<div class="param">${head}<input class="input" value="${esc(text)}" ${attrs} />${descHtml}</div>`;
}

function readParam(el, schema) {
  if (schema.type === "number") return Number(el.value);
  if (schema.type === "integer") return Math.round(Number(el.value));
  if (schema.type === "boolean") return el.checked;
  if (schema.type === "array") return el.value.split(",").map((v) => v.trim()).filter(Boolean);
  return el.value;
}

function paramText(key, value) {
  if (key === "min_confidence") return `${Math.round(value * 100)}%`;
  if (key.endsWith("_s")) return `${value} с`;
  if (Array.isArray(value)) return value.join(", ");
  return String(value ?? "");
}

function markDirty() {
  $("#save-zone").disabled = false;
  $("#revert-zone").disabled = false;
}

function polyArea(poly) {
  let s = 0;
  for (let i = 0; i < poly.length; i++) {
    const [x1, y1] = poly[i], [x2, y2] = poly[(i + 1) % poly.length];
    s += x1 * y2 - x2 * y1;
  }
  return Math.abs(s) / 2;
}

// --- Сохранение ----------------------------------------------------------------------------

async function save() {
  const zone = ed.zones[ed.sel];
  const sourceId = state.sourceId;
  const name = zone.name.trim();
  if (!name) return toast("Введите название зоны", "info");
  $("#save-zone").disabled = true;
  const polygon = zone.polygon.map(([x, y]) => [round4(x), round4(y)]);
  try {
    if (!zone.id) {
      const created = await api.createZone(sourceId, { name, polygon });
      zone.id = created.id;
    } else if (zone.dirty) {
      await api.updateZone(zone.id, { name, polygon });
    }
    zone.dirty = false;
    for (const r of Object.values(ed.rules)) {
      if (!r.dirty) continue;
      const params = { ...r.params };
      if (r.id) await api.updateRule(r.id, { enabled: r.enabled, priority: r.priority, params });
      else await api.createRule({ source_id: sourceId, zone_id: zone.id, scenario_type: r.info.scenario_type, enabled: r.enabled, priority: r.priority, params });
      r.dirty = false;
    }
    await loadZonesAndRules(sourceId);
    resetModel();
    toast(`Зона «${name}» сохранена`, "success", { timeout: 2500 });
  } catch (err) {
    toastError(err);
    $("#save-zone").disabled = false;
  }
}

const round4 = (v) => Math.round(v * 10000) / 10000;

async function removeZone() {
  const zone = ed.zones[ed.sel];
  if (!zone.id) {
    ed.zones.splice(ed.sel, 1);
    ed.sel = ed.zones.length ? 0 : -1;
    loadRuleForm();
    return renderAll();
  }
  const ok = await confirmModal(`Удалить зону «${zone.name}»?`, "Правила этой зоны тоже удалятся. События в журнале сохранятся.");
  if (!ok) return;
  try {
    await api.deleteZone(zone.id);
    ed.zones.splice(ed.sel, 1);
    await loadZonesAndRules(state.sourceId);
    resetModel();
    toast("Зона удалена", "success", { timeout: 2000 });
  } catch (err) {
    toastError(err);
  }
}

async function restartAnalysis() {
  const src = currentSource();
  if (hasDirty()) return toast("Сначала сохраните изменения", "info");
  try {
    patchSource(await api.stopSource(src.id));
    // Воркер завершает эпизоды и выходит — ждём, пока backend освободит источник.
    for (let i = 0; i < 40; i++) {
      try {
        patchSource(await api.startSource(src.id, true));
        return toast("Анализ перезапущен с новыми настройками", "success");
      } catch (err) {
        if (err.code !== "SOURCE_ALREADY_RUNNING") throw err;
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    toast("Воркер не остановился вовремя — попробуйте ещё раз", "error");
  } catch (err) {
    toastError(err);
  }
}
