// WebSocket с автоматическим переподключением (1 → 2 → 4 … → 10 с).
import { wsUrl } from "./config.js";

export class Socket {
  constructor(path, { onMessage, onStatus } = {}) {
    this.path = path;
    this.onMessage = onMessage;
    this.onStatus = onStatus;
    this.delay = 1000;
    this.closed = false;
    this.connected = false;
    this._open();
  }

  _open() {
    if (this.closed) return;
    const ws = (this.ws = new WebSocket(wsUrl(this.path)));
    ws.onopen = () => {
      this.delay = 1000;
      this.connected = true;
      this.onStatus?.(true);
    };
    ws.onmessage = (e) => {
      try {
        this.onMessage?.(JSON.parse(e.data));
      } catch (err) {
        console.error("WS: не удалось обработать сообщение", err);
      }
    };
    ws.onclose = () => {
      if (this.connected) this.onStatus?.(false);
      this.connected = false;
      if (!this.closed) {
        this.timer = setTimeout(() => this._open(), this.delay);
        this.delay = Math.min(this.delay * 2, 10000);
      }
    };
    ws.onerror = () => ws.close();
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    this.ws?.close();
  }
}
