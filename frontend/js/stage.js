// Холст «кадр + разметка»: вписывание кадра, координаты 0..1 <-> пиксели, отрисовка зон и людей.

export const ZONE_COLORS = ["#f59e0b", "#38bdf8", "#a78bfa", "#34d399", "#f472b6", "#fb7185"];
export const zoneColor = (index) => ZONE_COLORS[index % ZONE_COLORS.length];

const STATE_COLORS = { normal: "#38bdf8", in_zone: "#f59e0b", alert: "#ef4444" };

// Постоянный цвет трека по его ID (обычное состояние). Кандидат — жёлтый, нарушение — красный.
const TRACK_COLORS = ["#38bdf8", "#34d399", "#a78bfa", "#f472b6", "#22d3ee", "#a3e635", "#fb923c", "#c084fc"];
export const trackColor = (track) =>
  track.state === "alert" ? STATE_COLORS.alert
    : track.state === "in_zone" ? STATE_COLORS.in_zone
      : TRACK_COLORS[track.track_id % TRACK_COLORS.length];

// Рёбра скелета COCO-17: голова, руки, корпус, ноги.
const SKELETON = [
  [0, 1], [0, 2], [1, 3], [2, 4], [5, 6], [5, 7], [7, 9], [6, 8], [8, 10],
  [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
];

export class Stage {
  constructor(container) {
    this.container = container;
    this.canvas = document.createElement("canvas");
    container.prepend(this.canvas);
    this.ctx = this.canvas.getContext("2d");
    this.image = null; // HTMLImageElement | ImageBitmap
    this.frameSize = { w: 1280, h: 720 };
    this.onResize = null;
    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.width = w;
    this.height = h;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.onResize?.();
  }

  setImage(img, w, h) {
    this.image = img;
    if (w && h) this.frameSize = { w, h };
    else if (img) this.frameSize = { w: img.naturalWidth || img.width, h: img.naturalHeight || img.height };
  }

  // Прямоугольник кадра внутри холста (object-fit: contain).
  get rect() {
    const { w, h } = this.frameSize;
    const scale = Math.min(this.width / w, this.height / h);
    const rw = w * scale, rh = h * scale;
    return { x: (this.width - rw) / 2, y: (this.height - rh) / 2, w: rw, h: rh };
  }

  px([x, y]) {
    const r = this.rect;
    return [r.x + x * r.w, r.y + y * r.h];
  }

  rel(clientX, clientY) {
    const b = this.canvas.getBoundingClientRect();
    const r = this.rect;
    const x = (clientX - b.left - r.x) / r.w;
    const y = (clientY - b.top - r.y) / r.h;
    return [Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))];
  }

  inside(clientX, clientY) {
    const b = this.canvas.getBoundingClientRect();
    const r = this.rect;
    const x = clientX - b.left, y = clientY - b.top;
    return x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h;
  }

  drawBackground() {
    const { ctx } = this;
    ctx.fillStyle = "#05080d";
    ctx.fillRect(0, 0, this.width, this.height);
    const r = this.rect;
    if (this.image) {
      ctx.drawImage(this.image, r.x, r.y, r.w, r.h);
    } else {
      ctx.strokeStyle = "#111a28";
      ctx.lineWidth = 1;
      for (let x = r.x; x <= r.x + r.w; x += r.w / 16) line(ctx, x, r.y, x, r.y + r.h);
      for (let y = r.y; y <= r.y + r.h; y += r.h / 9) line(ctx, r.x, y, r.x + r.w, y);
    }
  }
}

function line(ctx, x1, y1, x2, y2) {
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
}

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function chip(ctx, x, y, text, bg, { fg = "#fff", font = "600 11px ui-monospace, Consolas, monospace" } = {}) {
  ctx.font = font;
  const w = ctx.measureText(text).width + 12;
  ctx.fillStyle = bg;
  ctx.beginPath();
  ctx.roundRect ? ctx.roundRect(x, y - 18, w, 18, 4) : ctx.rect(x, y - 18, w, 18);
  ctx.fill();
  ctx.fillStyle = fg;
  ctx.fillText(text, x + 6, y - 5);
  return w;
}

