"""EpisodeManager: из покадровых сигналов правил (RuleHit) делает эпизоды: нарушение = одно событие.

Одна машина состояний для ЛЮБОГО сценария (правила ничего не хранят между кадрами).
Ключ эпизода: (source_id, scenario_type, zone_id, track_id); track_id = None — нарушение зоны.
Все длительности — по video_ts.

                   VIOLATION                VIOLATION держится confirm_window_s
    нет эпизода ─────────────▶ кандидат ───────────────────────────────▶ эпизод active
         ▲                        │ нет VIOLATION дольше                  │ тревога + кадры;
         └────────────────────────┘ min(gap_tolerance_s, end_window_s)    │ VIOLATION/HOLD —
                                                                           │ продолжение
    эпизод ended ◀── нарушения явно нет (нет хита) дольше end_window_s ───┤
                 ◀── нет VIOLATION/HOLD дольше gap_tolerance_s (UNKNOWN, трек пропал)
                 ◀── конец видео / остановка

Начало эпизода — время первого VIOLATION кандидата. HOLD не открывает кандидата, только продолжает
идущий эпизод (гистерезис). UNKNOWN не продолжает и не прерывает. Обновления активного эпизода
уходят не чаще update_interval_s.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.models.base import utcnow
from app.pipeline.annotate import annotate_frame, encode_jpeg
from app.pipeline.interfaces import (
    BaseScenarioParams,
    Frame,
    HitLevel,
    RuleHit,
    Track,
    ZoneGeometry,
)
from app.schemas.enums import EpisodeState, EvidenceKind, Priority, ScenarioType
from app.schemas.internal import EventUpsert

logger = logging.getLogger(__name__)

EpisodeKey = tuple[str, str, str | None, int | None]
TRAIL_S = 1.5  # хвост траектории ног на кадре-доказательстве
_LEVEL_ORDER = {HitLevel.VIOLATION: 3, HitLevel.HOLD: 2, HitLevel.UNKNOWN: 1}


def _trail(track: Track | None, ts: float) -> list[tuple[float, float]] | None:
    """Точки ног трека за последние TRAIL_S секунд в долях кадра."""
    if track is None or not track.frame_w or not track.frame_h:
        return None
    return [
        (p.foot_x / track.frame_w, p.foot_y / track.frame_h)
        for p in track.history
        if ts - p.video_ts <= TRAIL_S
    ]


def shape_bbox(shape: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Ограничивающая рамка фигуры из RuleHit.extra["shapes"] (bbox или polygon) в долях кадра."""
    if shape.get("bbox"):
        return tuple(shape["bbox"])
    pts = shape.get("polygon") or []
    if len(pts) < 2:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if max(xs) <= min(xs) or max(ys) <= min(ys):
        return None
    return (min(xs), min(ys), max(xs), max(ys))


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
    zone_ids: tuple[str, ...] = ()  # зоны правила (для zone_states); пусто — весь кадр

    @property
    def end_window_s(self) -> float:
        return float(self.params.end_window_s)  # заполнено валидатором BaseScenarioParams

    @property
    def candidate_gap_s(self) -> float:
        """Кандидат забывается, если VIOLATION нет дольше этого."""
        return min(self.params.gap_tolerance_s, self.end_window_s)


@dataclass(slots=True)
class _Candidate:
    rule: RuleContext
    first_ts: float  # первый VIOLATION — начало будущего эпизода
    last_ts: float  # последний VIOLATION


