// Клиент REST API backend (контракт: backend/docs/CONTRACT.md).
import { API_BASE } from "./config.js";

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details || {};
  }
}

async function request(method, path, { json, query, form } = {}) {
  const url = new URL(API_BASE + "/api/v1" + path, location.href);
  for (const [k, v] of Object.entries(query || {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  }
  const init = { method, headers: {} };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form) {
    init.body = form;
  }
  let resp;
  try {
    resp = await fetch(url, init);
  } catch {
    throw new ApiError(0, "NETWORK", "Нет связи с backend. Он запущен?");
  }
  if (resp.status === 204) return null;
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const err = body?.error || {};
    throw new ApiError(resp.status, err.code || "HTTP_" + resp.status, err.message || resp.statusText, err.details);
  }
  return body;
}

// Загрузка файла через XHR — ради индикатора прогресса.
function uploadWithProgress(form, onProgress, path = "/sources/upload") {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API_BASE + "/api/v1" + path);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* пусто */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new ApiError(xhr.status, body?.error?.code || "HTTP", body?.error?.message || "Ошибка загрузки", body?.error?.details));
    };
    xhr.onerror = () => reject(new ApiError(0, "NETWORK", "Нет связи с backend"));
    xhr.send(form);
  });
}

export const api = {
  health: () => request("GET", "/health"),
  scenarios: () => request("GET", "/scenarios"),

  sources: () => request("GET", "/sources"),
  createSource: (body) => request("POST", "/sources", { json: body }),
  uploadSource: (form, onProgress) => uploadWithProgress(form, onProgress),
  uploadReplay: (id, form, onProgress) => uploadWithProgress(form, onProgress, `/sources/${id}/replay`),
  updateSource: (id, body) => request("PATCH", `/sources/${id}`, { json: body }),
  deleteSource: (id) => request("DELETE", `/sources/${id}`),
  startSource: (id, realtime) => request("POST", `/sources/${id}/start`, { query: { realtime } }),
  stopSource: (id) => request("POST", `/sources/${id}/stop`),
  previewUrl: (id) => `${API_BASE}/api/v1/sources/${id}/preview?t=${Date.now()}`,
  saveLayout: (id, body) => request("PUT", `/sources/${id}/layout`, { json: body }),

  zones: (sourceId) => request("GET", `/sources/${sourceId}/zones`),
  createZone: (sourceId, body) => request("POST", `/sources/${sourceId}/zones`, { json: body }),
  updateZone: (id, body) => request("PATCH", `/zones/${id}`, { json: body }),
  deleteZone: (id) => request("DELETE", `/zones/${id}`),
  setZoneReference: (id) => request("POST", `/zones/${id}/reference`),
  clearZoneReference: (id) => request("DELETE", `/zones/${id}/reference`),

  rules: (sourceId) => request("GET", "/rules", { query: { source_id: sourceId } }),
  createRule: (body) => request("POST", "/rules", { json: body }),
  updateRule: (id, body) => request("PATCH", `/rules/${id}`, { json: body }),
  deleteRule: (id) => request("DELETE", `/rules/${id}`),

  events: (query) => request("GET", "/events", { query }),
  event: (id) => request("GET", `/events/${id}`),
  viewEvent: (id) => request("POST", `/events/${id}/view`),
  unviewed: (site) => request("GET", "/events/unviewed", { query: { site } }),
  action: (id, body) => request("POST", `/events/${id}/actions`, { json: body }),
  // Серверная выгрузка всей выборки (не страницы). Возвращает Blob.
  exportCsv: async (query) => {
    const url = new URL(API_BASE + "/api/v1/events/export.csv", location.href);
    for (const [k, v] of Object.entries(query || {})) if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    let resp;
    try { resp = await fetch(url); } catch { throw new ApiError(0, "NETWORK", "Нет связи с backend. Он запущен?"); }
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      throw new ApiError(resp.status, body?.error?.code || "HTTP", body?.error?.message || "Выгрузка не удалась");
    }
    return resp.blob();
  },

  stats: (query) => request("GET", "/stats", { query }),
  metrics: (sourceId) => request("GET", "/metrics", { query: { source_id: sourceId } }),
};
