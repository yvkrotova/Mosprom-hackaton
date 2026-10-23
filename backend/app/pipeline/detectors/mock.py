"""MockDetector: детерминированная заглушка нейросети с заранее известным сценарием.

Сценарий по video_ts (секунды), относительно первой зоны источника:
    0–2     человек A стоит вне зоны
    2–4     идёт в зону (ноги пересекают границу примерно на 3 с)
    4–11    стоит в зоне; 7–8 — пропадает на 1 с (перекрытие)
    11–13   выходит из зоны и пропадает
    15      ОДИН ложный кадр с человеком в зоне — события быть не должно
    19–24.5 человек B: короткий вход в зону (~3 с в зоне) — второе событие
Итого при параметрах по умолчанию — ровно 2 эпизода.

Шум confidence и рамок зависит только от seed и video_ts, поэтому прогоны повторяемы.
"""

import math
import random
from collections.abc import Sequence

from app.pipeline.geometry import point_in_polygon
from app.pipeline.interfaces import Detection, Detector, Frame, FrameDetections

Point = tuple[float, float]

BOX_W, BOX_H = 0.10, 0.30
FALSE_POSITIVE_TS = 15.0
DURATION_S = 25.0

# Траектории: (t0, t1, откуда, куда, базовая уверенность); точки — "out" / "in" / "in2".
_PERSON_A = [
    (0.0, 2.0, "out", "out", 0.78),
    (2.0, 4.0, "out", "in", 0.74),
    (4.0, 7.0, "in", "in", 0.86),
    # 7.0–8.0 — перекрытие, детекций нет
    (8.0, 11.0, "in", "in", 0.84),
    (11.0, 13.0, "in", "out", 0.72),
]
_PERSON_B = [
    (19.0, 20.0, "out", "out", 0.76),
    (20.0, 21.0, "out", "in2", 0.73),
    (21.0, 23.5, "in2", "in2", 0.81),
    (23.5, 24.5, "in2", "out", 0.70),
]


# Поза COCO-17 в долях рамки (u — слева направо, v — сверху вниз):
# нос, глаза, уши, плечи, локти, запястья, бёдра, колени, лодыжки.
_POSE_TEMPLATE = [
    (0.50, 0.07), (0.46, 0.05), (0.54, 0.05), (0.42, 0.07), (0.58, 0.07),
    (0.30, 0.21), (0.70, 0.21), (0.24, 0.38), (0.76, 0.38), (0.22, 0.53), (0.78, 0.53),
    (0.39, 0.54), (0.61, 0.54), (0.39, 0.76), (0.61, 0.76), (0.39, 0.98), (0.61, 0.98),
]  # fmt: skip
_POSE_LIMB = [
    "head", "head", "head", "head", "head",
    "body", "body", "elbow_l", "elbow_r", "arm_l", "arm_r",
    "body", "body", "knee_l", "knee_r", "leg_l", "leg_r",
]  # fmt: skip
# Смещение по u в долях амплитуды шага: колени и локти качаются вдвое меньше.
_SWING = {"leg_l": 1.0, "leg_r": -1.0, "knee_l": 0.5, "knee_r": -0.5,
          "arm_l": -0.7, "arm_r": 0.7, "elbow_l": -0.35, "elbow_r": 0.35}  # fmt: skip


def _inside_point(polygon: Sequence[Sequence[float]]) -> Point:
    cx = sum(p[0] for p in polygon) / len(polygon)
    cy = sum(p[1] for p in polygon) / len(polygon)
    if point_in_polygon((cx, cy), polygon):
        return cx, cy
    for i in range(1, 20):  # невыпуклый полигон: ищем точку внутри по сетке
        for j in range(1, 20):
            if point_in_polygon((i / 20, j / 20), polygon):
                return i / 20, j / 20
    return cx, cy


def _outside_point(polygon: Sequence[Sequence[float]], inside: Point) -> Point:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    candidates = [
        (min(xs) - 0.12, inside[1]),
        (max(xs) + 0.12, inside[1]),
        (inside[0], max(ys) + 0.05),
        (0.06, 0.95),
        (0.94, 0.95),
    ]
    for x, y in candidates:
        if 0.06 <= x <= 0.94 and BOX_H <= y <= 1.0 and not point_in_polygon((x, y), polygon):
            return x, y
    return 0.06, 0.95


