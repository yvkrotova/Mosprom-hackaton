"""Формат предзаписанных детекций (JSONL, schema_version 2) — чтение, запись, проверка, индекс.

Модуль без зависимостей от backend (только стандартная библиотека): его использует и
ReplayDetector, и скрипт linq/run_linq_detections.py, который пишет файл в Jupyter LinQ.

Файл:
- строка 1 — заголовок {"type": "header", "schema_version": 2, ...метаданные запуска};
- далее по строке на обработанный кадр {"frame_idx", "video_ts", "inference_ms", ..., "detections"};
  пустой кадр — "detections": []; рамки — [x1, y1, x2, y2] в ПИКСЕЛЯХ исходного кадра;
  track_id в записи детектора запрещён (трекинг делает backend);
- последняя строка (если прогон завершён) — {"type": "summary", ...сводка замеров}.
Время кадра — секунды от начала видео; все длительности замеров — миллисекунды.
Подробности и пример — backend/docs/CV_CONTRACT.md.

Старый формат (v1: строки FrameDetections с рамками в долях 0..1, без заголовка) читается только
с явной пометкой «v1, без метаданных» — проверить соответствие видео для него нельзя.
"""

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
INDEX_SUFFIX = ".idx.json"
INDEX_VERSION = 1
LEGACY_LABEL = "v1, без метаданных"

FRAME_KEYS = {
    "frame_idx",
    "video_ts",
    "inference_ms",
    "preprocess_ms",
    "postprocess_ms",
    "frame_ms",
    "device_ms",
    "detections",
}
DETECTION_KEYS = {"label", "confidence", "bbox"}

# Что измеряет inference_ms, если SDK не отдаёт чистое время на чипе.
INFERENCE_MS_DEFINITION = (
    "вызов runtime LinQ с ожиданием результата (отправка тензора, вычисление, получение выхода)"
)


class ReplayFormatError(ValueError):
    """Файл записи повреждён или не соответствует формату. Текст — для оператора."""


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def stats_ms(values: list[float]) -> dict[str, float | int | None]:
    """Среднее, медиана и p95 (мс). Пустой список — None, а не ноль."""
    if not values:
        return {"count": 0, "avg": None, "p50": None, "p95": None}
    ordered = sorted(values)

    def pct(q: float) -> float:
        pos = (len(ordered) - 1) * q
        lo, hi = math.floor(pos), math.ceil(pos)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)

    return {
        "count": len(ordered),
        "avg": round(sum(ordered) / len(ordered), 3),
        "p50": round(pct(0.5), 3),
        "p95": round(pct(0.95), 3),
    }


# --- запись -----------------------------------------------------------------------------------


def header_line(meta: dict[str, Any]) -> str:
    header = {"type": "header", "schema_version": SCHEMA_VERSION, **meta}
    validate_header(header)
    return json.dumps(header, ensure_ascii=False) + "\n"


def frame_line(
    frame_idx: int,
    video_ts: float,
    detections: list[dict[str, Any]],
    *,
    inference_ms: float,
    preprocess_ms: float | None = None,
    postprocess_ms: float | None = None,
    frame_ms: float | None = None,
    device_ms: float | None = None,
) -> str:
    record = {
        "frame_idx": frame_idx,
        "video_ts": round(video_ts, 6),
        "inference_ms": round(inference_ms, 3),
        "preprocess_ms": None if preprocess_ms is None else round(preprocess_ms, 3),
        "postprocess_ms": None if postprocess_ms is None else round(postprocess_ms, 3),
        "frame_ms": None if frame_ms is None else round(frame_ms, 3),
        "device_ms": None if device_ms is None else round(device_ms, 3),
        "detections": detections,
    }
    return json.dumps(record, ensure_ascii=False) + "\n"


def summary_line(summary: dict[str, Any]) -> str:
    return json.dumps({"type": "summary", **summary}, ensure_ascii=False) + "\n"


# --- проверка ---------------------------------------------------------------------------------


def validate_header(header: dict[str, Any]) -> None:
    version = header.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ReplayFormatError(
            f"Неизвестная версия схемы записи: {version!r} (поддерживается {SCHEMA_VERSION})"
        )
    video = header.get("video")
    if not isinstance(video, dict):
        raise ReplayFormatError("В заголовке записи нет раздела video")
    for key in ("sha256", "fps", "width", "height"):
        if video.get(key) in (None, ""):
            raise ReplayFormatError(f"В заголовке записи нет video.{key}")
    if not isinstance(header.get("step"), int) or header["step"] < 1:
        raise ReplayFormatError("В заголовке записи step должен быть целым ≥ 1")
    if not header.get("detector_kind"):
        raise ReplayFormatError("В заголовке записи нет detector_kind")


