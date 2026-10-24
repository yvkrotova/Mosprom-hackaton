// Общее состояние приложения и шина событий между вкладками.
import { api } from "./api.js";
import { stored, store } from "./util.js";

export const state = {
  sources: [],
  sourceId: stored("sourceId", null),
  zones: {}, // source_id -> Zone[]
  rules: {}, // source_id -> Rule[]
  scenarios: [],
  feed: new Map(), // event_id -> Event (лента на главной)
  health: null,
  metrics: null,
  wsConnected: false,
  serverOnline: null, // null — ещё не знаем (загрузка), true/false — ответил ли backend
  loaded: false, // справочники (камеры, зоны, правила) загружены
  operator: stored("operator", "Оператор смены"),
};

const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));
// Возвращает функцию отписки (нужна окнам, которые живут недолго).
export const on = (name, fn) => {
  const handler = (e) => fn(e.detail);
  bus.addEventListener(name, handler);
  return () => bus.removeEventListener(name, handler);
};

export const currentSource = () => state.sources.find((s) => s.id === state.sourceId) || null;
export const sourceById = (id) => state.sources.find((s) => s.id === id) || null;

export async function refreshSources() {
  state.sources = await api.sources();
  if (!state.sources.some((s) => s.id === state.sourceId)) {
    state.sourceId = state.sources[0]?.id ?? null;
    store("sourceId", state.sourceId);
  }
  emit("sources");
  return state.sources;
}

export function selectSource(id) {
  if (id === state.sourceId) return;
  state.sourceId = id;
  store("sourceId", id);
  emit("source-selected", id);
}

export function patchSource(source) {
  const i = state.sources.findIndex((s) => s.id === source.id);
  if (i >= 0) state.sources[i] = { ...state.sources[i], ...source };
  emit("sources");
}

export async function loadZonesAndRules(sourceId) {
  if (!sourceId) return;
  const [zones, rules] = await Promise.all([api.zones(sourceId), api.rules(sourceId)]);
  state.zones[sourceId] = zones;
  state.rules[sourceId] = rules;
  emit("zones", sourceId);
}

export function setServerOnline(online) {
  if (state.serverOnline === online) return;
  state.serverOnline = online;
  emit("server", online);
}

export function setOperator(name) {
  state.operator = name || "Оператор смены";
  store("operator", state.operator);
  emit("operator");
}