class MockDetector(Detector):
    kind = "mock"

    def __init__(
        self,
        zone_polygon: Sequence[Sequence[float]] | None = None,
        seed: int = 42,
        *,
        false_positive: bool = True,
    ):
        self.model_version = "mock-1.0"
        self.seed = seed
        polygon = zone_polygon or [[0.35, 0.45], [0.75, 0.45], [0.75, 0.95], [0.35, 0.95]]
        inside = _inside_point(polygon)
        self._points: dict[str, Point] = {
            "in": inside,
            "out": _outside_point(polygon, inside),
            # Второй человек встаёт чуть в стороне от первого, но тоже в зоне.
            "in2": self._shifted_inside(polygon, inside),
        }
        self._false_positive_done = not false_positive

    @staticmethod
    def _shifted_inside(polygon: Sequence[Sequence[float]], inside: Point) -> Point:
        for dx in (0.05, -0.05, 0.0):
            p = (inside[0] + dx, inside[1])
            if point_in_polygon(p, polygon):
                return p
        return inside

    def _position(self, track: list, ts: float) -> tuple[Point, float, bool] | None:
        """(точка ног, базовая уверенность, идёт ли человек) или None — человека нет."""
        for t0, t1, a, b, conf in track:
            if t0 <= ts < t1:
                k = (ts - t0) / (t1 - t0)
                (ax, ay), (bx, by) = self._points[a], self._points[b]
                return (ax + (bx - ax) * k, ay + (by - ay) * k), conf, a != b
        return None

    @staticmethod
    def _keypoints(
        bbox: tuple[float, float, float, float], ts: float, walking: bool, rng: random.Random
    ) -> list[tuple[float, float, float]]:
        """17 точек позы COCO внутри рамки; при ходьбе руки и ноги качаются."""
        x1, y1, x2, y2 = bbox
        swing = 0.09 * math.sin(ts * 2 * math.pi * 1.4) if walking else 0.0
        points = []
        for (u, v), kind in zip(_POSE_TEMPLATE, _POSE_LIMB, strict=True):
            x = x1 + (u + swing * _SWING.get(kind, 0.0)) * (x2 - x1)
            y = y1 + v * (y2 - y1)
            conf = 0.9 + rng.uniform(-0.08, 0.08)
            points.append(
                (round(min(max(x, 0.0), 1.0), 4), round(min(max(y, 0.0), 1.0), 4), round(conf, 3))
            )
        return points

    @staticmethod
    def _box(feet: Point, rng: random.Random) -> tuple[float, float, float, float]:
        fx = feet[0] + rng.uniform(-0.003, 0.003)
        fy = feet[1] + rng.uniform(-0.003, 0.003)
        x1 = min(max(fx - BOX_W / 2, 0.0), 1.0 - BOX_W)
        y2 = min(max(fy, BOX_H), 1.0)
        return (round(x1, 4), round(y2 - BOX_H, 4), round(x1 + BOX_W, 4), round(y2, 4))

    def infer(self, frame: Frame) -> FrameDetections:
        ts = frame.video_ts
        rng = random.Random(f"{self.seed}:{ts:.3f}")
        detections = []
        for person in (_PERSON_A, _PERSON_B):
            pos = self._position(person, ts)
            if pos is not None:
                feet, base_conf, walking = pos
                bbox = self._box(feet, rng)
                detections.append(
                    Detection(
                        label="person",
                        confidence=round(min(0.99, base_conf + rng.uniform(-0.05, 0.05)), 3),
                        bbox=bbox,
                        keypoints=self._keypoints(bbox, ts, walking, rng),
                    )
                )
        if ts >= FALSE_POSITIVE_TS and not self._false_positive_done:
            self._false_positive_done = True
            fx, fy = self._points["in"]
            bbox = self._box((fx, fy - 0.02), rng)
            detections.append(
                Detection(
                    label="person",
                    confidence=0.55,
                    bbox=bbox,
                    keypoints=self._keypoints(bbox, ts, False, rng),
                )
            )
        return FrameDetections(
            source_id=frame.source_id,
            frame_idx=frame.frame_idx,
            video_ts=ts,
            frame_w=frame.width,
            frame_h=frame.height,
            inference_ms=round(12.0 + rng.uniform(0, 6), 2),
            model_version=self.model_version,
            detections=detections,
        )
