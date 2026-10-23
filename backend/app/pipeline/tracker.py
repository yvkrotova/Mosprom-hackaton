"""Трекер: назначает детекциям устойчивые track_id между кадрами."""

from app.pipeline.geometry import iou
from app.pipeline.interfaces import FrameDetections, Track, Tracker


class IoUTracker(Tracker):
    """Жадное сопоставление по IoU с треками того же класса.

    Трек живёт max_age_s секунд видео без наблюдений (должно быть не меньше gap_tolerance_s
    сценариев, иначе человек после перекрытия получит новый id и новый эпизод).
    Если детектор сам дал track_id всем детекциям — они используются как есть (passthrough).
    """

    def __init__(self, iou_threshold: float = 0.3, max_age_s: float = 2.0) -> None:
        self.iou_threshold = iou_threshold
        self.max_age_s = max_age_s
        self._tracks: dict[int, Track] = {}
        self._next_id = 1

    def update(self, dets: FrameDetections) -> list[Track]:
        ts = dets.video_ts
        self._tracks = {
            tid: t for tid, t in self._tracks.items() if ts - t.last_seen_ts <= self.max_age_s
        }
        if dets.detections and all(d.track_id is not None for d in dets.detections):
            return self._passthrough(dets)

        pairs = sorted(
            (
                (iou(track.bbox, det.bbox), tid, i)
                for tid, track in self._tracks.items()
                for i, det in enumerate(dets.detections)
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
        for i, det in enumerate(dets.detections):
            tid = assigned.get(i)
            if tid is None:
                tid = self._next_id
                self._next_id += 1
                first_seen = ts
            else:
                first_seen = self._tracks[tid].first_seen_ts
            track = Track(
                track_id=tid,
                label=det.label,
                bbox=tuple(det.bbox),
                confidence=det.confidence,
                first_seen_ts=first_seen,
                last_seen_ts=ts,
                keypoints=[tuple(k) for k in det.keypoints] if det.keypoints else None,
            )
            self._tracks[tid] = track
            observed.append(track)
        return observed

    def _passthrough(self, dets: FrameDetections) -> list[Track]:
        observed = []
        for det in dets.detections:
            tid = int(det.track_id)  # type: ignore[arg-type]
            prev = self._tracks.get(tid)
            track = Track(
                track_id=tid,
                label=det.label,
                bbox=tuple(det.bbox),
                confidence=det.confidence,
                first_seen_ts=prev.first_seen_ts if prev else dets.video_ts,
                last_seen_ts=dets.video_ts,
                keypoints=[tuple(k) for k in det.keypoints] if det.keypoints else None,
            )
            self._tracks[tid] = track
            observed.append(track)
        return observed
