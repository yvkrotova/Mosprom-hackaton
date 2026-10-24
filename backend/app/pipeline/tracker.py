"""Трекеры: назначают детекциям устойчивые track_id между кадрами.

- ByteTrackTracker (по умолчанию, TRACKER=bytetrack) — фильтр Калмана + двухэтапное сопоставление:
  сначала уверенные детекции, затем слабые (частично закрытый человек) продлевают уже идущие треки.
  Новый трек подтверждается (confirmed) после TRACK_MIN_HITS попаданий; пропавший трек живёт
  TRACK_BUFFER_S секунд видео как lost (рамка — прогноз Калмана) и может быть подхвачен снова.
- IoUTracker (TRACKER=iou) — прежний жадный трекер по IoU, для сравнения.

Оба ведут историю ног трека (TrackPoint, пиксели кадра) — по ней сценарии считают скорость.
Если детектор сам дал track_id всем детекциям, они используются как есть (passthrough).
"""

from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

from app.pipeline.geometry import iou
from app.pipeline.interfaces import Detection, FrameDetections, Track, Tracker, TrackPoint

HISTORY_S = 3.0  # сколько секунд видео держать в истории трека


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    kind: str = "bytetrack"  # bytetrack | iou
    high_thresh: float = 0.5  # уверенные детекции: первый этап и новые треки
    low_thresh: float = 0.1  # ниже — мусор; между low и high — второй этап
    match_thresh: float = 0.8  # максимальная «стоимость» 1 − IoU на первом этапе
    buffer_s: float = 2.0  # сколько секунд видео держать потерянный трек
    min_hits: int = 3  # попаданий до confirmed
    # Запасное сопоставление: центр детекции не дальше стольких ростов от прогноза трека.
    max_center_dist_h: float = 0.8


def build_tracker(config: TrackerConfig) -> Tracker:
    if config.kind == "iou":
        # IoU-трекер не умеет отличать слабые детекции — отдаём ему только уверенные.
        return IoUTracker(max_age_s=config.buffer_s, min_confidence=config.high_thresh)
    if config.kind == "bytetrack":
        return ByteTrackTracker(config)
    raise ValueError(f"Неизвестный трекер: {config.kind!r} (ожидается bytetrack или iou)")


def _remember(track: Track, bbox_px: tuple[float, float, float, float], ts: float) -> None:
    """Добавляет точку ног в историю трека и отрезает всё старше HISTORY_S."""
    x1, _, x2, y2 = bbox_px
    track.history.append(TrackPoint(ts, (x1 + x2) / 2, y2, y2 - bbox_px[1]))
    while track.history and ts - track.history[0].video_ts > HISTORY_S:
        track.history.popleft()


def _to_px(bbox, w: int, h: int) -> tuple[float, float, float, float]:
    return (bbox[0] * w, bbox[1] * h, bbox[2] * w, bbox[3] * h)


def _to_rel(box_px, w: int, h: int) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = box_px
    return (
        round(min(max(x1 / w, 0.0), 1.0), 5),
        round(min(max(y1 / h, 0.0), 1.0), 5),
        round(min(max(x2 / w, 0.0), 1.0), 5),
        round(min(max(y2 / h, 0.0), 1.0), 5),
    )


def _keypoints(det: Detection):
    return [tuple(k) for k in det.keypoints] if det.keypoints else None


# --- IoU ---------------------------------------------------------------------------------------


class IoUTracker(Tracker):
    """Жадное сопоставление по IoU с треками того же класса.

    Трек живёт max_age_s секунд видео без наблюдений (должно быть не меньше gap_tolerance_s
    сценариев, иначе человек после перекрытия получит новый id и новый эпизод).
    """

    def __init__(
        self, iou_threshold: float = 0.3, max_age_s: float = 2.0, min_confidence: float = 0.0
    ) -> None:
        self.iou_threshold = iou_threshold
        self.max_age_s = max_age_s
        self.min_confidence = min_confidence
        self._tracks: dict[int, Track] = {}
        self._next_id = 1

    def update(self, dets: FrameDetections) -> list[Track]:
        ts = dets.video_ts
        self._tracks = {
            tid: t for tid, t in self._tracks.items() if ts - t.last_seen_ts <= self.max_age_s
        }
        if dets.detections and all(d.track_id is not None for d in dets.detections):
            return _passthrough(self._tracks, dets)
        detections = [d for d in dets.detections if d.confidence >= self.min_confidence]

        pairs = sorted(
            (
                (iou(track.bbox, det.bbox), tid, i)
                for tid, track in self._tracks.items()
                for i, det in enumerate(detections)
                if track.label == det.label
            ),
            reverse=True,
        )
        assigned: dict[int, int] = {}  # индекс детекции -> track_id
        used_tracks: set[int] = set()
        for score, tid, i in pairs:
            if score < self.iou_threshold:
                break
            if i in assigned or tid in used_tracks:
                continue
            assigned[i] = tid
            used_tracks.add(tid)

        observed: list[Track] = []
        for i, det in enumerate(detections):
            tid = assigned.get(i)
            prev = self._tracks.get(tid) if tid is not None else None
            if tid is None:
                tid = self._next_id
                self._next_id += 1
            track = Track(
                track_id=tid,
                label=det.label,
                bbox=tuple(det.bbox),
                confidence=det.confidence,
                first_seen_ts=prev.first_seen_ts if prev else ts,
                last_seen_ts=ts,
                keypoints=_keypoints(det),
                hits=prev.hits + 1 if prev else 1,
                history=prev.history if prev else deque(maxlen=128),
                frame_w=dets.frame_w,
                frame_h=dets.frame_h,
            )
            _remember(track, _to_px(det.bbox, dets.frame_w, dets.frame_h), ts)
            self._tracks[tid] = track
            observed.append(track)
        return observed