@dataclass(slots=True)
class _Episode:
    event_id: str
    rule: RuleContext
    zone_id: str | None
    track_id: int | None
    video_ts_start: float
    video_ts_detected: float
    last_ts: float  # последний VIOLATION или HOLD
    confidence: float
    detected_at: datetime
    last_sent_ts: float
    dirty: bool = False
    updates_sent: int = field(default=0)
    # Данные сценария для события: последние значения RuleHit.extra["details"] и максимумы
    # числовых (speed -> speed_max). Остальные ключи extra — только для кадра-доказательства.
    details: dict[str, Any] = field(default_factory=dict)

    def absorb(self, extra: dict[str, Any] | None) -> None:
        for key, value in ((extra or {}).get("details") or {}).items():
            self.details[key] = value
            if isinstance(value, int | float):
                peak = f"{key}_max"
                self.details[peak] = max(self.details.get(peak, value), value)


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
        self._last_hits: dict[EpisodeKey, RuleHit] = {}  # сигналы последнего кадра (zone_states)
        self.episodes_confirmed = 0

    # --- время -------------------------------------------------------------------------------

    def wall(self, video_ts: float) -> datetime:
        """«Настенное» время момента видео: время запуска обработки + video_ts."""
        return self.run_started_at + timedelta(seconds=video_ts)

    # --- основной цикл ----------------------------------------------------------------------

    def _key(self, hit: RuleHit) -> EpisodeKey:
        return (self.source_id, str(hit.scenario_type), hit.zone_id, hit.track_id)

    def process(
        self,
        frame: Frame,
        hits: list[RuleHit],
        tracks: list[Track],
        all_tracks: list[Track] | None = None,
    ) -> None:
        """tracks — подтверждённые треки кадра: эпизод трека, которого нет среди них, считается
        «неизвестным» (человек пропал), а не «нарушения нет». all_tracks — для доказательства."""
        ts = frame.video_ts
        present = {t.track_id for t in tracks}

        # Один сигнал на ключ: самый сильный уровень, затем самая высокая уверенность.
        by_key: dict[EpisodeKey, RuleHit] = {}
        for hit in hits:
            key = self._key(hit)
            old = by_key.get(key)
            if old is None or (_LEVEL_ORDER[hit.level], hit.confidence) > (
                _LEVEL_ORDER[old.level],
                old.confidence,
            ):
                by_key[key] = hit
        self._last_hits = by_key

        # 1. Завершение активных эпизодов (до учёта новых сигналов, как раньше).
        for key, ep in list(self._active.items()):
            hit = by_key.get(key)
            elapsed = ts - ep.last_ts
            if elapsed > ep.rule.params.gap_tolerance_s:
                self._end(key, ep)  # ни VIOLATION, ни HOLD слишком долго (в т.ч. UNKNOWN)
                continue
            explicit_no = hit is None and (ep.track_id is None or ep.track_id in present)
            if explicit_no and elapsed > ep.rule.end_window_s:
                self._end(key, ep)

        # 2. Сигналы кадра.
        for key, hit in by_key.items():
            rule = self.rules[hit.rule_id]
            episode = self._active.get(key)
            if episode is not None:
                if hit.level in (HitLevel.VIOLATION, HitLevel.HOLD):
                    episode.last_ts = ts
                    episode.confidence = max(episode.confidence, hit.confidence)
                    episode.absorb(hit.extra)
                    episode.dirty = True
                continue
            if hit.level != HitLevel.VIOLATION:
                continue  # HOLD и UNKNOWN кандидата не открывают
            cand = self._candidates.get(key)
            if cand is None or ts - cand.last_ts > rule.candidate_gap_s:
                cand = self._candidates[key] = _Candidate(rule=rule, first_ts=ts, last_ts=ts)
            cand.last_ts = ts
            if ts - cand.first_ts >= rule.params.confirm_window_s:
                del self._candidates[key]
                self._confirm(key, cand, hit, rule, frame, all_tracks or tracks)

        # 3. Забываем кандидатов, у которых давно нет VIOLATION.
        for key, cand in list(self._candidates.items()):
            if ts - cand.last_ts > cand.rule.candidate_gap_s:
                del self._candidates[key]

        # 4. Периодические обновления активных эпизодов.
        for ep in self._active.values():
            if ep.dirty and ts - ep.last_sent_ts >= self.update_interval_s:
                self._send(ep, EpisodeState.ACTIVE)
                ep.last_sent_ts = ts
                ep.dirty = False
                ep.updates_sent += 1

    def reconfigure(self, rules: dict[str, RuleContext], zones: dict[str, ZoneGeometry]) -> None:
        """Правила поменялись на лету: эпизоды удалённых правил завершаются, у остальных —
        новые параметры (тот же эпизод продолжается)."""
        for key, ep in list(self._active.items()):
            if ep.rule.rule_id in rules:
                ep.rule = rules[ep.rule.rule_id]
            else:
                self._end(key, ep)
        for key, cand in list(self._candidates.items()):
            if cand.rule.rule_id in rules:
                cand.rule = rules[cand.rule.rule_id]
            else:
                del self._candidates[key]
        self.rules = rules
        self.zones = zones

    def finish(self) -> None:
        """Конец видео или остановка: все активные эпизоды завершаются."""
        for key, ep in list(self._active.items()):
            self._end(key, ep)
        self._candidates.clear()

    # --- для живого просмотра -------------------------------------------------------------------

    @property
    def active_count(self) -> int:
        return len(self._active)

    def alerting_track_ids(self) -> set[int]:
        """Треки, по которым сейчас идёт подтверждённый эпизод (тревога уже отправлена)."""
        return {ep.track_id for ep in self._active.values() if ep.track_id is not None}

    def candidate_track_ids(self) -> set[int]:
        """Треки с открытым, но ещё не подтверждённым кандидатом."""
        return {key[3] for key in self._candidates if key[3] is not None}

    def alerting_scenarios(self) -> dict[int, set[str]]:
        out: dict[int, set[str]] = {}
        for ep in self._active.values():
            if ep.track_id is not None:
                out.setdefault(ep.track_id, set()).add(str(ep.rule.scenario_type))
        return out

    def zone_states(self) -> list[dict[str, Any]]:
        """Состояние каждой пары (зона правила, сценарий): violation — идёт эпизод; candidate —
        открыт кандидат; unknown — правило не может оценить зону (reason); clear — чисто."""
        pairs: dict[tuple[str, str], dict[str, Any]] = {}
        for rule in self.rules.values():
            for zone_id in rule.zone_ids:
                pairs.setdefault(
                    (zone_id, str(rule.scenario_type)),
                    {
                        "zone_id": zone_id,
                        "scenario_type": str(rule.scenario_type),
                        "state": "clear",
                    },
                )
        rank = {"clear": 0, "unknown": 1, "candidate": 2, "violation": 3}

        def raise_to(zone_id, scenario, state, reason=None):
            item = pairs.get((zone_id, scenario))
            if item is not None and rank[state] > rank[item["state"]]:
                item["state"] = state
                if reason:
                    item["reason"] = reason

        for (_, scenario, zone_id, _), hit in self._last_hits.items():
            if hit.level == HitLevel.UNKNOWN and zone_id is not None:
                raise_to(zone_id, scenario, "unknown", (hit.extra or {}).get("reason"))
        for _, scenario, zone_id, _ in self._candidates:
            if zone_id is not None:
                raise_to(zone_id, scenario, "candidate")
        for _, scenario, zone_id, _ in self._active:
            if zone_id is not None:
                raise_to(zone_id, scenario, "violation")
        return list(pairs.values())

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
        ep.absorb(hit.extra)
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
            details=dict(ep.details) or None,
        )
        self.sink.send_event(ep.event_id, payload.model_dump(mode="json"))

    def _send_evidence(self, ep: _Episode, hit: RuleHit, frame: Frame, tracks: list[Track]) -> None:
        extra = hit.extra or {}
        shapes = extra.get("shapes") or []
        if hit.bbox is not None:
            bboxes = [{"label": hit.label, "confidence": round(hit.confidence, 4),
                       "bbox": list(hit.bbox), "track_id": hit.track_id}]  # fmt: skip
        else:  # нарушение зоны: рамки фигур сценария (например, препятствий)
            bboxes = [
                {"label": s.get("label") or hit.label, "confidence": round(hit.confidence, 4),
                 "bbox": list(b), "track_id": None}
                for s in shapes if (b := shape_bbox(s)) is not None
            ]  # fmt: skip
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
                f"{self.wall(frame.video_ts):%Y-%m-%d %H:%M:%S}Z | видео {frame.video_ts:.1f} с",
            ]
            violator = next((t for t in tracks if t.track_id == hit.track_id), None)
            annotated = annotate_frame(
                frame.image,
                frame.width,
                frame.height,
                # Правилу без зоны (весь кадр) полигоны на кадре не нужны.
                list(self.zones.values()) if ep.rule.zone_ids or hit.zone_id else [],
                tracks,
                hit.track_id,
                hit.zone_id,
                caption,
                violator_label=extra.get("label"),
                trail=_trail(violator, frame.video_ts) if extra.get("trail") else None,
                shapes=shapes,
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
