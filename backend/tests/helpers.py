"""Помощники для тестов конвейера: конфигурация источника и запись событий в память."""

import uuid
from typing import Any

from app.schemas.internal import SourceConfig

T_ISO = "2026-10-23T09:00:00Z"
DEFAULT_ZONE = [[0.35, 0.45], [0.75, 0.45], [0.75, 0.95], [0.35, 0.95]]


def make_config(
    zones: list[list[list[float]]] | None = None,
    params: dict[str, Any] | None = None,
    *,
    zone_rule: bool = True,
    update_interval_s: float = 1.0,
    source_id: str | None = None,
) -> SourceConfig:
    """Источник с зонами и одним правилом danger_zone (на первую зону или на все)."""
    source_id = source_id or str(uuid.uuid4())
    zone_objs = [
        {
            "id": str(uuid.uuid4()),
            "source_id": source_id,
            "name": f"Зона {i + 1}",
            "polygon": poly,
            "created_at": T_ISO,
            "updated_at": T_ISO,
        }
        for i, poly in enumerate(zones or [DEFAULT_ZONE])
    ]
    rule = {
        "id": str(uuid.uuid4()),
        "source_id": source_id,
        "zone_id": zone_objs[0]["id"] if zone_rule else None,
        "scenario_type": "danger_zone",
        "enabled": True,
        "priority": "high",
        "params": params or {},
        "version": 3,
        "created_at": T_ISO,
        "updated_at": T_ISO,
    }
    return SourceConfig.model_validate({
        "source": {"id": source_id, "name": "Камера 1", "kind": "file", "uri": "v.mp4",
                   "site": None, "status": "running", "last_error": None,
                   "last_heartbeat_at": None, "analysis_fps": 10, "created_at": T_ISO,
                   "updated_at": T_ISO},
        "zones": zone_objs,
        "rules": [rule],
        "settings": {"episode_update_interval_s": update_interval_s, "heartbeat_interval_s": 2},
    })  # fmt: skip


class RecordingSink:
    """EpisodeSink, который всё складывает в списки."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.evidence: list[tuple[str, str, bytes, dict[str, Any]]] = []

    def send_event(self, event_id: str, payload: dict[str, Any]) -> None:
        self.events.append((event_id, payload))

    def send_evidence(self, event_id: str, kind: str, image: bytes, meta: dict[str, Any]) -> None:
        self.evidence.append((event_id, kind, image, meta))

    def episodes(self) -> list[dict[str, Any]]:
        """Финальное состояние каждого эпизода в порядке создания."""
        final: dict[str, dict[str, Any]] = {}
        for event_id, payload in self.events:
            final[event_id] = payload
        return list(final.values())

    def sends(self, event_id: str) -> list[dict[str, Any]]:
        return [p for eid, p in self.events if eid == event_id]

    def event_ids(self) -> list[str]:
        return list(dict.fromkeys(eid for eid, _ in self.events))


def summary(episodes: list[dict[str, Any]]) -> list[tuple]:
    """Сравнимое описание эпизодов (без случайных event_id и времени запуска)."""
    return [
        (e["zone_id"], e["track_id"], e["video_ts_start"], e["video_ts_end"],
         e["video_ts_detected"], e["confidence"], e["state"])
        for e in episodes
    ]  # fmt: skip
