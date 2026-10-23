// Простые SVG-графики без библиотек: столбцы, горизонтальные полосы, кольцо.
import { esc } from "./util.js";

const EMPTY = '<div class="chart-empty">Нет данных за период</div>';

export function barChart(el, { labels, values, color = "var(--blue)", unit = "" }) {
  if (!values.length || values.every((v) => !v)) return void (el.innerHTML = EMPTY);
  const W = 640, H = 240, pad = { l: 34, r: 8, t: 12, b: 28 };
  const max = Math.max(...values, 1);
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const bw = iw / values.length;
  const y = (v) => pad.t + ih - (v / top) * ih;
  let grid = "";
  for (let v = 0; v <= top; v += step) {
    grid += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}" class="grid"/>
             <text x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end" class="tick">${v}</text>`;
  }
  const every = Math.ceil(labels.length / 12);
  const bars = values.map((v, i) => {
    const x = pad.l + i * bw + bw * 0.15;
    const label = i % every === 0
      ? `<text x="${x + bw * 0.35}" y="${H - 8}" text-anchor="middle" class="tick">${esc(labels[i])}</text>` : "";
    return `<g><rect x="${x}" y="${y(v)}" width="${bw * 0.7}" height="${Math.max(0, pad.t + ih - y(v))}" rx="3" fill="${color}">
      <title>${esc(labels[i])}: ${v}${unit}</title></rect>${label}</g>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart" preserveAspectRatio="none">${grid}${bars.join("")}</svg>`;
}

export function hbarChart(el, items, { color = "var(--blue)" } = {}) {
  if (!items.length) return void (el.innerHTML = EMPTY);
  const max = Math.max(...items.map((i) => i.value), 1);
  el.innerHTML = `<div class="hbars">${items.map((i) => `
      <div class="hbar" title="${esc(i.label)}: ${i.value}">
        <div class="hbar-label">${esc(i.label)}</div>
        <div class="hbar-track"><div class="hbar-fill" style="width:${(i.value / max) * 100}%;background:${i.color || color}"></div></div>
        <div class="hbar-value">${i.value}</div>
      </div>`).join("")}</div>`;
}

export function donutChart(el, items, { centerLabel = "" } = {}) {
  const total = items.reduce((s, i) => s + i.value, 0);
  if (!total) return void (el.innerHTML = EMPTY);
  const R = 70, r = 46, C = 100;
  let angle = -Math.PI / 2;
  const arcs = items.filter((i) => i.value).map((i) => {
    const sweep = (i.value / total) * Math.PI * 2;
    const a0 = angle, a1 = angle + sweep - (sweep < Math.PI * 2 ? 0.0001 : 0.001);
    angle += sweep;
    const large = sweep > Math.PI ? 1 : 0;
    const p = (rad, a) => `${C + rad * Math.cos(a)},${C + rad * Math.sin(a)}`;
    return `<path d="M${p(R, a0)} A${R},${R} 0 ${large} 1 ${p(R, a1)} L${p(r, a1)} A${r},${r} 0 ${large} 0 ${p(r, a0)}Z" fill="${i.color}">
      <title>${esc(i.label)}: ${i.value}</title></path>`;
  });
  el.innerHTML = `<div class="donut">
    <svg viewBox="0 0 200 200" class="donut-svg">${arcs.join("")}
      <text x="100" y="98" text-anchor="middle" class="donut-total">${total}</text>
      <text x="100" y="118" text-anchor="middle" class="donut-caption">${esc(centerLabel)}</text></svg>
    <ul class="legend">${items.map((i) => `<li><span class="dot" style="background:${i.color}"></span>${esc(i.label)}
      <b>${i.value}</b><span class="muted">${Math.round((i.value / total) * 100)}%</span></li>`).join("")}</ul></div>`;
}

function niceStep(max) {
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  for (const m of [1, 2, 5, 10]) if (raw <= m * mag) return Math.max(1, m * mag);
  return Math.max(1, 10 * mag);
}
