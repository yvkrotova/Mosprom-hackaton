// Карточка события: доказательства, детали, задержки, действия оператора, история.
import { api } from "./api.js";
import { mediaUrl } from "./config.js";
import { icon } from "./icons.js";
import { emit, state } from "./state.js";
import {
  $, $$, canDo, episodeSeconds, esc, fmtDateTime, fmtDuration, fmtNum, label, openModal, toast, toastError,
} from "./util.js";

export const reviewBadge = (status) => {
  const cls = { unreviewed: "badge-amber", confirmed: "badge-red", false_positive: "badge-gray", escalated: "badge-purple" }[status];
  return `<span class="badge ${cls}">${esc(label("review", status))}</span>`;
};

export const stateBadge = (ev) =>
  ev.state === "active"
    ? `<span class="badge badge-red">● ${esc(label("state", "active"))}</span>`
    : `<span class="badge badge-gray">${esc(label("state", "ended"))}</span>`;

export const priorityBadge = (p) => {
  const cls = { low: "badge-gray", medium: "badge-blue", high: "badge-amber", critical: "badge-red" }[p];
  return `<span class="badge ${cls}">${esc(label("priority", p))}</span>`;
};

// Действие оператора из любого места интерфейса. Возвращает обновлённое событие.
export async function act(eventId, action, comment) {
  try {
    const ev = await api.action(eventId, { action, comment: comment || undefined, author: state.operator });
    emit("event", ev);
    const msgs = { confirm: "Нарушение подтверждено", mark_false: "Отмечено как ложное срабатывание", escalate: "Передано на эскалацию", reset: "Проверка сброшена", comment: "Комментарий добавлен" };
    toast(msgs[action] || "Сохранено", "success", { timeout: 2500 });
    return ev;
  } catch (err) {
    toastError(err);
    return null;
  }
}

