"""Геометрия в относительных координатах кадра (0..1)."""

from collections.abc import Sequence

BBox = tuple[float, float, float, float]
Point = tuple[float, float]


def foot_point(bbox: BBox) -> Point:
    """Точка «ноги»: центр нижней кромки рамки."""
    x1, _, x2, y2 = bbox
    return ((x1 + x2) / 2, y2)


def point_in_polygon(point: Point, polygon: Sequence[Sequence[float]]) -> bool:
    """Ray casting: считаем пересечения горизонтального луча из точки с рёбрами полигона."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def iou(a: BBox, b: BBox) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


MIN_POLYGON_AREA = 1e-4  # доля кадра: меньше — контур вырожден (линия или точка)


def polygon_area(polygon: Sequence[Sequence[float]]) -> float:
    """Площадь многоугольника (формула шнурования), в долях площади кадра."""
    s = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def _orient(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_cross(p1, p2, q1, q2) -> bool:
    """Отрезки пересекаются (включая касание и наложение)."""
    eps = 1e-12
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    if ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and (
        (d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)
    ):
        return True

    def on_segment(a, b, c) -> bool:  # c лежит на отрезке ab (коллинеарно)
        return (
            min(a[0], b[0]) - eps <= c[0] <= max(a[0], b[0]) + eps
            and min(a[1], b[1]) - eps <= c[1] <= max(a[1], b[1]) + eps
        )

    return (
        (abs(d1) <= eps and on_segment(q1, q2, p1))
        or (abs(d2) <= eps and on_segment(q1, q2, p2))
        or (abs(d3) <= eps and on_segment(p1, p2, q1))
        or (abs(d4) <= eps and on_segment(p1, p2, q2))
    )


def polygon_problems(polygon: Sequence[Sequence[float]]) -> list[str]:
    """Почему контур зоны нельзя сохранить (пустой список — можно). Та же логика — во фронте
    (frontend/js/geometry.js) для мгновенной подсказки."""
    if len(polygon) < 3:
        return ["нужно минимум 3 точки"]
    problems = []
    if any(not (0 <= x <= 1 and 0 <= y <= 1) for x, y in polygon):
        problems.append("точки должны быть внутри кадра (координаты 0..1)")
    distinct = {(round(x, 6), round(y, 6)) for x, y in polygon}
    if len(distinct) < 3:
        problems.append("нужно минимум 3 разные точки")
    if polygon_area(polygon) < MIN_POLYGON_AREA:
        problems.append("площадь контура почти нулевая — точки лежат на одной линии")
    n = len(polygon)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # соседние рёбра имеют общую вершину
            if _segments_cross(polygon[i], polygon[(i + 1) % n], polygon[j], polygon[(j + 1) % n]):
                problems.append("контур самопересекается — рёбра не должны перекрещиваться")
                return problems
    return problems
