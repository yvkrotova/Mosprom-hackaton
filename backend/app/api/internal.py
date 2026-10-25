"""Внутренний API для воркера конвейера. Все запросы — с заголовком X-Internal-Token."""

import secrets
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, File, Form, Header, Path, Response, UploadFile, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from app.api.deps import DbSession, HubDep, LiveDep, SettingsDep, WorkersDep
from app.errors import AppError, UnauthorizedError, UnprocessableError
from app.models import Rule, Zone
from app.schemas.common import UuidStr
from app.schemas.enums import UpsertOutcome
from app.schemas.events import EvidenceOut
from app.schemas.internal import (
    EventUpsert,
    EventUpsertResult,
    EvidenceMeta,
    HeartbeatIn,
    PipelineSettings,
    SourceConfig,
)
from app.schemas.live import LiveFrameMeta
from app.schemas.rules import RuleOut
from app.schemas.sources import SourceOut, ZoneOut
from app.services import event_service, evidence_service, replay_service, source_service


def require_internal_token(
    settings: SettingsDep,
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    if not x_internal_token or not secrets.compare_digest(
        x_internal_token, settings.internal_token
    ):
        raise UnauthorizedError("Неверный или отсутствующий X-Internal-Token")


router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal_token)]
)

EventId = Annotated[UuidStr, Path(description="UUID эпизода, его генерирует конвейер")]
MAX_LIVE_FRAME_BYTES = 4 * 1024 * 1024

M = TypeVar("M", bound=BaseModel)


def _parse_metadata(model: type[M], raw: str) -> M:
    """JSON-поле metadata multipart-запроса -> модель или 422 в едином формате."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise UnprocessableError(
            "Поле metadata не прошло проверку",
            errors=[
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
            ],
        ) from exc


@router.put("/events/{event_id}", response_model=EventUpsertResult)
def upsert_event(
    event_id: EventId, payload: EventUpsert, response: Response, db: DbSession, hub: HubDep
) -> EventUpsertResult:
    outcome, _ = event_service.upsert_event(db, hub, event_id, payload)  # deleted — отклонено
    if outcome == UpsertOutcome.CREATED:
        response.status_code = status.HTTP_201_CREATED
    return EventUpsertResult(event_id=event_id, outcome=outcome)


@router.post("/events/{event_id}/evidence", response_model=EvidenceOut)
def upload_evidence(
    event_id: EventId,
    response: Response,
    db: DbSession,
    hub: HubDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="JPEG/PNG кадр или MP4 клип")],
    metadata: Annotated[str, Form(description="JSON по схеме EvidenceMeta")],
) -> EvidenceOut:
    meta = _parse_metadata(EvidenceMeta, metadata)
    event = event_service.get_event(db, event_id)
    content = file.file.read()
    evidence, created = evidence_service.save_evidence(
        db, settings, hub, event, meta, content, file.filename, file.content_type
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
    return EvidenceOut.model_validate(evidence)


@router.post(
    "/live/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Живой кадр с треками для просмотра (последний кадр, без истории)",
)
def live_frame(
    source_id: UuidStr,
    db: DbSession,
    live: LiveDep,
    metadata: Annotated[str, Form(description="JSON по схеме LiveFrameMeta")],
    file: Annotated[UploadFile | None, File(description="JPEG кадра")] = None,
) -> Response:
    meta = _parse_metadata(LiveFrameMeta, metadata)
    source_service.get_source(db, source_id)
    jpeg = None
    if file is not None:
        jpeg = file.file.read(MAX_LIVE_FRAME_BYTES + 1)
        if len(jpeg) > MAX_LIVE_FRAME_BYTES:
            raise AppError("Живой кадр слишком большой", code="FILE_TOO_LARGE", status_code=413)
    live.publish(source_id, meta.model_dump(mode="json"), jpeg)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/heartbeat", response_model=SourceOut)
def heartbeat(payload: HeartbeatIn, db: DbSession, hub: HubDep, workers: WorkersDep) -> SourceOut:
    stopping = workers.is_stopping(payload.source_id)
    return SourceOut.model_validate(
        source_service.apply_heartbeat(db, hub, payload, stopping=stopping)
    )


@router.get("/sources/{source_id}/config", response_model=SourceConfig)
def source_config(source_id: str, db: DbSession, settings: SettingsDep) -> SourceConfig:
    source = source_service.get_source(db, source_id)
    zones = db.scalars(select(Zone).where(Zone.source_id == source.id).order_by(Zone.created_at))
    rules = db.scalars(
        select(Rule)
        .where(Rule.source_id == source.id, Rule.enabled.is_(True))
        .order_by(Rule.created_at)
    )
    replay, note = replay_service.resolve(db, settings, source)
    return SourceConfig(
        replay=replay,
        replay_note=note,
        source=SourceOut.model_validate(source),
        zones=[ZoneOut.model_validate(z) for z in zones],
        rules=[RuleOut.model_validate(r) for r in rules],
        settings=PipelineSettings(
            episode_update_interval_s=settings.episode_update_interval_s,
            heartbeat_interval_s=settings.heartbeat_interval_s,
        ),
    )
