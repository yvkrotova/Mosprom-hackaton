// Проверка контура зоны — та же логика, что backend/app/pipeline/geometry.py (polygon_problems):
// мгновенная подсказка в редакторе; окончательное решение — за сервером (PUT layout).

export const MIN_POLYGON_AREA = 1e-4; // доля кадра

export function polygonArea(poly) {
  let s = 0;
  for (let i = 0; i < poly.length; i++) {
    const [x1, y1] = poly[i], [x2, y2] = poly[(i + 1) % poly.length];
    s += x1 * y2 - x2 * y1;
  }
  return Math.abs(s) / 2;
}

const orient = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);

function segmentsCross(p1, p2, q1, q2) {
  const eps = 1e-12;
  const d1 = orient(q1, q2, p1), d2 = orient(q1, q2, p2), d3 = orient(p1, p2, q1), d4 = orient(p1, p2, q2);
  if (((d1 > eps && d2 < -eps) || (d1 < -eps && d2 > eps)) && ((d3 > eps && d4 < -eps) || (d3 < -eps && d4 > eps))) return true;
  const on = (a, b, c) => Math.min(a[0], b[0]) - eps <= c[0] && c[0] <= Math.max(a[0], b[0]) + eps
    && Math.min(a[1], b[1]) - eps <= c[1] && c[1] <= Math.max(a[1], b[1]) + eps;
  return (Math.abs(d1) <= eps && on(q1, q2, p1)) || (Math.abs(d2) <= eps && on(q1, q2, p2))
    || (Math.abs(d3) <= eps && on(p1, p2, q1)) || (Math.abs(d4) <= eps && on(p1, p2, q2));
}

// Почему контур нельзя сохранить (пустой список — можно). Тексты — как у backend.
export function polygonProblems(poly) {
  if (poly.length < 3) return ["нужно минимум 3 точки"];
  const problems = [];
  if (poly.some(([x, y]) => !(x >= 0 && x <= 1 && y >= 0 && y <= 1))) problems.push("точки должны быть внутри кадра (координаты 0..1)");
  const distinct = new Set(poly.map(([x, y]) => `${x.toFixed(6)},${y.toFixed(6)}`));
  if (distinct.size < 3) problems.push("нужно минимум 3 разные точки");
  if (polygonArea(poly) < MIN_POLYGON_AREA) problems.push("площадь контура почти нулевая — точки лежат на одной линии");
  const n = poly.length;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (j === i + 1 || (i === 0 && j === n - 1)) continue;
      if (segmentsCross(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n])) {
        problems.push("контур самопересекается — рёбра не должны перекрещиваться");
        return problems;
      }
    }
  }
  return problems;
}
