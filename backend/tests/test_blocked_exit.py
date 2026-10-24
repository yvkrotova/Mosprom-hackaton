"""blocked_exit: синтетические кадры (numpy) — фон с текстурой и шумом, «человек» (прямоугольник с
детекцией person), «предмет» (прямоугольник без детекции). Весь конвейер: Pipeline → SceneAnalyzer →
правило → EpisodeManager."""

import cv2
import numpy as np
import pytest

from app.pipeline.interfaces import Detection, Detector, Frame, FrameDetections, FrameSource
from app.pipeline.runner import Pipeline
from app.pipeline.scene import SceneAnalyzer, SceneParams
from tests.helpers import RecordingSink, make_config

W, H = 640, 360
ZONE = [[0.25, 0.3], [0.75, 0.3], [0.75, 0.9], [0.25, 0.9]]  # 320×216 px
FPS = 4.0


def _background() -> np.ndarray:
    rng = np.random.default_rng(1)
    noise = rng.random((H // 8, W // 8, 3)) * 80 + 80
    bg = cv2.resize(noise.astype(np.uint8), (W, H), interpolation=cv2.INTER_CUBIC)
    cv2.line(bg, (0, 200), (W, 220), (60, 160, 200), 6)  # разметка на полу
    return bg


BG = _background()


class Scene:
    """Описание ролика: функции video_ts -> что в кадре."""

    def __init__(self, duration, person=None, obj=None, mover=None, brightness=None):
        self.duration, self.person, self.obj, self.mover, self.brightness = (
            duration, person, obj, mover, brightness)  # fmt: skip

    def image(self, ts: float) -> np.ndarray:
        img = BG.astype(np.float32)
        for spec in (self.obj, self.mover):
            box = spec(ts) if spec else None
            if box:
                x1, y1, x2, y2 = box
                img[y1:y2, x1:x2] = (40, 55, 70)  # тёмная коробка
        p = self.person(ts) if self.person else None
        if p:
            x1, y1, x2, y2 = p
            img[y1:y2, x1:x2] = (150, 60, 30)  # рабочий в синем
        if self.brightness:
            img *= self.brightness(ts)
        rng = np.random.default_rng(int(ts * 1000))
        img += rng.normal(0, 3, img.shape)
        return np.clip(img, 0, 255).astype(np.uint8)


class Frames(FrameSource):
    def __init__(self, scene: Scene) -> None:
        self.scene = scene

    def __iter__(self):
        for i in range(int(self.scene.duration * FPS) + 1):
            ts = round(i / FPS, 3)
            yield Frame("s", i, ts, W, H, self.scene.image(ts))


class PersonDetector(Detector):
    kind, model_version = "mock", "scene-test"

    def __init__(self, scene: Scene) -> None:
        self.scene = scene

    def infer(self, frame: Frame) -> FrameDetections:
        p = self.scene.person(frame.video_ts) if self.scene.person else None
        dets = []
        if p:
            x1, y1, x2, y2 = p
            dets.append(
                Detection(label="person", confidence=0.9, bbox=(x1 / W, y1 / H, x2 / W, y2 / H))
            )
        return FrameDetections(
            frame_idx=frame.frame_idx, video_ts=frame.video_ts, frame_w=W, frame_h=H,
            inference_ms=1.0, model_version="t", detections=dets,
        )  # fmt: skip


def run(scene: Scene, *, scenario="blocked_exit", params=None):
    cfg = make_config([ZONE], params=params, scenario_type=scenario)
    sink = RecordingSink()
    pipeline = Pipeline(cfg, PersonDetector(scene), sink, scene_hz=4, scene_bootstrap_s=3)
    pipeline.run(Frames(scene))
    return sink, pipeline


def box(x, y, w, h):
    return (int(x), int(y), int(x + w), int(y + h))


def walker(t0, t1, y=150):
    """Человек идёт слева направо через зону с t0 до t1."""

    def pos(ts):
        if not t0 <= ts < t1:
            return None
        return box(40 + (560 - 40) * (ts - t0) / (t1 - t0), y, 40, 120)

    return pos


OBJECT = box(300, 230, 80, 60)  # ~7% площади зоны


def test_1_person_walks_through_zone_no_event():
    sink, _ = run(Scene(25, person=walker(5, 9)))
    assert sink.episodes() == []


def test_2_person_stands_in_zone_15s_no_event():
    stand = lambda ts: box(300, 160, 40, 120) if 5 <= ts < 20 else None  # noqa: E731
    sink, _ = run(Scene(30, person=stand))
    assert sink.episodes() == []


def test_3_4_static_object_one_episode_then_removed():
    obj = lambda ts: OBJECT if 5 <= ts < 30 else None  # noqa: E731
    sink, _ = run(Scene(40, obj=obj))
    (ep,) = sink.episodes()
    assert len(sink.event_ids()) == 1  # одно event.created
    assert ep["scenario_type"] == "blocked_exit" and ep["track_id"] is None
    assert 5 <= ep["video_ts_start"] <= 8  # предмет «оседает» в быстром фоне за 1–2 с
    assert ep["video_ts_detected"] - ep["video_ts_start"] == pytest.approx(10, abs=0.3)
    assert ep["state"] == "ended" and 29.5 <= ep["video_ts_end"] < 30  # убрали — конец
    assert 5 <= ep["details"]["blocked_pct"] <= 9 and ep["details"]["obstacles"] == 1
    annotated = [m for _, k, _, m in sink.evidence if k == "frame_annotated"]
    assert annotated and annotated[0]["bboxes"][0]["track_id"] is None


def test_5_object_lies_five_minutes_episode_stays_active():
    obj = lambda ts: OBJECT if ts >= 5 else None  # noqa: E731
    sink, pipeline = run(Scene(300, obj=obj))
    (ep,) = sink.episodes()
    assert len(sink.event_ids()) == 1
    assert ep["video_ts_end"] >= 299  # не «впитался» в фон, эпизод шёл до конца видео


def test_6_moving_object_without_detection_no_event():
    def cart(ts):
        if not 5 <= ts < 11:
            return None
        return box(20 + 100 * (ts - 5), 240, 80, 60)  # тележка 100 px/с

    sink, _ = run(Scene(25, mover=cart))
    assert sink.episodes() == []


def test_7_brightness_jump_no_event_and_unknown():
    light = lambda ts: 1.6 if ts >= 8 else 1.0  # noqa: E731
    sink, _ = run(Scene(30, brightness=light))
    assert sink.episodes() == []
    # Сам анализ сцены на скачке говорит «оценить нельзя», а не «заблокировано».
    zone = make_config([ZONE]).zones[0]
    scene = Scene(10, brightness=light)
    analyzer = SceneAnalyzer({zone.id: (_zone_geom(zone), SceneParams())}, hz=4, bootstrap_s=3)
    det = PersonDetector(scene)
    results = {}
    for f in Frames(scene):
        results[f.video_ts] = analyzer.update(f, [], det.infer(f))[zone.id]
    assert results[7.75].ready and not results[8.0].ready
    assert "освещ" in results[8.0].unknown_reason
    assert results[8.25].ready and not results[8.25].static_blobs  # нормализация яркости


def test_8_object_next_to_standing_person_detected():
    stand = lambda ts: box(220, 160, 40, 120) if ts >= 4 else None  # noqa: E731
    obj = lambda ts: box(330, 230, 80, 60) if ts >= 6 else None  # noqa: E731
    sink, _ = run(Scene(25, person=stand, obj=obj))
    (ep,) = sink.episodes()
    assert ep["track_id"] is None and ep["details"]["obstacles"] == 1


def test_9_small_object_no_event():
    obj = lambda ts: box(320, 240, 30, 20) if ts >= 5 else None  # noqa: E731  ~0.9% зоны
    sink, _ = run(Scene(25, obj=obj))
    assert sink.episodes() == []


def test_10_no_scene_rules_no_scene_analysis():
    _, pipeline = run(Scene(3), scenario="danger_zone")
    assert pipeline.scene is None
    _, pipeline = run(Scene(3))
    assert pipeline.scene is not None


def test_saved_reference_is_used_and_zone_states_report_unknown_before_ready():
    zone = make_config([ZONE]).zones[0]
    geom = _zone_geom(zone)
    obj = lambda ts: OBJECT  # noqa: E731  предмет лежит с самого начала
    scene = Scene(4, obj=obj)
    det = PersonDetector(scene)
    auto = SceneAnalyzer({zone.id: (geom, SceneParams())}, hz=4, bootstrap_s=1)
    saved = SceneAnalyzer({zone.id: (geom, SceneParams())}, hz=4, bootstrap_s=1,
                          references={zone.id: BG})  # fmt: skip
    last_auto = last_saved = None
    for f in Frames(scene):
        dets = det.infer(f)
        last_auto, last_saved = (
            auto.update(f, [], dets)[zone.id],
            saved.update(f, [], dets)[zone.id],
        )
    # Без эталона предмет, лежавший с начала, вошёл в эталон — не виден (честное ограничение);
    # с сохранённым пустым проходом — виден.
    assert last_auto.reference == "auto" and not last_auto.static_blobs
    assert (
        last_saved.reference == "saved"
        and sum(b["area_pct"] for b in last_saved.static_blobs) > 0.05
    )


def _zone_geom(zone):
    from app.pipeline.interfaces import ZoneGeometry

    return ZoneGeometry(zone.id, zone.name, [tuple(p) for p in zone.polygon])


def test_camera_shift_gives_unknown_not_blocked():
    """Камеру повернули (новый ракурс с 8 с) — зона «оценить нельзя», события нет."""

    class TurnedScene(Scene):
        def image(self, ts):
            img = super().image(ts)
            return np.ascontiguousarray(img[::-1, ::-1]) if ts >= 8 else img

    scene = TurnedScene(30)
    sink, _ = run(scene)
    assert sink.episodes() == []
    zone = make_config([ZONE]).zones[0]
    analyzer = SceneAnalyzer({zone.id: (_zone_geom(zone), SceneParams())}, hz=4, bootstrap_s=3)
    det = PersonDetector(scene)
    results = {f.video_ts: analyzer.update(f, [], det.infer(f))[zone.id] for f in Frames(scene)}
    assert results[7.75].ready and not results[8.0].ready
    assert "сдвиг" in results[8.0].unknown_reason
