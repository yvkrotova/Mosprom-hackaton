"""Источники: CRUD, загрузка видео, статус и heartbeat воркера."""

import logging
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ConflictError, NotFoundError, UnprocessableError
from app.models import ProcessingStat, Source
from app.models.base import as_utc, new_id, utcnow
from app.schemas.enums import SourceKind, SourceStatus, WsMessageType
from app.schemas.internal import HeartbeatIn
from app.schemas.sources import SourceCreate, SourceUpdate
from app.schemas.ws import SourceStatusData
from app.services import media
from app.services.notifier import WsHub

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
_CHUNK = 1024 * 1024
# Пока воркер может быть жив, источник нельзя удалять и менять ему uri.
ACTIVE_STATUSES = {SourceStatus.RUNNING, SourceStatus.DISCONNECTED}


def get_source(session: Session, source_id: str) -> Source:
    source = session.get(Source, source_id)
    if source is None:
        raise NotFoundError("Источник не найден", code="SOURCE_NOT_FOUND", source_id=source_id)
    return source


def list_sources(session: Session) -> list[Source]:
    return list(session.scalars(select(Source).order_by(Source.created_at)))


def _ensure_not_running(source: Source, message: str) -> None:
    if source.status in ACTIVE_STATUSES:
        raise ConflictError(message, code="SOURCE_RUNNING", source_id=source.id)


def create_source(session: Session, settings: Settings, body: SourceCreate) -> Source:
    source = Source(
        name=body.name,
        kind=body.kind,
        uri=body.uri,
        site=body.site,
        analysis_fps=body.analysis_fps or settings.default_analysis_fps,
    )
    session.add(source)
    session.commit()
    return source


def update_source(session: Session, source_id: str, body: SourceUpdate) -> Source:
    source = get_source(session, source_id)
    changes = body.model_dump(exclude_unset=True)
    if "uri" in changes and changes["uri"] != source.uri:
        _ensure_not_running(source, "Нельзя менять uri запущенного источника — сначала stop")
    for key, value in changes.items():
        if value is None and key in ("name", "uri", "analysis_fps"):
            continue  # обязательные поля null не принимают
        setattr(source, key, value)
    session.commit()
    return source


def _uploaded_file(settings: Settings, source: Source) -> Path | None:
    """Путь к видео, если его загружали через /sources/upload (лежит в STORAGE_DIR/videos)."""
    if source.kind != SourceKind.FILE:
        return None
    path = Path(source.uri).resolve()
    return path if path.parent == settings.videos_dir.resolve() else None


def delete_source(session: Session, settings: Settings, source_id: str) -> None:
    source = get_source(session, source_id)
    _ensure_not_running(source, "Нельзя удалить запущенный источник — сначала stop")
    uploaded = _uploaded_file(settings, source)
    session.delete(source)  # зоны, правила и замеры удаляются каскадом; журнал остаётся
    session.commit()
    if uploaded is not None:
        uploaded.unlink(missing_ok=True)


def upload_video(
    session: Session,
    settings: Settings,
    stream: BinaryIO,
    filename: str | None,
    name: str,
    site: str | None,
    analysis_fps: float | None,
) -> Source:
    ext = Path(filename or "").suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        raise AppError(
            f"Поддерживаются видео: {', '.join(sorted(VIDEO_EXTENSIONS))}",
            code="UNSUPPORTED_MEDIA_TYPE",
            status_code=415,
            filename=filename,
        )

    source_id = new_id()
    target = settings.videos_dir / f"{source_id}{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    try:
        with target.open("wb") as out:
            while chunk := stream.read(_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise AppError(
                        f"Видео больше {settings.max_upload_mb} МБ",
                        code="FILE_TOO_LARGE",
                        status_code=413,
                    )
                out.write(chunk)
        info = media.probe_video(target)
        if info is None:
            raise UnprocessableError(
                "Файл не читается как видео", code="INVALID_VIDEO", filename=filename
            )
    except Exception:
        target.unlink(missing_ok=True)
        raise

    logger.info("Загружено видео %s: %s", target.name, info)
    source = Source(
        id=source_id,
        name=name,
        kind=SourceKind.FILE,
        uri=str(target.resolve()),
        site=site,
        analysis_fps=analysis_fps or settings.default_analysis_fps,
    )
    session.add(source)
    session.commit()
    return source


def preview(session: Session, source_id: str) -> bytes:
    source = get_source(session, source_id)
    jpeg = media.preview_jpeg(source.kind, source.uri)
    if jpeg is None:
        raise UnprocessableError(
            "Не удалось получить кадр источника", code="PREVIEW_UNAVAILABLE", source_id=source_id
        )
    return jpeg


def set_source_status(
    session: Session,
    hub: WsHub,
    source: Source,
    status: SourceStatus,
    last_error: str | None,
) -> None:
    """Сохраняет статус; при смене статуса шлёт source.status в WS."""
    prev = SourceStatus(source.status)
    source.status = status
    source.last_error = last_error
    session.commit()
    if prev != status:
        data = SourceStatusData(
            id=source.id,
            name=source.name,
            status=status,
            prev_status=prev,
            last_error=source.last_error,
            last_heartbeat_at=source.last_heartbeat_at,
        )
        hub.publish(WsMessageType.SOURCE_STATUS, data.model_dump(mode="json"))


def apply_heartbeat(
    session: Session, hub: WsHub, hb: HeartbeatIn, *, stopping: bool = False
) -> Source:
    """stopping=True — backend уже попросил воркер остановиться (см. WorkerManager)."""
    source = get_source(session, hb.source_id)
    source.last_heartbeat_at = utcnow()
    session.add(
        ProcessingStat(
            source_id=source.id,
            fps=hb.fps,
            inference_ms_avg=hb.inference_ms_avg,
            inference_ms_p95=hb.inference_ms_p95,
            frames_processed=hb.frames_processed,
            model_version=hb.model_version,
            detector_kind=hb.detector_kind,
        )
    )

    status = SourceStatus(hb.status)
    if status == SourceStatus.RUNNING and stopping:
        # Запоздавший heartbeat после нажатия «стоп» не должен снова «запускать» источник.
        status = SourceStatus.STOPPED
    last_error = None
    if status == SourceStatus.ERROR:
        last_error = hb.error or "Воркер сообщил об ошибке без описания"
    set_source_status(session, hub, source, status, last_error)
    return source


def check_heartbeats(session: Session, hub: WsHub, timeout_s: float) -> list[str]:
    """running без heartbeat дольше timeout_s -> disconnected. Возвращает id таких источников."""
    cutoff = utcnow() - timedelta(seconds=timeout_s)
    lost = []
    for source in session.scalars(select(Source).where(Source.status == SourceStatus.RUNNING)):
        last = as_utc(source.last_heartbeat_at)
        if last is None or last < cutoff:
            message = f"Нет heartbeat от воркера дольше {timeout_s:g} с"
            set_source_status(session, hub, source, SourceStatus.DISCONNECTED, message)
            lost.append(source.id)
    return lost
