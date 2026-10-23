"""EpisodeManager: из покадровых нарушений (RuleHit) делает эпизоды — одно нарушение = одно событие.

Ключ эпизода: (source_id, scenario_type, zone_id, track_id). Все длительности — по video_ts.

    нет эпизода ──hit──▶ кандидат ──держится confirm_window_s──▶ эпизод active (тревога + кадры)
                          │ нет hit > gap_tolerance_s                │ hit — продолжение
                          ▼                                          │ нет hit > gap_tolerance_s
                       забыт (событий нет)                           ▼ или конец видео
                                                                  эпизод ended

Пропуск наблюдения короче gap_tolerance_s (перекрытие, мигание детекции) не завершает эпизод
и не создаёт новый. Обновления активного эпизода уходят не чаще update_interval_s.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.models.base import utcnow
from app.pipeline.annotate import annotate_frame, encode_jpeg
from app.pipeline.interfaces import BaseScenarioParams, Frame, RuleHit, Track, ZoneGeometry
from app.schemas.enums import EpisodeState, EvidenceKind, Priority, ScenarioType
from app.schemas.internal import EventUpsert

logger = logging.getLogger(__name__)

EpisodeKey = tuple[str, str, str | None, int]


class EpisodeSink(Protocol):
    """Куда уходят события. В воркере — HTTP-клиент backend, в тестах — запись в память."""

    def send_event(self, event_id: str, payload: dict[str, Any]) -> None: ...

    def send_evidence(
        self, event_id: str, kind: str, image: bytes, meta: dict[str, Any]
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RuleContext:
    rule_id: str
    scenario_type: ScenarioType
    title: str
    priority: Priority
    version: int
    params: BaseScenarioParams


@dataclass(slots=True)
class _Candidate:
    rule: RuleContext
    first_ts: float
    last_ts: float


@dataclass(slots=True)
class _Episode:
    event_id: str
    rule: RuleContext
    zone_id: str | None
    track_id: int
    video_ts_start: float
    video_ts_detected: float
    last_ts: float
    confidence: float
    detected_at: datetime
    last_sent_ts: float
    dirty: bool = False
    updates_sent: int = field(default=0)


class EpisodeManager:
    def __init__(
        self,
        source_id: str,
        source_name: str,
        rules: dict[str, RuleContext],
        zones: dict[str, ZoneGeometry],
        sink: EpisodeSink,
        model_version: str,
        run_started_at: datetime,
        update_interval_s: float = 1.0,
    ) -> None:
        self.source_id = source_id
        self.source_name = source_name
        self.rules = rules
        self.zones = zones
        self.sink = sink
        self.model_version = model_version
        self.run_started_at = run_started_at
        self.update_interval_s = update_interval_s
        self._candidates: dict[EpisodeKey, _Candidate] = {}
        self._active: dict[EpisodeKey, _Episode] = {}
        self.episodes_confirmed = 0

    # --- время -------------------------------------------------------------------------------

    def wall(self, video_ts: float) -> datetime:
        """«Настенное» время момента видео: время запуска обработки + video_ts."""
        return self.run_started_at + timedelta(seconds=video_ts)

    # --- основной цикл ----------------------------------------------------------------------

    def process(self, frame: Frame, hits: list[RuleHit], tracks: list[Track]) -> None:
        ts = frame.video_ts

        # 1. Завершаем эпизоды, пропавшие дольше допуска (до учёта новых hit на этом кадре).
        for key, ep in list(self._active.items()):
            if ts - ep.last_ts > ep.rule.params.gap_tolerance_s:
                self._end(key, ep)

        # 2. Один hit на ключ (несколько правил могут дать одно и то же нарушение).
        by_key: dict[EpisodeKey, RuleHit] = {}
        for hit in hits:
            key = (self.source_id, str(hit.scenario_type), hit.zone_id, hit.track_id)
            if key not in by_key or hit.confidence > by_key[key].confidence:
                by_key[key] = hit

        for key, hit in by_key.items():
            rule = self.rules[hit.rule_id]
            episode = self._active.get(key)
            if episode is not None:
                episode.last_ts = ts
                episode.confidence = max(episode.confidence, hit.confidence)
                episode.dirty = True
                continue
            cand = self._candidates.get(key)
            if cand is None or ts - cand.last_ts > cand.rule.params.gap_tolerance_s:
                cand = self._candidates[key] = _Candidate(rule=rule, first_ts=ts, last_ts=ts)
            cand.last_ts = ts
            if ts - cand.first_ts >= rule.params.confirm_window_s:
                del self._candidates[key]
                self._confirm(key, cand, hit, rule, frame, tracks)

        # 3. Забываем кандидатов, которые не дотянули до подтверждения.
        for key, cand in list(self._candidates.items()):
            if ts - cand.last_ts > cand.rule.params.gap_tolerance_s:
                del self._candidates[key]

        # 4. Периодические обновления активных эпизодов.
        for ep in self._active.values():
            if ep.dirty and ts - ep.last_sent_ts >= self.update_interval_s:
                self._send(ep, EpisodeState.ACTIVE)
                ep.last_sent_ts = ts
                ep.dirty = False
                ep.updates_sent += 1

    def finish(self) -> None:
        """Конец видео или остановка: все активные эпизоды завершаются."""
        for key, ep in list(self._active.items()):
            self._end(key, ep)
        self._candidates.clear()

    @property
    def active_count(self) -> int:
        return len(self._active)

    def alerting_track_ids(self) -> set[int]:
        """Треки, по которым сейчас идёт подтверждённый эпизод (тревога уже отправлена)."""
        return {ep.track_id for ep in self._active.values()}

    # --- внутреннее ---------------------------------------------------------------------------

    def _confirm(
        self,
        key: EpisodeKey,
        cand: _Candidate,
        hit: RuleHit,
        rule: RuleContext,
        frame: Frame,
        tracks: list[Track],
    ) -> None:
        ep = _Episode(
            event_id=str(uuid.uuid4()),
            rule=rule,
            zone_id=hit.zone_id,
            track_id=hit.track_id,
            video_ts_start=cand.first_ts,
            video_ts_detected=frame.video_ts,
            last_ts=frame.video_ts,
            confidence=hit.confidence,
            detected_at=utcnow(),
            last_sent_ts=frame.video_ts,
        )
        self._active[key] = ep
        self.episodes_confirmed += 1
        logger.info(
            "Эпизод подтверждён: %s зона=%s трек=%s video_ts=%.2f–%.2f",
            rule.scenario_type,
            hit.zone_id,
            hit.track_id,
            cand.first_ts,
            frame.video_ts,
        )
        self._send(ep, EpisodeState.ACTIVE)  # событие раньше кадров: иначе backend ответит 404
        self._send_evidence(ep, hit, frame, tracks)

    def _end(self, key: EpisodeKey, ep: _Episode) -> None:
        del self._active[key]
        self._send(ep, EpisodeState.ENDED)

    def _description(self, ep: _Episode) -> str:
        zone = self.zones.get(ep.zone_id or "")
        where = f"зона «{zone.name}»" if zone else "без зоны"
        return f"{ep.rule.title}: {where}, камера «{self.source_name}»"

    def _send(self, ep: _Episode, state: EpisodeState) -> None:
        ended = state == EpisodeState.ENDED
        payload = EventUpsert(
            source_id=self.source_id,
            scenario_type=ep.rule.scenario_type,
            zone_id=ep.zone_id,
            track_id=ep.track_id,
            priority=ep.rule.priority,
            state=state,
            confidence=round(ep.confidence, 4),
            started_at=self.wall(ep.video_ts_start),
            last_seen_at=self.wall(ep.last_ts),
            ended_at=self.wall(ep.last_ts) if ended else None,
            video_ts_start=round(ep.video_ts_start, 3),
            video_ts_end=round(ep.last_ts, 3),
            video_ts_detected=round(ep.video_ts_detected, 3),
            detected_at=ep.detected_at,
            model_version=self.model_version,
            rule_id=ep.rule.rule_id,
            rule_version=ep.rule.version,
            description=self._description(ep),
        )
        self.sink.send_event(ep.event_id, payload.model_dump(mode="json"))

    def _send_evidence(self, ep: _Episode, hit: RuleHit, frame: Frame, tracks: list[Track]) -> None:
        bboxes = [
            {
                "label": hit.label,
                "confidence": round(hit.confidence, 4),
                "bbox": list(hit.bbox),
                "track_id": hit.track_id,
            }
        ]
        meta = {
            "video_ts": round(frame.video_ts, 3),
            "frame_time": self.wall(frame.video_ts).isoformat().replace("+00:00", "Z"),
            "bboxes": bboxes,
        }
        try:
            if frame.image is not None:
                self.sink.send_evidence(
                    ep.event_id,
                    EvidenceKind.FRAME_RAW,
                    encode_jpeg(frame.image),
                    meta | {"kind": EvidenceKind.FRAME_RAW},
                )
            caption = [
                f"{ep.rule.title} | {self.source_name}",
                f"{self.wall(frame.video_ts):%Y-%m-%d %H:%M:%S}Z | video {frame.video_ts:.1f}s",
            ]
            annotated = annotate_frame(
                frame.image,
                frame.width,
                frame.height,
                list(self.zones.values()),
                tracks,
                hit.track_id,
                hit.zone_id,
                caption,
            )
            self.sink.send_evidence(
                ep.event_id,
                EvidenceKind.FRAME_ANNOTATED,
                encode_jpeg(annotated),
                meta | {"kind": EvidenceKind.FRAME_ANNOTATED},
            )
        except Exception:
            # Без кадра тревога всё равно важнее: событие уже отправлено.
            logger.exception("Не удалось отправить кадр-доказательство для %s", ep.event_id)
