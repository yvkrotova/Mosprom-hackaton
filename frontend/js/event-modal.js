// Подробности эпизода: видео эпизода (исходный файл с перемоткой на начало) или кадр с пояснением,
// все поля, комментарий и явные действия проверки. Открытие отмечает событие просмотренным, но не
// подтверждает его. Открытая карточка обновляется сама по WebSocket.
import { api } from "./api.js";
import { mediaUrl } from "./config.js";
import { openEmergency } from "./emergency.js";
import { icon } from "./icons.js";
import { emit, on, refreshUnviewed, sourceById, state } from "./state.js";
import {
  $, $$, confirmModal, detailItems, episodeSeconds, esc, fmtDateTime, fmtDuration, fmtNum, label, openModal,
  reviewPill, scenarioIcon, statePill, toast, toastError, verdictControl, violatorText,
} from "./util.js";

const ACTION_DONE = {
  confirm: "Нарушение подтверждено", mark_false: "Отмечено как ложное срабатывание", escalate: "Передано мастеру смены",
  reset: "Проверка сброшена", comment: "Комментарий добавлен",
};

const VERDICT_DONE = { confirmed: "Нарушение подтверждено", false_positive: "Отмечено как ложное срабатывание" };

// Оценка оператора в один клик (POST /events/{id}/feedback). Возвращает обновлённое событие или null.
export async function rate(eventId, verdict, comment) {
  try {
    const ev = await api.feedback(eventId, { verdict, author: state.operator, comment: comment || undefined });
    emit("event", ev);
    refreshUnviewed();
    toast(VERDICT_DONE[verdict], "success", { timeout: 1800 });
    return ev;
  } catch (err) {
    toastError(err);
    return null;
  }
}

// Действие оператора (автор — имя из меню пользователя). Возвращает обновлённое событие.
export async function act(eventId, action, comment) {
  try {
    const ev = await api.action(eventId, { action, comment: comment || undefined, author: state.operator });
    emit("event", ev);
    refreshUnviewed();
    toast(ACTION_DONE[action] || "Сохранено", "success", { timeout: 2500 });
    return ev;
  } catch (err) {
    toastError(err);
    return null;
  }
}

function media(ev) {
  const range = `${fmtNum(ev.video_ts_start, 1)}–${fmtNum(ev.video_ts_end, 1)} с видео`;
  if (ev.video_url) {
    return `<video controls preload="metadata" src="${esc(mediaUrl(ev.video_url))}" data-start="${ev.video_ts_start}"></video>
      <div class="media-note">Эпизод: ${range}. Видео открыто на начале эпизода; это исходная запись камеры, не отдельный клип.</div>`;
  }
  const frame = ev.thumbnail_url
    ? `<img src="${esc(mediaUrl(ev.thumbnail_url))}" alt="Кадр нарушения" />`
    : `<div class="empty" style="background:#050b1c;border-radius:12px">${icon("film")}<b>Кадр недоступен</b></div>`;
  return `${frame}<div class="media-note">${esc(ev.video_note || "Видео эпизода недоступно")}. Эпизод: ${range}.</div>`;
}

function body(ev, draft) {
  const src = sourceById(ev.source_id);
  const duration = ev.state === "active"
    ? `${fmtDuration(episodeSeconds(ev))} (продолжается)` : fmtDuration(episodeSeconds(ev));
  const details = detailItems(ev).map((d) => `<dt>${esc(d.title)}</dt><dd>${esc(
    [ev.state === "active" && d.now != null ? `сейчас ${d.now}` : null, d.max != null ? `максимум ${d.max}` : d.now]
      .filter(Boolean).join(" · "))}</dd>`).join("");
  const history = (ev.actions || []).map((a) => `<div class="history-item"><b>${esc(a.author)}</b> · ${fmtDateTime(a.created_at)}<br>
    ${esc(label("action", a.action))}${a.prev_status !== a.new_status ? ` (${esc(label("review", a.prev_status))} → ${esc(label("review", a.new_status))})` : ""}
    ${a.comment ? `<div class="muted">«${esc(a.comment)}»</div>` : ""}</div>`).join("");
  return `
    <h3 class="modal-title">${icon(scenarioIcon(ev.scenario_type))}${esc(label("scenario", ev.scenario_type))}</h3>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin:-4px 0 16px">${statePill(ev.state, "pill-sm")}${reviewPill(ev.review_status, "pill-sm")}</div>
    <div class="episode">
      <div class="episode-media">${media(ev)}</div>
      <div>
        <dl class="kv">
          <dt>Камера</dt><dd>${esc(ev.source_name || "удалённая камера")}</dd>
          <dt>Участок</dt><dd>${esc(ev.source_site || src?.site || "—")}</dd>
          <dt>Зона</dt><dd>${esc(ev.zone_name || (ev.zone_id ? "удалённая зона" : "Весь кадр"))}</dd>
          <dt>Нарушитель</dt><dd>${esc(violatorText(ev))}</dd>
          <dt>Начало</dt><dd>${fmtDateTime(ev.started_at)}</dd>
          <dt>Окончание</dt><dd>${ev.state === "active" ? "продолжается" : fmtDateTime(ev.ended_at)}</dd>
          <dt>Длительность</dt><dd data-duration>${duration}</dd>
          <dt>На видео</dt><dd>${fmtNum(ev.video_ts_start, 1)}–${fmtNum(ev.video_ts_end, 1)} с</dd>
          <dt>Уверенность</dt><dd>${Math.round(ev.confidence * 100)}% (максимум за эпизод)</dd>
          ${details}
          <dt>Подтверждено за</dt><dd>${fmtNum(ev.video_ts_detected - ev.video_ts_start, 1)} с видео</dd>
          <dt>Модель</dt><dd class="mono">${esc(ev.model_version)}</dd>
          <dt>Версия правила</dt><dd>${ev.rule_version ?? "—"}</dd>
        </dl>
        <div class="emergency-row">
          <button class="btn btn-emergency" data-emergency>${icon("phone")}Вызвать экстренные службы</button>
          ${ev.emergency_at ? `<span class="sub">Вызваны: ${esc(ev.emergency_service)} в ${fmtDateTime(ev.emergency_at)} · ${esc(ev.emergency_by)}</span>` : ""}
        </div>
        <div class="verdict-block"><b>Оценка оператора</b>${verdictControl(ev, { large: true })}</div>
        <div style="display:flex;justify-content:flex-end;margin-top:10px">
          <button class="btn btn-sm btn-danger" data-delete>${icon("trash")}Удалить событие</button></div>
        <label class="field" style="margin-top:14px"><span>Комментарий</span>
          <textarea class="input" rows="2" maxlength="2000" data-comment placeholder="Что увидел оператор">${esc(draft)}</textarea></label>
        <div style="display:flex;justify-content:flex-end;margin-top:8px"><button class="btn btn-sm" data-act="comment">${icon("message")}Сохранить комментарий</button></div>
        <div class="history"><b>История проверки</b>${history || `<span class="muted">Оператор ещё не реагировал</span>`}</div>
      </div>
    </div>`;
}