// state: "normal" | "in_zone" | "alert"; pulse 0..1 — для мигания тревоги.
export function drawZone(stage, zone, color, { state = "normal", pulse = 0, label = true, dashed = false } = {}) {
  const { ctx } = stage;
  if (!zone.polygon?.length) return;
  const pts = zone.polygon.map((p) => stage.px(p));
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.closePath();
  const stroke = state === "alert" ? "#ef4444" : state === "in_zone" ? "#f59e0b" : color;
  const fillA = state === "alert" ? 0.22 + 0.18 * pulse : state === "in_zone" ? 0.22 : 0.12;
  ctx.fillStyle = hexA(stroke, fillA);
  ctx.fill();
  ctx.setLineDash(dashed ? [8, 6] : []);
  ctx.lineWidth = state === "normal" ? 2 : 3;
  ctx.strokeStyle = stroke;
  ctx.stroke();
  ctx.setLineDash([]);
  if (label) {
    const top = pts.reduce((a, b) => (b[1] < a[1] || (b[1] === a[1] && b[0] < a[0]) ? b : a));
    const prefix = state === "alert" ? "⚠ " : "";
    chip(ctx, top[0], top[1] - 4, prefix + zone.name, hexA(stroke, 0.85), { fg: state === "normal" ? "#0b0f17" : "#fff" });
  }
}

// track.bbox — уже интерполированная рамка; track_state "lost" рисуется полупрозрачно (прогноз).
export function drawTrack(stage, track, { skeleton = true, boxes = true, trail = null, highlight = false, dim = false } = {}) {
  const { ctx } = stage;
  const color = trackColor(track);
  const [x1, y1] = stage.px([track.bbox[0], track.bbox[1]]);
  const [x2, y2] = stage.px([track.bbox[2], track.bbox[3]]);
  ctx.save();
  ctx.globalAlpha = track.track_state === "lost" ? 0.4 : dim ? 0.35 : 1;
  if (highlight) {
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 4;
    ctx.strokeRect(x1 - 3, y1 - 3, x2 - x1 + 6, y2 - y1 + 6);
  }

  if (trail?.length > 1) {
    ctx.lineWidth = 2;
    for (let i = 1; i < trail.length; i++) {
      ctx.strokeStyle = hexA(color, (i / trail.length) * 0.7);
      const [ax, ay] = stage.px(trail[i - 1]);
      const [bx, by] = stage.px(trail[i]);
      line(ctx, ax, ay, bx, by);
    }
  }

  if (boxes) {
    // Уголки вместо сплошной рамки: не закрывают человека.
    const len = Math.min(18, (x2 - x1) / 3, (y2 - y1) / 3);
    ctx.strokeStyle = color;
    ctx.lineWidth = track.state === "alert" ? 3 : 2;
    ctx.beginPath();
    for (const [cx, cy, dx, dy] of [[x1, y1, 1, 1], [x2, y1, -1, 1], [x1, y2, 1, -1], [x2, y2, -1, -1]]) {
      ctx.moveTo(cx + dx * len, cy);
      ctx.lineTo(cx, cy);
      ctx.lineTo(cx, cy + dy * len);
    }
    ctx.stroke();
    ctx.strokeStyle = hexA(color, 0.35);
    ctx.lineWidth = 1;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    const flags = track.flags || [];
    const running = (track.scenario_types || []).includes("run_fast_movement");
    const tag = track.track_state === "lost" ? "потерян"
      : track.state === "alert" ? (running ? "БЕГ" : "ТРЕВОГА")
        : flags.includes("candidate") && !flags.includes("in_zone") ? "БЫСТРО?"
          : track.state === "in_zone" ? "В ЗОНЕ" : "Человек";
    const name = track.label === "person" ? tag : track.label;
    const speed = track.speed != null ? ` ${track.speed.toFixed(1)} р/с` : "";
    chip(ctx, x1, y1 - 2, `#${track.track_id} ${name}${speed}`, hexA(color, 0.9));
  }

  if (skeleton && track.keypoints?.length === 17) {
    const kp = track.keypoints.map(([x, y, c]) => [...stage.px([x, y]), c]);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    for (const [a, b] of SKELETON) {
      if (kp[a][2] > 0.3 && kp[b][2] > 0.3) line(ctx, kp[a][0], kp[a][1], kp[b][0], kp[b][1]);
    }
    for (const [x, y, c] of kp) {
      if (c <= 0.3) continue;
      ctx.fillStyle = "#fff";
      ctx.beginPath();
      ctx.arc(x, y, 2.6, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // Точка контроля: центр нижней кромки рамки — именно её сценарий проверяет на попадание в зону.
  const fx = (x1 + x2) / 2;
  ctx.fillStyle = color;
  ctx.strokeStyle = "#0b0f17";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(fx, y2, 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}
