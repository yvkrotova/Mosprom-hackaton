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
