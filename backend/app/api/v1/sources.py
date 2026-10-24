"""Источники видео: CRUD, загрузка файла, кадр-превью."""

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from app.api.deps import DbSession, LiveDep, SettingsDep, WorkersDep
from app.errors import NotFoundError
from app.schemas.enums import SourceStatus
from app.schemas.sources import SourceCreate, SourceOut, SourceUpdate
from app.services import rule_service, source_service

router = APIRouter(tags=["sources"])
logger = logging.getLogger(__name__)


@router.get("/sources", response_model=list[SourceOut])
def list_sources(db: DbSession) -> list[SourceOut]:
    return [SourceOut.model_validate(s) for s in source_service.list_sources(db)]


@router.post("/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(body: SourceCreate, db: DbSession, settings: SettingsDep) -> SourceOut:
    source = source_service.create_source(db, settings, body)
    rule_service.ensure_default_rules(db, source.id)  # бег и др. — без ручной настройки
    return SourceOut.model_validate(source)


@router.post(
    "/sources/upload",
    response_model=SourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Загрузить видеофайл и создать источник kind=file",
)
def upload_source(
    db: DbSession,
    settings: SettingsDep,
    workers: WorkersDep,
    file: Annotated[UploadFile, File(description="Видео: mp4, avi, mov, mkv, webm")],
    name: Annotated[str, Form(min_length=1, max_length=200)],
    site: Annotated[str | None, Form(max_length=200)] = None,
    analysis_fps: Annotated[float | None, Form(gt=0, le=60)] = None,
) -> SourceOut:
    source = source_service.upload_video(
        db, settings, file.file, file.filename, name, site or None, analysis_fps
    )
    rule_service.ensure_default_rules(db, source.id)
    if settings.auto_start_on_upload:
        # Анализ сразу после загрузки. Ошибка запуска не ломает загрузку: источник создан,
        # статус error с текстом причины.
        try:
            workers.start(db, source)
        except Exception as exc:
            logger.exception("Автозапуск анализа источника %s не удался", source.id)
            source_service.set_source_status(
                db, workers.hub, source, SourceStatus.ERROR, f"Автозапуск не удался: {exc}"
            )
    return SourceOut.model_validate(source)


@router.get("/sources/{source_id}", response_model=SourceOut)
def get_source(source_id: str, db: DbSession) -> SourceOut:
    return SourceOut.model_validate(source_service.get_source(db, source_id))


@router.patch("/sources/{source_id}", response_model=SourceOut)
def update_source(source_id: str, body: SourceUpdate, db: DbSession) -> SourceOut:
    return SourceOut.model_validate(source_service.update_source(db, source_id, body))


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: str, db: DbSession, settings: SettingsDep) -> Response:
    source_service.delete_source(db, settings, source_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sources/{source_id}/start",
    response_model=SourceOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Запустить анализ (воркер в отдельном процессе)",
)
def start_source(
    source_id: str,
    db: DbSession,
    workers: WorkersDep,
    realtime: Annotated[
        bool | None,
        Query(
            description="Файл в темпе реального времени (демо); false — максимальная скорость "
            "(замеры). По умолчанию — REALTIME_PLAYBACK"
        ),
    ] = None,
) -> SourceOut:
    source = source_service.get_source(db, source_id)
    workers.start(db, source, realtime=realtime)
    return SourceOut.model_validate(source)


@router.post(
    "/sources/{source_id}/stop",
    response_model=SourceOut,
    summary="Остановить анализ; активные эпизоды завершаются",
)
def stop_source(source_id: str, db: DbSession, workers: WorkersDep) -> SourceOut:
    source = source_service.get_source(db, source_id)
    workers.stop(db, source)
    return SourceOut.model_validate(source)


@router.get(
    "/sources/{source_id}/live.jpg",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
    summary="Последний кадр, обработанный воркером (пока идёт анализ)",
)
def live_jpeg(source_id: str, db: DbSession, live: LiveDep) -> Response:
    source_service.get_source(db, source_id)
    jpeg = live.latest_jpeg(source_id)
    if jpeg is None:
        raise NotFoundError(
            "Живого кадра нет: анализ не запущен", code="LIVE_NOT_AVAILABLE", source_id=source_id
        )
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get(
    "/sources/{source_id}/preview",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
    summary="Один JPEG-кадр источника для разметки зон",
)
def preview(source_id: str, db: DbSession) -> Response:
    return Response(
        source_service.preview(db, source_id),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
