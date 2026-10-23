// Звуковые оповещения на Web Audio API (без файлов — работает офлайн).
//   chime()  — короткий сигнал: человек вошёл в зону (нарушение ещё не подтверждено);
//   siren()  — тревога: эпизод подтверждён (event.created) — одна на эпизод;
//   speak()  — голосовое сообщение (если в системе есть русский голос).
// Браузер разрешает звук только после действия пользователя — отсюда unlock().
import { stored, store } from "./util.js";

const DEFAULTS = { enabled: true, volume: 0.7, entryChime: true, voice: true, repeat: false };

class SoundEngine extends EventTarget {
  constructor() {
    super();
    this.settings = { ...DEFAULTS, ...stored("sound", {}) };
    this.ctx = null;
    this.lastChime = 0;
    this.lastSiren = 0;
  }

  get unlocked() {
    return this.ctx?.state === "running";
  }

  async unlock() {
    if (!this.ctx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      this.ctx = new Ctx();
    }
    if (this.ctx.state !== "running") await this.ctx.resume().catch(() => {});
    this.dispatchEvent(new Event("change"));
    return this.unlocked;
  }

  set(patch) {
    Object.assign(this.settings, patch);
    store("sound", this.settings);
    this.dispatchEvent(new Event("change"));
  }

  _tone(freq, start, duration, { type = "square", gain = 0.25, endFreq } = {}) {
    const ctx = this.ctx;
    const osc = ctx.createOscillator();
    const amp = ctx.createGain();
    const filter = ctx.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.value = 3200; // мягче, чем «голый» меандр
    osc.type = type;
    osc.frequency.setValueAtTime(freq, start);
    if (endFreq) osc.frequency.exponentialRampToValueAtTime(endFreq, start + duration);
    const peak = gain * this.settings.volume;
    amp.gain.setValueAtTime(0.0001, start);
    amp.gain.exponentialRampToValueAtTime(peak, start + 0.015);
    amp.gain.setValueAtTime(peak, start + duration - 0.03);
    amp.gain.exponentialRampToValueAtTime(0.0001, start + duration);
    osc.connect(filter).connect(amp).connect(ctx.destination);
    osc.start(start);
    osc.stop(start + duration + 0.02);
  }

  _ready() {
    return this.settings.enabled && this.unlocked;
  }

  chime() {
    if (!this._ready() || !this.settings.entryChime) return;
    const now = performance.now();
    if (now - this.lastChime < 1200) return;
    this.lastChime = now;
    const t = this.ctx.currentTime;
    this._tone(880, t, 0.12, { type: "sine", gain: 0.35 });
    this._tone(1175, t + 0.15, 0.16, { type: "sine", gain: 0.35 });
  }

  siren(text) {
    if (!this._ready()) return;
    const now = performance.now();
    if (now - this.lastSiren < 1500) return;
    this.lastSiren = now;
    const t = this.ctx.currentTime;
    // Двухтональная сирена: 4 цикла «высокий — низкий».
    for (let i = 0; i < 4; i++) {
      this._tone(960, t + i * 0.5, 0.24, { gain: 0.22 });
      this._tone(720, t + i * 0.5 + 0.25, 0.24, { gain: 0.22 });
    }
    if (text) setTimeout(() => this.speak(text), 2100);
  }

  speak(text) {
    if (!this._ready() || !this.settings.voice || !("speechSynthesis" in window)) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "ru-RU";
    u.volume = this.settings.volume;
    u.rate = 1.05;
    const ru = speechSynthesis.getVoices().find((v) => v.lang?.toLowerCase().startsWith("ru"));
    if (ru) u.voice = ru;
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  }

  test() {
    this.unlock().then(() => this.siren("Проверка звука. Человек в опасной зоне."));
  }
}

export const sound = new SoundEngine();

// Любой клик по странице разблокирует звук (требование браузеров).
for (const ev of ["pointerdown", "keydown"]) {
  document.addEventListener(ev, () => sound.unlock(), { once: false, passive: true });
}
