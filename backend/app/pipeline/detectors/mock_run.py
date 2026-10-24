"""MockRunDetector: детерминированная сцена для run_fast_movement (MOCK_SCENARIO=run_fast_movement).

Всё задано по video_ts, поэтому результат не зависит от частоты анализа. Кадр 16:9.
Скорости — в «ростах человека в секунду» (смещение ног / высота рамки).

    W  0–20 с   медленно ходит туда-обратно (0.6 роста/с), рост 0.30 кадра;
                на 10.0 с — ОДИН кадр с рамкой, сдвинутой на полроста (ошибка детектора)
    R  2–4 с    идёт (0.8 роста/с), 4–6.4 с бежит (2.6 роста/с), с 6.4 с стоит до 9 с;
                5.6–5.9 с — перекрыт (детекций нет)
    F  10–14 с  далёкий бегущий: рост 0.08 кадра, 2.5 роста/с
Итого при параметрах по умолчанию — 2 эпизода бега: R (~4–7.4 с) и F (~10–14 с).
"""

import random

from app.pipeline.interfaces import Detection, Detector, Frame, FrameDetections

ASPECT = 16 / 9
DURATION_S = 20.0
JUMP_TS = 10.0
OCCLUSION = (5.6, 5.9)


def _box(foot_x: float, foot_y: float, h: float) -> tuple[float, float, float, float]:
    w = h * 0.4 / ASPECT  # ширина человека ≈ 0.4 роста, в долях ширины кадра
    return (
        round(foot_x - w / 2, 4),
        round(foot_y - h, 4),
        round(foot_x + w / 2, 4),
        round(foot_y, 4),
    )


def _dx(speed: float, h: float, seconds: float) -> float:
    """Смещение по x в долях ширины кадра при скорости speed ростов/с."""
    return speed * h * seconds / ASPECT


def walker(ts: float) -> tuple[float, float, float] | None:
    h, period = 0.30, 16.0
    span = _dx(0.6, h, period / 2)  # туда за полпериода
    k = (ts % period) / (period / 2)
    x = 0.1 + span * (k if k <= 1 else 2 - k)
    if JUMP_TS <= ts < JUMP_TS + 0.05:
        x += _dx(0.5, h, 1.0)  # скачок на полроста за один кадр
    return x, 0.92, h


def runner(ts: float) -> tuple[float, float, float] | None:
    h, y = 0.20, 0.62
    if ts < 2.0 or ts >= 9.0 or OCCLUSION[0] <= ts < OCCLUSION[1]:
        return None
    x = 0.92 - _dx(0.8, h, min(ts, 4.0) - 2.0)
    if ts > 4.0:
        x -= _dx(2.6, h, min(ts, 6.4) - 4.0)
    return x, y, h


def far_runner(ts: float) -> tuple[float, float, float] | None:
    h = 0.08
    if not 10.0 <= ts < 14.0:
        return None
    return 0.25 + _dx(2.5, h, ts - 10.0), 0.42, h


class MockRunDetector(Detector):
    kind = "mock"

    def __init__(self, seed: int = 7) -> None:
        self.model_version = "mock-run-1.0"
        self.seed = seed

    def infer(self, frame: Frame) -> FrameDetections:
        ts = frame.video_ts
        rng = random.Random(f"{self.seed}:{ts:.3f}")
        detections = []
        for person, conf in ((walker, 0.82), (runner, 0.78), (far_runner, 0.66)):
            pos = person(ts)
            if pos is None:
                continue
            x, y, h = pos
            # Небольшой шум рамки (±0.5% кадра), как у настоящего детектора.
            x += rng.uniform(-0.002, 0.002)
            h *= 1 + rng.uniform(-0.02, 0.02)
            detections.append(
                Detection(
                    label="person",
                    confidence=round(conf + rng.uniform(-0.05, 0.05), 3),
                    bbox=_box(x, y, h),
                )
            )
        return FrameDetections(
            source_id=frame.source_id,
            frame_idx=frame.frame_idx,
            video_ts=ts,
            frame_w=frame.width,
            frame_h=frame.height,
            inference_ms=round(10.0 + rng.uniform(0, 4), 2),
            model_version=self.model_version,
            detections=detections,
        )