export async function openEvent(eventId) {
  let ev;
  try {
    ev = await api.event(eventId);
  } catch (err) {
    return toastError(err);
  }
  let unsubscribe = () => {};
  let timer = null;
  const { root, close } = openModal(`<div data-body></div>`, {
    wide: true,
    onClose: () => { unsubscribe(); clearInterval(timer); },
  });
  const host = $("[data-body]", root);
  const render = () => {
    const draft = $("[data-comment]", host)?.value || "";
    const video = $("video", host);
    const playing = video && !video.paused ? video.currentTime : null;
    host.innerHTML = body(ev, draft);
    const v = $("video", host);
    if (v) {
      v.addEventListener("loadedmetadata", () => { v.currentTime = playing ?? Number(v.dataset.start); }, { once: true });
      // Браузер не играет формат файла (например, AVI) — показываем кадр нарушения с пояснением.
      v.addEventListener("error", () => {
        const box = v.parentElement;
        box.innerHTML = `${ev.thumbnail_url ? `<img src="${esc(mediaUrl(ev.thumbnail_url))}" alt="Кадр нарушения" />` : ""}
          <div class="media-note">Видео не воспроизводится в браузере (формат файла не поддерживается) — показан кадр нарушения.
          Эпизод: ${fmtNum(ev.video_ts_start, 1)}–${fmtNum(ev.video_ts_end, 1)} с видео.</div>`;
      }, { once: true });
    }
    $("[data-emergency]", host).onclick = () => openEmergency(ev);
    $("[data-delete]", host).onclick = async () => {
      const ok = await confirmModal("Удалить событие?", "Событие, его кадры и история оценок удалятся без возможности восстановления.", { okLabel: "Удалить событие" });
      if (!ok) return;
      try {
        await api.deleteEvent(ev.id);
        emit("event-deleted", ev.id);
        refreshUnviewed();
        toast("Событие удалено", "success", { timeout: 2500 });
        close();
      } catch (err) { toastError(err); }
    };
    for (const b of $$("[data-verdict]", host)) {
      b.onclick = async () => {
        if (b.classList.contains("on")) return; // эта оценка уже стоит
        for (const x of $$("[data-verdict]", host)) x.disabled = true;
        const updated = await rate(ev.id, b.dataset.verdict, $("[data-comment]", host).value.trim());
        if (updated) {
          ev = updated;
          $("[data-comment]", host).value = "";
        }
        render();
      };
    }
    for (const b of $$("[data-act]", host)) {
      b.onclick = async () => {
        const action = b.dataset.act;
        const comment = $("[data-comment]", host).value.trim();
        if (action === "comment" && !comment) return toast("Введите комментарий", "info", { timeout: 2500 });
        b.disabled = true;
        const updated = await act(ev.id, action, comment);
        if (updated) {
          ev = updated;
          $("[data-comment]", host).value = "";
          render();
        } else {
          b.disabled = false;
        }
      };
    }
  };
  render();
  unsubscribe = on("event", (changed) => {
    if (changed.id !== ev.id) return;
    ev = { ...ev, ...changed, actions: changed.actions || ev.actions };
    render();
  });
  // Длительность идущего эпизода обновляется на глазах.
  timer = setInterval(() => {
    if (ev.state !== "active") return;
    const el = $("[data-duration]", host);
    if (el) el.textContent = `${fmtDuration(episodeSeconds(ev))} (продолжается)`;
  }, 1000);
  if (!ev.viewed_at) {
    try {
      const viewed = await api.viewEvent(ev.id);
      ev = { ...ev, viewed_at: viewed.viewed_at };
      emit("event", viewed);
      refreshUnviewed();
    } catch { /* отметка просмотра не мешает работе с карточкой */ }
  }
}