def _passthrough(tracks: dict[int, Track], dets: FrameDetections) -> list[Track]:
    observed = []
    for det in dets.detections:
        tid = int(det.track_id)  # type: ignore[arg-type]
        prev = tracks.get(tid)
        track = Track(
            track_id=tid,
            label=det.label,
            bbox=tuple(det.bbox),
            confidence=det.confidence,
            first_seen_ts=prev.first_seen_ts if prev else dets.video_ts,
            last_seen_ts=dets.video_ts,
            keypoints=_keypoints(det),
            hits=prev.hits + 1 if prev else 1,
            history=prev.history if prev else deque(maxlen=128),
            frame_w=dets.frame_w,
            frame_h=dets.frame_h,
        )
        _remember(track, _to_px(det.bbox, dets.frame_w, dets.frame_h), dets.video_ts)
        tracks[tid] = track
        observed.append(track)
    return observed


# --- ByteTrack ---------------------------------------------------------------------------------


class KalmanXYAH:
    """Фильтр Калмана с постоянной скоростью в (cx, cy, aspect = w/h, h) — как в ByteTrack.

    Шумы пропорциональны высоте рамки, поэтому поведение не зависит от расстояния до камеры.
    Шаг по времени — в кадрах анализа; dt масштабирует переход, чтобы разная частота кадров
    давала одинаковое движение.
    """

    STD_POS = 1 / 20
    STD_VEL = 1 / 160

    def initiate(self, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mean = np.r_[z, np.zeros(4)]
        h = z[3]
        p, v = 2 * self.STD_POS * h, 10 * self.STD_VEL * h
        std = [p, p, 1e-2, p, v, v, 1e-5, v]
        return mean, np.diag(np.square(std))

    def predict(
        self, mean: np.ndarray, cov: np.ndarray, dt: float
    ) -> tuple[np.ndarray, np.ndarray]:
        f = np.eye(8)
        f[:4, 4:] = np.eye(4) * dt
        h = mean[3]
        std = [self.STD_POS * h, self.STD_POS * h, 1e-2, self.STD_POS * h,
               self.STD_VEL * h, self.STD_VEL * h, 1e-5, self.STD_VEL * h]  # fmt: skip
        q = np.diag(np.square(std)) * max(dt, 1e-3)
        return f @ mean, f @ cov @ f.T + q

    def update(
        self, mean: np.ndarray, cov: np.ndarray, z: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        hm = np.eye(4, 8)
        h = mean[3]
        r = np.diag(np.square([self.STD_POS * h, self.STD_POS * h, 1e-1, self.STD_POS * h]))
        s = hm @ cov @ hm.T + r
        k = cov @ hm.T @ np.linalg.inv(s)
        mean = mean + k @ (z - hm @ mean)
        cov = (np.eye(8) - k @ hm) @ cov
        return mean, cov


def _xyah(b) -> np.ndarray:
    x1, y1, x2, y2 = b
    w, h = max(x2 - x1, 1e-6), max(y2 - y1, 1e-6)
    return np.array([x1 + w / 2, y1 + h / 2, w / h, h])


def _xyxy(m: np.ndarray) -> tuple[float, float, float, float]:
    cx, cy, a, h = m[:4]
    w = a * h
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


class _BTrack:
    __slots__ = ("track", "mean", "cov", "state", "last_ts", "box_px")

    def __init__(self, track: Track, mean: np.ndarray, cov: np.ndarray, box_px) -> None:
        self.track = track
        self.mean = mean
        self.cov = cov
        self.state = track.state
        self.last_ts = track.last_seen_ts
        self.box_px = box_px


def _iou_px(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / area


def _match(tracks: list[_BTrack], dets: list[tuple[Detection, tuple]], max_cost: float,
           fuse_score: bool) -> tuple[list[tuple[int, int]], list[int], list[int]]:  # fmt: skip
    """Венгерский алгоритм по стоимости 1 − IoU; разные классы не сопоставляются."""
    if not tracks or not dets:
        return [], list(range(len(tracks))), list(range(len(dets)))
    cost = np.ones((len(tracks), len(dets)))
    for i, t in enumerate(tracks):
        for j, (det, box) in enumerate(dets):
            if det.label != t.track.label:
                continue
            sim = _iou_px(t.box_px, box)
            if fuse_score:
                sim *= det.confidence  # как в ByteTrack: уверенность усиливает совпадение
            cost[i, j] = 1.0 - sim
    rows, cols = linear_sum_assignment(cost)
    matches = [(r, c) for r, c in zip(rows, cols, strict=True) if cost[r, c] <= max_cost]
    matched_t = {r for r, _ in matches}
    matched_d = {c for _, c in matches}
    return (
        matches,
        [i for i in range(len(tracks)) if i not in matched_t],
        [j for j in range(len(dets)) if j not in matched_d],
    )


def _match_distance(tracks: list[_BTrack], dets: list[tuple[Detection, tuple]], max_dist_h: float
                    ) -> tuple[list[tuple[int, int]], list[int], list[int]]:  # fmt: skip
    """Запасное сопоставление по расстоянию центров в «ростах» трека, для рамок, которые уже не
    пересекаются (быстрое движение при низкой частоте кадров). Рост должен быть похож (±50%)."""
    if not tracks or not dets:
        return [], list(range(len(tracks))), list(range(len(dets)))
    big = 1e6
    cost = np.full((len(tracks), len(dets)), big)
    for i, t in enumerate(tracks):
        tx1, ty1, tx2, ty2 = t.box_px
        th = max(ty2 - ty1, 1e-6)
        for j, (det, (x1, y1, x2, y2)) in enumerate(dets):
            dh = y2 - y1
            if det.label != t.track.label or not 0.67 <= dh / th <= 1.5:
                continue
            d = np.hypot((x1 + x2 - tx1 - tx2) / 2, (y1 + y2 - ty1 - ty2) / 2) / th
            if d <= max_dist_h:
                cost[i, j] = d
    rows, cols = linear_sum_assignment(cost)
    matches = [(r, c) for r, c in zip(rows, cols, strict=True) if cost[r, c] < big]
    mt, md = {r for r, _ in matches}, {c for _, c in matches}
    return (
        matches,
        [i for i in range(len(tracks)) if i not in mt],
        [j for j in range(len(dets)) if j not in md],
    )


class ByteTrackTracker(Tracker):
    """ByteTrack (Zhang et al., 2022) без сторонних библиотек, кроме scipy.

    Отличия от оригинала, важные для конвейера:
    - время — по video_ts (буфер потерянных в секундах, а не кадрах): не зависит от analysis_fps;
    - новый трек становится confirmed после min_hits попаданий (оригинал — со второго кадра);
    - сопоставляются только детекции того же класса;
    - этап 1b: если рамки уже не пересекаются (бегущий человек при 5–10 кадрах/с смещается за кадр
      больше своей ширины), уверенная детекция сопоставляется по расстоянию центров в ростах.
    """

    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.config = config or TrackerConfig()
        self.kf = KalmanXYAH()
        self._tracks: list[_BTrack] = []
        self._next_id = 1
        self._last_ts: float | None = None
        self._frame_dt: float | None = None  # обычный шаг между кадрами анализа, с
        self._passthrough: dict[int, Track] = {}

    def update(self, dets: FrameDetections) -> list[Track]:
        if dets.detections and all(d.track_id is not None for d in dets.detections):
            return _passthrough(self._passthrough, dets)
        cfg = self.config
        ts, w, h = dets.video_ts, dets.frame_w, dets.frame_h
        # Шаг Калмана — в «кадрах анализа», чтобы скорость в фильтре не зависела от fps.
        if self._last_ts is not None and ts > self._last_ts:
            step = ts - self._last_ts
            self._frame_dt = step if self._frame_dt is None else min(self._frame_dt, step)
        dt = (
            1.0
            if self._frame_dt is None or self._last_ts is None
            else (ts - self._last_ts) / self._frame_dt
        )
        self._last_ts = ts

        boxes = [(d, _to_px(d.bbox, w, h)) for d in dets.detections]
        high = [b for b in boxes if b[0].confidence >= cfg.high_thresh]
        low = [b for b in boxes if cfg.low_thresh <= b[0].confidence < cfg.high_thresh]

        for t in self._tracks:
            t.mean, t.cov = self.kf.predict(t.mean, t.cov, dt)
            t.box_px = _xyxy(t.mean)

        confirmed = [t for t in self._tracks if t.state in ("confirmed", "lost")]
        tentative = [t for t in self._tracks if t.state == "tentative"]

        # 1. Подтверждённые и потерянные — с уверенными детекциями.
        m1, u_tracks, u_high = _match(confirmed, high, cfg.match_thresh, fuse_score=True)
        for ti, di in m1:
            self._apply(confirmed[ti], *high[di], ts, w, h)
        # 1b. Рамки разошлись (быстрое движение, низкая fps): близость центров в ростах.
        left = [confirmed[i] for i in u_tracks]
        left_high = [high[j] for j in u_high]
        m1b, u_left, u_left_high = _match_distance(left, left_high, cfg.max_center_dist_h)
        for ti, di in m1b:
            self._apply(left[ti], *left_high[di], ts, w, h)
        u_tracks = [confirmed.index(left[i]) for i in u_left]
        u_high = [high.index(left_high[j]) for j in u_left_high]
        # 2. Оставшиеся подтверждённые (не lost) — со слабыми детекциями: продлеваем трек.
        remaining = [confirmed[i] for i in u_tracks if confirmed[i].state == "confirmed"]
        m2, u_rem, _ = _match(remaining, low, 0.5, fuse_score=False)
        for ti, di in m2:
            self._apply(remaining[ti], *low[di], ts, w, h)
        for i in u_rem:
            remaining[i].state = remaining[i].track.state = "lost"
        # 3. Неподтверждённые — с оставшимися уверенными; не нашлись — удаляются.
        rest_high = [high[i] for i in u_high]
        m3, u_tent, u_new = _match(tentative, rest_high, 0.7, fuse_score=True)
        for ti, di in m3:
            self._apply(tentative[ti], *rest_high[di], ts, w, h)
        tent_left = [tentative[i] for i in u_tent]
        new_left = [rest_high[j] for j in u_new]
        m3b, u_tent_left, u_new_left = _match_distance(tent_left, new_left, cfg.max_center_dist_h)
        for ti, di in m3b:
            self._apply(tent_left[ti], *new_left[di], ts, w, h)
        u_new = [rest_high.index(new_left[j]) for j in u_new_left]
        dead = {id(tent_left[i]) for i in u_tent_left}
        # 4. Новые треки из неиспользованных уверенных детекций.
        for di in u_new:
            self._new(*rest_high[di], ts, w, h)
        # 5. Потерянные дольше буфера удаляются.
        self._tracks = [
            t for t in self._tracks
            if id(t) not in dead and not (t.state == "lost" and ts - t.last_ts > cfg.buffer_s)
        ]  # fmt: skip

        out = []
        for t in self._tracks:
            if t.state == "lost":
                t.track.bbox = _to_rel(t.box_px, w, h)  # прогноз для живого просмотра
            out.append(t.track)
        return out

    def _apply(self, t: _BTrack, det: Detection, box_px, ts: float, w: int, h: int) -> None:
        t.mean, t.cov = self.kf.update(t.mean, t.cov, _xyah(box_px))
        t.box_px = _xyxy(t.mean)
        tr = t.track
        tr.hits += 1
        if t.state == "lost" or (t.state == "tentative" and tr.hits >= self.config.min_hits):
            t.state = "confirmed"
        tr.state = t.state
        tr.bbox = _to_rel(t.box_px, w, h)
        tr.confidence = det.confidence
        tr.last_seen_ts = ts
        tr.keypoints = _keypoints(det)
        t.last_ts = ts
        _remember(tr, box_px, ts)

    def _new(self, det: Detection, box_px, ts: float, w: int, h: int) -> None:
        mean, cov = self.kf.initiate(_xyah(box_px))
        state = "confirmed" if self.config.min_hits <= 1 else "tentative"
        track = Track(
            track_id=self._next_id,
            label=det.label,
            bbox=tuple(det.bbox),
            confidence=det.confidence,
            first_seen_ts=ts,
            last_seen_ts=ts,
            keypoints=_keypoints(det),
            hits=1,
            state=state,
            frame_w=w,
            frame_h=h,
        )
        self._next_id += 1
        _remember(track, box_px, ts)
        self._tracks.append(_BTrack(track, mean, cov, box_px))
