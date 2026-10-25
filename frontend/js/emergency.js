// Вызов экстренных служб. Система сама не звонит: показывает номера из EMERGENCY_CONTACTS
// (ссылки tel: — на телефоне или софтфоне сразу набор) и фиксирует, кто и куда позвонил по событию.
import { api } from "./api.js";
import { icon } from "./icons.js";
import { emit, state } from "./state.js";
import { $, $$, esc, fmtTime, label, openModal, toast, toastError } from "./util.js";

let contacts = null;

async function loadContacts() {
  if (!contacts) contacts = await api.emergencyContacts();
  return contacts;
}

// ev — событие, к которому привязать вызов (null — вызов без события, только номера).
export async function openEmergency(ev) {
  let list;
  try {
    list = await loadContacts();
  } catch (err) {
    return toastError(err);
  }
  const what = ev ? `${label("scenario", ev.scenario_type)} · ${ev.source_name || "камера"}${ev.zone_name ? " · " + ev.zone_name : ""}` : "";
  const { root, close } = openModal(`
    <h3 class="modal-title" style="color:#ff6b6b">${icon("phone")}Вызов экстренных служб</h3>
    <div class="stack">
      ${ev ? `<div class="notice notice-error">${icon("siren")}<span><b>${esc(what)}</b><br>Начало: ${fmtTime(ev.started_at)}.
        ${ev.emergency_at ? `Службы уже вызывались: ${esc(ev.emergency_service)} в ${fmtTime(ev.emergency_at)} (${esc(ev.emergency_by)}).` : ""}</span></div>`
        : `<div class="notice notice-info">${icon("info")}<span>Вызов не привязан к событию. Чтобы записать его в историю нарушения, откройте событие и вызовите службы из него.</span></div>`}
      <div class="emergency-list">${list.map((c) => `
        <a class="emergency-btn" href="tel:${esc(c.number)}" data-number="${esc(c.number)}">
          <b>${esc(c.number)}</b><span>${esc(c.name)}</span>${icon("phone")}</a>`).join("")}</div>
      ${ev ? `<label class="field"><span>Что сообщили службе (необязательно)</span>
        <textarea class="input" rows="2" maxlength="2000" data-comment placeholder="Например: человек упал со стеллажа, участок 3"></textarea></label>` : ""}
      <span class="sub">Нажатие на номер открывает набор (на компьютере — в программе-телефоне, если она есть). Система не звонит сама${ev ? ", но запишет вызов в историю события и поднимет приоритет до критического" : ""}.</span>
    </div>
    <div class="modal-actions"><button class="btn" data-cancel>Закрыть</button></div>`);
  $("[data-cancel]", root).onclick = close;
  for (const a of $$("[data-number]", root)) {
    a.addEventListener("click", async () => {
      if (!ev) return;
      try {
        const updated = await api.callEmergency(ev.id, {
          number: a.dataset.number,
          author: state.operator,
          comment: $("[data-comment]", root)?.value.trim() || undefined,
        });
        emit("event", updated);
        toast(`Вызов ${a.dataset.number} записан в историю события`, "success", { timeout: 4000 });
        close();
      } catch (err) {
        toastError(err);
      }
    });
  }
}