export async function openEvent(eventId) {
  let ev;
  try {
    ev = await api.event(eventId);
  } catch (err) {
    return toastError(err);
  }
  const { root, close } = openModal(`<div data-body></div>`, { wide: true });
  const body = $("[data-body]", root);
  let showKind = "frame_annotated";

  const render = () => {
    const evidence = ev.evidence.find((e) => e.kind === showKind) || ev.evidence[0];
    const kinds = [...new Set(ev.evidence.map((e) => e.kind))];
    const realDelay = (new Date(ev.detected_at) - new Date(ev.started_at)) / 1000;
    const notifyMs = ev.notified_at ? new Date(ev.notified_at) - new Date(ev.detected_at) : null;
    body.innerHTML = `
      <div class="card-head" style="padding-right:36px">
        <div>
          <h3 class="modal-title" style="margin:0">${esc(label("scenario", ev.scenario_type))}</h3>
          <p class="card-sub">${esc(ev.source_name || "Источник удалён")} • ${esc(ev.zone_name || "без зоны")} • ${fmtDateTime(ev.started_at)}</p>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">${stateBadge(ev)}${reviewBadge(ev.review_status)}${priorityBadge(ev.priority)}</div>
      </div>
      <div class="ev-grid">
        <div>
          ${evidence
            ? `<img class="ev-img" src="${esc(mediaUrl(evidence.url))}" alt="Кадр-доказательство" />`
            : `<div class="ev-img empty" style="display:grid;place-items:center">${icon("film")}<div>Кадр ещё загружается…</div></div>`}
          <div class="ev-img-tabs">${kinds.map((k) => `<button class="btn btn-sm ${k === showKind ? "btn-primary" : ""}" data-kind="${k}">
              ${k === "frame_annotated" ? "С разметкой" : k === "frame_raw" ? "Исходный кадр" : "Клип"}</button>`).join("")}
            ${evidence ? `<a class="btn btn-sm btn-ghost" href="${esc(mediaUrl(evidence.url))}" target="_blank" rel="noopener">${icon("download")}Открыть файл</a>` : ""}
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:16px">
          <dl class="kv">
            <dt>Камера</dt><dd>${esc(ev.source_name || "—")}</dd>
            <dt>Зона</dt><dd>${esc(ev.zone_name || "—")}</dd>
            <dt>Трек</dt><dd class="mono">#${ev.track_id ?? "—"}</dd>
            <dt>Уверенность</dt><dd class="mono">${Math.round(ev.confidence * 100)}% (макс. за эпизод)</dd>
            <dt>Начало</dt><dd class="mono">${fmtDateTime(ev.started_at)}</dd>
            <dt>Конец</dt><dd class="mono">${ev.ended_at ? fmtDateTime(ev.ended_at) : "идёт"}</dd>
            <dt>Длительность</dt><dd class="mono">${fmtDuration(episodeSeconds(ev))}</dd>
            <dt>На видео</dt><dd class="mono">${fmtNum(ev.video_ts_start, 1)}–${fmtNum(ev.video_ts_end, 1)} с</dd>
            <dt>Подтверждено за</dt><dd class="mono">${fmtNum(ev.video_ts_detected - ev.video_ts_start, 2)} с видео · ${fmtNum(realDelay, 2)} с реально</dd>
            <dt>Оповещение</dt><dd class="mono">${notifyMs == null ? "—" : fmtNum(notifyMs, 0) + " мс после подтверждения"}</dd>
            <dt>Модель</dt><dd class="mono">${esc(ev.model_version)}</dd>
          </dl>
          <div>
            <div class="ev-actions">
              <button class="btn btn-success" data-act="confirm" ${canDo("confirm", ev.review_status) ? "" : "disabled"}>${icon("check")}Подтвердить</button>
              <button class="btn" data-act="mark_false" ${canDo("mark_false", ev.review_status) ? "" : "disabled"}>${icon("x")}Ложное</button>
              <button class="btn" data-act="escalate" ${canDo("escalate", ev.review_status) ? "" : "disabled"}>${icon("arrow-up")}Эскалировать</button>
              <button class="btn btn-ghost" data-act="reset" ${canDo("reset", ev.review_status) ? "" : "disabled"}>${icon("undo")}Сбросить</button>
            </div>
            <textarea class="input" rows="2" placeholder="Комментарий: что сделано, кто уведомлён…" style="margin-top:10px;resize:vertical"></textarea>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;gap:8px">
              <span class="muted" style="font-size:12px">${icon("user")} ${esc(state.operator)}</span>
              <button class="btn btn-sm" data-act="comment">${icon("message")}Комментировать</button>
            </div>
          </div>
          <div>
            <h4 class="muted" style="margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.06em">История проверки</h4>
            ${ev.actions.length ? `<ul class="timeline">${ev.actions.slice().reverse().map((a) => `
              <li><div class="t-when">${fmtDateTime(a.created_at)} · ${esc(a.author)}</div>
                <div><b>${esc(label("action", a.action))}</b>${a.prev_status !== a.new_status ? ` <span class="muted">${esc(label("review", a.prev_status))} → ${esc(label("review", a.new_status))}</span>` : ""}</div>
                ${a.comment ? `<div class="muted">«${esc(a.comment)}»</div>` : ""}</li>`).join("")}</ul>`
              : `<div class="muted" style="font-size:13px">Оператор ещё не реагировал</div>`}
          </div>
        </div>
      </div>`;

    for (const btn of $$("[data-kind]", body)) btn.onclick = () => { showKind = btn.dataset.kind; render(); };
    for (const btn of $$("[data-act]", body)) {
      btn.onclick = async () => {
        const comment = $("textarea", body).value.trim();
        if (btn.dataset.act === "comment" && !comment) return toast("Введите текст комментария", "info", { timeout: 2500 });
        btn.disabled = true;
        const updated = await act(ev.id, btn.dataset.act, comment);
        if (updated) ev = updated;
        render();
      };
    }
  };
  render();
  return { close };
}
