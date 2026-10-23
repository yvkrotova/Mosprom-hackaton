"""Кадры-доказательства: файлы в STORAGE_DIR/evidence/{event_id}/, в БД — путь и метаданные."""

import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.models import Event, Evidence
from app.schemas.enums import WsMessageType
from app.schemas.internal import EvidenceMeta
from app.services.event_service import publish_event
from app.services.notifier import WsHub

MAX_EVIDENCE_BYTES = 50 * 1024 * 1024
_EXT_BY_CONTENT_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "video/mp4": ".mp4"}
_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".mp4"}


def _extension(filename: str | None, content_type: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in _ALLOWED_EXT:
        return ext
    if content_type in _EXT_BY_CONTENT_TYPE:
        return _EXT_BY_CONTENT_TYPE[content_type]
    raise AppError(
        "Доказательство должно быть JPEG, PNG или MP4",
        code="UNSUPPORTED_MEDIA_TYPE",
        status_code=415,
        filename=filename,
        content_type=content_type,
    )


def _find_existing(session: Session, event_id: str, meta: EvidenceMeta) -> Evidence | None:
    video_ts = (
        Evidence.video_ts.is_(None)
        if meta.video_ts is None
        else (Evidence.video_ts == meta.video_ts)
    )
    return session.scalars(
        select(Evidence).where(Evidence.event_id == event_id, Evidence.kind == meta.kind, video_ts)
    ).first()


def save_evidence(
    session: Session,
    settings: Settings,
    hub: WsHub,
    event: Event,
    meta: EvidenceMeta,
    content: bytes,
    filename: str | None,
    content_type: str | None,
) -> tuple[Evidence, bool]:
    """Сохраняет файл и запись. Повтор с теми же kind+video_ts возвращает существующую запись."""
    existing = _find_existing(session, event.id, meta)
    if existing is not None:
        return existing, False

    if len(content) > MAX_EVIDENCE_BYTES:
        raise AppError(
            "Файл доказательства слишком большой", code="FILE_TOO_LARGE", status_code=413
        )
    ext = _extension(filename, content_type)
    ts_part = "na" if meta.video_ts is None else f"{meta.video_ts:.3f}"
    name = f"{meta.kind}_{ts_part}{ext}"
    rel_path = Path("evidence") / event.id / name

    target = settings.storage_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(content)
    os.replace(tmp, target)  # атомарно: недописанный файл никогда не отдаётся по /media

    evidence = Evidence(
        event_id=event.id,
        kind=meta.kind,
        path=rel_path.as_posix(),
        url=f"/media/{rel_path.as_posix()}",
        video_ts=meta.video_ts,
        frame_time=meta.frame_time,
        bboxes=[b.model_dump(mode="json") for b in meta.bboxes],
    )
    session.add(evidence)
    session.commit()
    session.refresh(event)
    # Фронт получает событие уже с кадром (thumbnail_url).
    publish_event(session, hub, WsMessageType.EVENT_UPDATED, event)
    return evidence, True
