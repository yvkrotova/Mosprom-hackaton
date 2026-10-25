// Столбчатая диаграмма с накоплением (SVG, без библиотек): инциденты по дням с разбивкой по
// сценариям. Тонкие столбцы, скруглённый верх 4px, зазор 2px цвета фона между сегментами,
// приглушённая сетка; подсказка при наведении, выбор дня кликом. Цвета серий — категориальная
// палитра в фиксированном порядке (проверена на тёмном фоне: ΔE CVD ≥ 9, контраст ≥ 3:1).
import { esc } from "./util.js";

// Тёмные ступени категориальной палитры (порядок фиксирован, цвета не циклятся).
export const SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];

const H = 280; // высота области графика, px
const PAD = { top: 12, right: 12, bottom: 30, left: 40 };

function niceMax(v) {
  if (v <= 4) return 4;
  const step = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * step >= v) return m * step;
  return 10 * step;
}

// days: [{ date, total, by_scenario }]; series: [{ key, title, color }]
// opts.onHover(day, x, y) / opts.onLeave() / opts.onSelect(day); opts.selected — date.
export function stackedBars(container, days, series, opts = {}) {
  const width = Math.max(320, container.clientWidth);
  const plotW = width - PAD.left - PAD.right;
  const max = niceMax(Math.max(1, ...days.map((d) => d.total)));
  const y = (v) => PAD.top + H - (v / max) * H;
  const slot = plotW / Math.max(1, days.length);
  const barW = Math.max(4, Math.min(28, slot * 0.62));
  const ticks = [0, max / 4, max / 2, (3 * max) / 4, max];
  const labelEvery = Math.ceil(days.length / Math.max(1, Math.floor(plotW / 56)));

  let marks = "";
  days.forEach((d, i) => {
    const cx = PAD.left + slot * i + slot / 2;
    const x = cx - barW / 2;
    let acc = 0;
    const segs = series.filter((s) => d.by_scenario[s.key]);
    segs.forEach((s, j) => {
      const v = d.by_scenario[s.key];
      const y0 = y(acc), y1 = y(acc + v);
      acc += v;
      const top = j === segs.length - 1;
      const h = Math.max(1, y0 - y1 - (top ? 0 : 2)); // зазор 2px между сегментами
      marks += top
        ? `<path d="M${x},${y0} V${y1 + 4} Q${x},${y1} ${x + 4},${y1} H${x + barW - 4} Q${x + barW},${y1} ${x + barW},${y1 + 4} V${y0} Z" fill="${s.color}"/>`
        : `<rect x="${x}" y="${y0 - h}" width="${barW}" height="${h}" fill="${s.color}"/>`;
    });
    const sel = opts.selected === d.date;
    // Цель наведения — весь столбец дня по высоте (больше самой метки).
    marks += `<rect class="hit${sel ? " sel" : ""}" data-i="${i}" x="${PAD.left + slot * i}" y="${PAD.top}" width="${slot}" height="${H}" />`;
    if (i % labelEvery === 0 || i === days.length - 1) {
      const [, m, dd] = d.date.split("-");
      marks += `<text class="x-label" x="${cx}" y="${PAD.top + H + 20}" text-anchor="middle">${dd}.${m}</text>`;
    }
  });
  const grid = ticks.map((t) => `<line class="grid" x1="${PAD.left}" x2="${width - PAD.right}" y1="${y(t)}" y2="${y(t)}"/>
    <text class="y-label" x="${PAD.left - 8}" y="${y(t) + 4}" text-anchor="end">${Number.isInteger(t) ? t : t.toFixed(1)}</text>`).join("");

  container.innerHTML = `<svg class="bars-svg" width="${width}" height="${H + PAD.top + PAD.bottom}" role="img"
    aria-label="Инциденты по дням с разбивкой по сценариям">${grid}${marks}</svg>`;
  const svg = container.firstElementChild;
  for (const hit of svg.querySelectorAll(".hit")) {
    const d = days[Number(hit.dataset.i)];
    hit.addEventListener("mouseenter", () => {
      hit.classList.add("hover");
      const r = hit.getBoundingClientRect();
      opts.onHover?.(d, r.left + r.width / 2, r.top + (y(d.total) - PAD.top));
    });
    hit.addEventListener("mouseleave", () => { hit.classList.remove("hover"); opts.onLeave?.(); });
    hit.addEventListener("click", () => opts.onSelect?.(d));
  }
}

// Подсказка рядом с курсором (одна на страницу).
let tip = null;
export function showTip(html, x, y) {
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tip";
    document.body.append(tip);
  }
  tip.innerHTML = html;
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = `${Math.min(window.innerWidth - w - 12, Math.max(12, x - w / 2))}px`;
  tip.style.top = `${y - h - 14 < 8 ? y + 18 : y - h - 14}px`;
}
export function hideTip() {
  if (tip) tip.hidden = true;
}

export const legendHtml = (series) => `<div class="chart-legend">${series.map((s) =>
  `<span><i style="background:${s.color}"></i>${esc(s.title)}</span>`).join("")}</div>`;
