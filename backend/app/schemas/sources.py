"""Источники видео (камеры, файлы) и их зоны."""

from pydantic import Field

from app.schemas.common import ApiModel, InputModel, Polygon, UtcDatetime
from app.schemas.enums import SourceKind, SourceStatus

REPLAY_PATH_DESCRIPTION = (
    "Предзаписанные детекции (JSONL schema v2) на сервере: абсолютный путь или путь от "
    "STORAGE_DIR. Задан — источник анализируется ReplayDetector'ом по этой записи."
)


class SourceCreate(InputModel):
    name: str = Field(min_length=1, max_length=200, examples=["Камера 3 — цех штамповки"])
    kind: SourceKind = SourceKind.FILE
    uri: str = Field(min_length=1, max_length=2000, description="Путь к файлу или rtsp://...")
    site: str | None = Field(None, max_length=200, examples=["Цех №2"])
    analysis_fps: float | None = Field(
        None, gt=0, le=60, description="Частота анализа кадров; по умолчанию DEFAULT_ANALYSIS_FPS"
    )
    replay_path: str | None = Field(None, max_length=2000, description=REPLAY_PATH_DESCRIPTION)


class SourceUpdate(InputModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    uri: str | None = Field(None, min_length=1, max_length=2000)
    site: str | None = Field(None, max_length=200)
    analysis_fps: float | None = Field(None, gt=0, le=60)
    replay_path: str | None = Field(
        None, max_length=2000, description=REPLAY_PATH_DESCRIPTION + " Пустая строка — отвязать."
    )


class SourceOut(ApiModel):
    id: str
    name: str
    kind: SourceKind
    uri: str
    site: str | None
    status: SourceStatus
    last_error: str | None
    last_heartbeat_at: UtcDatetime | None
    analysis_fps: float
    replay_path: str | None = Field(None, description="Привязанная запись детекций (replay)")
    video_sha256: str | None = Field(None, description="SHA-256 видеофайла (если посчитан)")
    run_started_at: UtcDatetime | None = Field(None, description="Начало последнего запуска")
    created_at: UtcDatetime
    updated_at: UtcDatetime


SCENARIO_TYPES_DESCRIPTION = (
    "Для каких сценариев нужна зона (сценарии с requires_zone из GET /scenarios): для каждого "
    "создаётся правило с параметрами по умолчанию, снятый — удаляется. При создании без поля — "
    '["danger_zone"], как раньше; [] — зона без правил. При изменении без поля — без изменений.'
)


class ZoneCreate(InputModel):
    name: str = Field(min_length=1, max_length=200, examples=["Зона пресса"])
    polygon: Polygon = Field(
        description="Точки [[x, y], ...] в долях кадра 0..1, минимум 3",
        examples=[[[0.3, 0.4], [0.7, 0.4], [0.7, 0.95], [0.3, 0.95]]],
    )
    scenario_types: list[str] | None = Field(
        None, description=SCENARIO_TYPES_DESCRIPTION, examples=[["blocked_exit"]]
    )


class ZoneUpdate(InputModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    polygon: Polygon | None = None
    scenario_types: list[str] | None = Field(None, description=SCENARIO_TYPES_DESCRIPTION)


class ZoneOut(ApiModel):
    id: str
    source_id: str
    name: str
    polygon: list[tuple[float, float]]
    scenario_types: list[str] = Field(
        default_factory=list, description="Сценарии, для которых у зоны есть правило"
    )
    reference_url: str | None = Field(
        None, description="Эталон пустого прохода (кадр, сохранённый пользователем), /media/…"
    )
    enabled: bool = Field(False, description="Зона активна: есть правила и все включены")
    created_at: UtcDatetime
    updated_at: UtcDatetime
