"""Кинематика по истории трека: скорость в «ростах человека в секунду». Общая для всех сценариев.

Все функции — чистые: результат зависит только от истории трека и момента video_ts.
История (Track.history) — точки ног в пикселях исходного кадра за последние ~3 с.

Скорость = смещение ног (пиксели) / медианная высота рамки (пиксели) / Δvideo_ts. Единица не зависит
от расстояния до камеры: ходьба ≈ 0.6–1.0, бег ≈ 2–3. Это НЕ м/с; камера должна быть неподвижной.
"""

from dataclasses import dataclass
from statistics import median

from app.pipeline.interfaces import Track, TrackPoint

EDGE = 0.005  # рамка ближе к краю кадра — человек обрезан, точка ног недостоверна


@dataclass(frozen=True, slots=True)
class SpeedEstimate:
    speed: float | None  # роста/с; None — «неизвестна» (это не ноль)
    reason: str | None = None  # почему неизвестна


def window_points(track: Track, ts: float, window_s: float) -> list[TrackPoint]:
    return [p for p in track.history if ts - p.video_ts <= window_s + 1e-6]


def regression_velocity(points: list[TrackPoint]) -> tuple[float, float] | None:
    """Наклон прямой «положение ног от времени» по x и y (пиксели/с), МНК. None — точек мало."""
    if len(points) < 2:
        return None
    t0 = sum(p.video_ts for p in points) / len(points)
    x0 = sum(p.foot_x for p in points) / len(points)
    y0 = sum(p.foot_y for p in points) / len(points)
    denom = sum((p.video_ts - t0) ** 2 for p in points)
    if denom <= 0:
        return None
    vx = sum((p.video_ts - t0) * (p.foot_x - x0) for p in points) / denom
    vy = sum((p.video_ts - t0) * (p.foot_y - y0) for p in points) / denom
    return vx, vy


def touches_edge(
    bbox: tuple[float, float, float, float], margin: float = EDGE, *, sides: bool = True
) -> bool:
    """Касается ли рамка края кадра. sides=False — только верх и низ."""
    x1, y1, x2, y2 = bbox
    vertical = y1 <= margin or y2 >= 1 - margin
    return vertical or (sides and (x1 <= margin or x2 >= 1 - margin))


def estimate_speed(
    track: Track,
    ts: float,
    *,
    window_s: float,
    min_track_age_s: float,
    min_points: int,
    max_height_change: float,
) -> SpeedEstimate:
    """Скорость трека на момент ts или «неизвестна» с причиной.

    Сглаживание даёт сама регрессия по окну window_s, плюс медиана по вложенным окнам
    (полное окно и его последние 3/4): одиночный выброс рамки не делает скорость скачком.
    """
    if track.state != "confirmed":
        return SpeedEstimate(None, "трек не подтверждён")
    if ts - track.first_seen_ts < min_track_age_s:
        return SpeedEstimate(None, "трек слишком молодой")
    # Обрезан верх — рамка ниже роста и скорость завышена; обрезан низ — «ноги» это край кадра.
    # Обрезанный бок только занижает скорость (центр рамки отстаёт), ложной тревоги не даст —
    # поэтому человек, входящий в кадр сбоку, оценивается сразу (бегущий виден ~1 с, KTH).
    if touches_edge(track.bbox, sides=False):
        return SpeedEstimate(None, "рамка у верхнего или нижнего края кадра")
    pts = window_points(track, ts, window_s)
    if len(pts) < min_points:
        return SpeedEstimate(None, "мало точек в окне")
    heights = [p.bbox_h for p in pts]
    med_h = median(heights)
    if med_h <= 0 or (max(heights) - min(heights)) / med_h > max_height_change:
        return SpeedEstimate(None, "высота рамки скачет")
    speeds = []
    for part in (pts, window_points(track, ts, window_s * 0.75)):
        v = regression_velocity(part) if len(part) >= max(2, min_points - 1) else None
        if v is not None:
            speeds.append((v[0] ** 2 + v[1] ** 2) ** 0.5 / med_h)
    if not speeds:
        return SpeedEstimate(None, "мало точек в окне")
    return SpeedEstimate(median(speeds))