def validate_frame(
    record: Any, where: str, frame_w: float | None, frame_h: float | None, *, legacy: bool = False
) -> None:
    """Проверка строки кадра. where — «файл:строка» для текста ошибки.
    legacy=True — v1: рамки в долях 0..1 и поля FrameDetections."""
    if not isinstance(record, dict):
        raise ReplayFormatError(f"{where}: строка кадра должна быть JSON-объектом")
    if not legacy:
        extra = set(record) - FRAME_KEYS
        if extra:
            raise ReplayFormatError(f"{where}: неизвестные поля кадра {sorted(extra)}")
    idx = record.get("frame_idx")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
        raise ReplayFormatError(f"{where}: frame_idx должен быть целым ≥ 0")
    if not _number(record.get("video_ts")) or record["video_ts"] < 0:
        raise ReplayFormatError(f"{where}: video_ts должен быть числом ≥ 0 (секунды)")
    if not _number(record.get("inference_ms")) or record["inference_ms"] < 0:
        raise ReplayFormatError(f"{where}: inference_ms должен быть числом ≥ 0 (миллисекунды)")
    dets = record.get("detections")
    if not isinstance(dets, list):
        raise ReplayFormatError(f"{where}: detections должен быть списком (пустой — людей нет)")
    w, h = (1.0, 1.0) if legacy else (frame_w, frame_h)
    for i, det in enumerate(dets):
        place = f"{where}: детекция {i}"
        if not isinstance(det, dict):
            raise ReplayFormatError(f"{place}: должна быть объектом")
        if "track_id" in det and (not legacy or det["track_id"] is not None):
            raise ReplayFormatError(
                f"{place}: track_id в записи детектора запрещён — треки строит backend"
            )
        if not legacy:
            extra = set(det) - DETECTION_KEYS
            if extra:
                raise ReplayFormatError(f"{place}: неизвестные поля {sorted(extra)}")
        if not isinstance(det.get("label"), str) or not det["label"]:
            raise ReplayFormatError(f"{place}: нет label")
        conf = det.get("confidence")
        if not _number(conf) or not 0 <= conf <= 1:
            raise ReplayFormatError(f"{place}: confidence должен быть числом 0..1")
        box = det.get("bbox")
        if not isinstance(box, list | tuple) or len(box) != 4 or not all(map(_number, box)):
            raise ReplayFormatError(f"{place}: bbox должен быть [x1, y1, x2, y2]")
        x1, y1, x2, y2 = box
        if x2 < x1 or y2 < y1:
            raise ReplayFormatError(f"{place}: неверная рамка {box}: x2 < x1 или y2 < y1")
        eps = 1e-6 if legacy else 0.5
        if x1 < -eps or y1 < -eps or (w and x2 > w + eps) or (h and y2 > h + eps):
            unit = "долях кадра 0..1" if legacy else f"пикселях кадра {w:g}×{h:g}"
            raise ReplayFormatError(f"{place}: рамка {box} вне кадра (координаты в {unit})")


def _load_line(text: bytes, where: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReplayFormatError(f"{where}: повреждённый JSON ({exc})") from exc


# --- индекс -----------------------------------------------------------------------------------


def index_path(path: Path) -> Path:
    return path.with_name(path.name + INDEX_SUFFIX)


def build_index(path: Path) -> dict[str, Any]:
    """Полный проход по файлу: проверка каждой строки и смещения frame_idx -> позиция строки."""
    if not path.is_file():
        raise ReplayFormatError(f"Файл записи не найден: {path}")
    offsets: dict[int, int] = {}
    header: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    legacy = False
    frame_w = frame_h = None
    legacy_kind = None
    first_line = None
    with path.open("rb") as fh:
        lineno = 0
        while True:
            offset = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            lineno += 1
            if not raw.strip():
                continue
            where = f"{path.name}:{lineno}"
            record = _load_line(raw, where)
            if first_line is None:
                first_line = lineno
                if isinstance(record, dict) and record.get("type") == "header":
                    validate_header(record)
                    header = record
                    frame_w, frame_h = record["video"]["width"], record["video"]["height"]
                    continue
                if isinstance(record, dict) and "schema_version" in record:
                    validate_header(record)  # версия есть, но не та — понятная ошибка
                legacy = True
            if summary is not None:
                raise ReplayFormatError(f"{where}: строки после сводки (summary) — файл склеен?")
            if isinstance(record, dict) and record.get("type") == "summary":
                summary = record
                continue
            if isinstance(record, dict) and record.get("type") == "header":
                raise ReplayFormatError(f"{where}: второй заголовок в файле")
            validate_frame(record, where, frame_w, frame_h, legacy=legacy)
            idx = record["frame_idx"]
            if idx in offsets:
                raise ReplayFormatError(f"{where}: повтор frame_idx {idx}")
            offsets[idx] = offset
            if legacy and frame_w is None:
                frame_w, frame_h = record.get("frame_w"), record.get("frame_h")
                legacy_kind = record.get("detector_kind")
    if first_line is None:
        raise ReplayFormatError(f"Файл записи пуст: {path}")
    if not offsets:
        raise ReplayFormatError(f"В файле записи нет ни одного кадра: {path}")
    return {
        "index_version": INDEX_VERSION,
        "legacy": legacy,
        "header": header,
        "summary": summary,
        "legacy_detector_kind": legacy_kind,
        "legacy_frame_size": [frame_w, frame_h] if legacy else None,
        "offsets": sorted(offsets.items()),
    }


def load_index(path: str | Path) -> dict[str, Any]:
    """Индекс рядом с файлом (<имя>.idx.json). Хеш файла внутри: файл изменился — индекс заново."""
    path = Path(path)
    if not path.is_file():
        raise ReplayFormatError(f"Файл записи не найден: {path}")
    digest = sha256_file(path)
    cached = index_path(path)
    try:
        data = json.loads(cached.read_text(encoding="utf-8"))
        if data.get("jsonl_sha256") == digest and data.get("index_version") == INDEX_VERSION:
            return data
    except (OSError, ValueError):
        pass
    data = build_index(path)
    data["jsonl_sha256"] = digest
    try:
        tmp = cached.with_name(cached.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, cached)
    except OSError:
        pass  # каталог только для чтения — индекс живёт в памяти
    return data


def read_header(path: str | Path) -> dict[str, Any] | None:
    """Только первая строка (для поиска записи по SHA-256 видео в REPLAY_DIR). v1 — None."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline()
        record = json.loads(first)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("type") == "header" else None
