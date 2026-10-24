"""Привязка предзаписанных детекций (replay JSONL) к источнику.

Порядок поиска записи для источника:
1. source.replay_path — задан при загрузке видео или в настройках источника;
2. REPLAY_DIR — файл *.jsonl, в заголовке которого SHA-256 совпадает с SHA-256 видео источника;
3. REPLAY_PATH — запасной вариант «один файл на всё приложение» (с предупреждением в логе).
Источник без записи анализируется детектором DETECTOR; при DETECTOR=replay это ошибка источника.
"""

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Source
from app.pipeline.detectors.replay_format import read_header, sha256_file
from app.schemas.enums import SourceKind
from app.schemas.internal import ReplayBinding

logger = logging.getLogger(__name__)


def _fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def video_sha256(session: Session, source: Source) -> str | None:
    """SHA-256 видеофайла источника, посчитанный один раз (пересчёт — если файл изменился)."""
    if source.kind != SourceKind.FILE:
        return None
    path = Path(source.uri)
    if not path.is_file():
        return None
    fingerprint = _fingerprint(path)
    if source.video_sha256 and source.video_fingerprint == fingerprint:
        return source.video_sha256
    source.video_sha256 = sha256_file(path)
    source.video_fingerprint = fingerprint
    session.commit()
    return source.video_sha256


def remember_sha256(source: Source, path: Path, digest: str) -> None:
    """SHA-256, посчитанный при загрузке файла (без повторного чтения)."""
    source.video_sha256 = digest
    source.video_fingerprint = _fingerprint(path)


def _resolve_path(settings: Settings, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (settings.storage_dir / path)


def find_in_dir(directory: Path, sha256: str) -> Path | None:
    if not sha256 or not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.jsonl")):
        header = read_header(path)
        if header and (header.get("video") or {}).get("sha256") == sha256:
            return path
    return None


def resolve(
    session: Session, settings: Settings, source: Source
) -> tuple[ReplayBinding | None, str]:
    """(привязка, пояснение). Привязки нет — пояснение говорит, что искали."""
    sha = (
        video_sha256(session, source)
        if (source.replay_path or settings.replay_dir or settings.detector == "replay")
        else source.video_sha256
    )
    if source.replay_path:
        path = _resolve_path(settings, source.replay_path)
        return ReplayBinding(path=str(path), video_sha256=sha, origin="source"), ""
    if settings.replay_dir:
        found = find_in_dir(Path(settings.replay_dir), sha or "")
        if found is not None:
            return ReplayBinding(path=str(found), video_sha256=sha, origin="replay_dir"), ""
    if settings.detector == "replay" and settings.replay_path:
        logger.warning(
            "Источник %s: запись не привязана, используется общий REPLAY_PATH=%s — "
            "для нескольких источников задайте replay_path у каждого или REPLAY_DIR",
            source.id,
            settings.replay_path,
        )
        return (
            ReplayBinding(path=settings.replay_path, video_sha256=sha, origin="replay_path"),
            "",
        )
    searched = []
    if settings.replay_dir:
        searched.append(f"в REPLAY_DIR={settings.replay_dir} нет записи с SHA-256 видео")
    if not source.replay_path:
        searched.append("у источника не задан replay_path")
    if settings.detector == "replay" and not settings.replay_path:
        searched.append("REPLAY_PATH не задан")
    return None, "; ".join(searched)
