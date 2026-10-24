"""Детекции YOLOv8s на LinQ TPU по видео → JSONL (schema v2) для ReplayDetector backend.

Запускается в Jupyter организаторов (сервис LinQ доступен только из их сети), можно и из терминала:

    python run_linq_detections.py --video 0_te1.mp4 --model /auto/cache/<логин>/yolov8s.tpu \\
        --out dets_linq_te1.jsonl [--step 1] [--warmup 5] [--source-id <uuid>]

Подключение к сервису — только из переменных окружения (в коде и файлах проекта их нет):
LINQ_API_URL (amqp://<адрес из инструкции организаторов>:5672/), LINQ_LOGIN, LINQ_PASSWORD,
LINQ_TIMEOUT_S (необязательно). LINQ_PROGRAM берётся из --model.

Код пре- и постпроцессинга, формата записи и протокола LinQ — общий с backend, не копия:
рядом со скриптом должны лежать yolo_core.py, replay_format.py, linq_transport.py (в Jupyter
их загружают вместе со скриптом), иначе они берутся из backend/app/pipeline/detectors/.
Протокол вызова (RabbitMQ, tpu_pycloud_format, raw=False) — из examples/example.ipynb организаторов.

Никакого перехода на CPU, GPU, заглушку или случайные рамки: LinQ недоступен — ошибка.
Прогон можно прервать и продолжить той же командой: запись продолжится после последнего кадра.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
BACKEND_DETECTORS = HERE.parent / "backend" / "app" / "pipeline" / "detectors"
FSYNC_EVERY = 50  # кадров
PROGRESS_EVERY_S = 5.0


def load_shared(name: str) -> ModuleType:
    """Общий модуль backend как отдельный файл: рядом со скриптом или в backend (без пакета app)."""
    for folder in (HERE, BACKEND_DETECTORS):
        path = folder / f"{name}.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location(f"linq_shared_{name}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError(
        f"Нет {name}.py ни рядом со скриптом ({HERE}), ни в {BACKEND_DETECTORS} — "
        "загрузите его в Jupyter вместе со скриптом"
    )


yolo = load_shared("yolo_core")
fmt = load_shared("replay_format")


def _ms(ns: int) -> float:
    return ns / 1e6


# --- runtime LinQ -----------------------------------------------------------------------------


class LinqRuntime:
    """Вызов программы TPU через сервис LinQ (linq_transport.LinqTransport, протокол организаторов).
    Вход — float32 RGB NCHW 0..1 (raw=False: квантование на стороне сервиса), выход float32
    [1, 84, 8400]. inference_ms — «вызов runtime с ожиданием результата»; device_ms — поле
    inference_time из ответа воркера LinQ, если оно есть."""

    name = "LinQ TPU (удалённый сервис, RabbitMQ)"

    def __init__(self, program: str, timeout_s: float) -> None:
        missing = [
            k for k in ("LINQ_API_URL", "LINQ_LOGIN", "LINQ_PASSWORD") if not os.environ.get(k)
        ]
        if missing:
            raise RuntimeError(
                f"Не заданы переменные окружения {', '.join(missing)} — адрес и учётные данные "
                "сервиса LinQ из инструкции организаторов (в файлы их не записывать)"
            )
        transport = load_shared("linq_transport")
        settings = SimpleNamespace(
            linq_api_url=os.environ["LINQ_API_URL"],
            linq_login=os.environ["LINQ_LOGIN"],
            linq_password=os.environ["LINQ_PASSWORD"],
            linq_program=program,
            linq_timeout_s=timeout_s,
        )
        self.session = transport.LinqTransport(settings)
        self.device_ms: float | None = None

    def run(self, tensor: np.ndarray) -> np.ndarray:
        out = self.session.run(tensor)
        self.device_ms = self.session.device_ms
        return out

    def describe(self) -> dict[str, Any]:
        versions = {}
        for module in ("pika", "tpu_pycloud_format"):
            try:
                versions[module] = getattr(
                    __import__(module), "__version__", "версия не сообщается"
                )
            except ImportError:
                versions[module] = None
        return {
            "name": self.name,
            "protocol": "RabbitMQ, exchange tpu.direct, tpu_pycloud_format, raw=False",
            "packages": versions,
            "device": None,  # SDK не сообщает модель чипа — не выдумываем
            "device_ms_definition": "inference_time из ответа воркера LinQ (если есть), мс",
        }

    def close(self) -> None:
        self.session.close()


# --- прогон -----------------------------------------------------------------------------------


def video_info(path: Path) -> dict[str, Any]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Видео не открывается: {path}")
    try:
        return {
            "file_name": path.name,
            "sha256": fmt.sha256_file(path),
            "fps": cap.get(cv2.CAP_PROP_FPS),
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "frame_count_declared": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or None,
        }
    finally:
        cap.release()


def resume_point(out: Path, header: dict[str, Any]) -> int | None:
    """Номер последнего записанного кадра, если файл — незавершённый прогон того же видео с теми же
    настройками. Последняя неполная строка (прерванная запись) отрезается. Иначе — ошибка."""
    raw = out.read_bytes()
    if raw and not raw.endswith(b"\n"):
        cut = raw.rfind(b"\n") + 1
        print(f"Отрезана неполная последняя строка ({len(raw) - cut} байт) — прерванная запись")
        with out.open("r+b") as fh:
            fh.truncate(cut)
    try:
        index = fmt.build_index(out)
    except fmt.ReplayFormatError as exc:
        raise RuntimeError(
            f"{out} повреждён ({exc}) — продолжить нельзя; удалите файл или задайте другой --out"
        ) from exc
    old = index["header"]
    if index["legacy"] or old is None:
        raise RuntimeError(f"{out}: старый формат без заголовка — задайте другой --out")
    if index["summary"] is not None:
        raise RuntimeError(f"{out}: прогон уже завершён (есть сводка) — задайте другой --out")
    for key in ("video", "step", "model", "preprocess", "postprocess"):
        a, b = old.get(key), header.get(key)
        if key == "video":
            a = {k: v for k, v in (a or {}).items() if k != "file_name"}
            b = {k: v for k, v in (b or {}).items() if k != "file_name"}
        if a != b:
            raise RuntimeError(f"{out}: другой прогон ({key} не совпадает) — задайте другой --out")
    return max(int(k) for k, _ in index["offsets"])


def summarize(out: Path, warmup: int, started: float, frames_read: int) -> dict[str, Any]:
    inference, frame, device = [], [], []
    with out.open(encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if record.get("type"):
                continue
            inference.append(record["inference_ms"])
            if record.get("frame_ms") is not None:
                frame.append(record["frame_ms"])
            if record.get("device_ms") is not None:
                device.append(record["device_ms"])
    return {
        "frames_written": len(inference),
        "frame_count_read": frames_read,
        "warmup_runs_excluded": warmup,
        "inference_ms": fmt.stats_ms(inference),
        "frame_ms": fmt.stats_ms(frame),
        "device_ms": fmt.stats_ms(device),
        "duration_s": round(time.monotonic() - started, 2),
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def run(
    video: Path,
    out: Path,
    runtime,
    *,
    model: dict[str, Any],
    detector_kind: str = "linq_tpu",
    step: int = 1,
    warmup: int = 5,
    input_size: int = 640,
    conf: float = 0.1,
    iou: float = 0.45,
    classes: tuple[str, ...] = ("person",),
    source_id: str | None = None,
    log=print,
) -> dict[str, Any]:
    """Прогон видео через runtime (объект с run(tensor) -> выход сети, device_ms, describe()).
    Возвращает сводку. Пишет каждую строку с flush, fsync — раз в FSYNC_EVERY кадров."""
    names = yolo.COCO_NAMES
    class_ids = yolo.resolve_class_ids(classes, names)
    header_meta = {
        "video": video_info(video),
        "step": step,
        "model": model,
        "preprocess": {
            "input_size": input_size,
            "letterbox": True,
            "pad_value": 114,
            "color": "RGB",
            "layout": "NCHW float32 0..1",
        },
        "postprocess": {
            "conf_threshold": conf,
            "iou_threshold": iou,
            "nms": "по классам",
            "classes": list(classes),
            "max_det": 300,
            "bbox": "пиксели исходного кадра [x1, y1, x2, y2]",
        },
        "runtime": runtime.describe(),
        "detector_kind": detector_kind,
        "source_id": source_id,
        "started_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "inference_ms_definition": fmt.INFERENCE_MS_DEFINITION,
        "frame_ms_definition": "чтение кадра + preprocess + runtime + postprocess, мс",
    }
    last_done = None
    if out.exists() and out.stat().st_size:
        last_done = resume_point(out, {"type": "header", **header_meta})
        log(f"Продолжение {out.name}: записано до кадра {last_done}")
    else:
        out.write_text(fmt.header_line(header_meta), encoding="utf-8")

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"Видео не открывается: {video}")
    fps = header_meta["video"]["fps"] or 0
    total = header_meta["video"]["frame_count_declared"]
    started = time.monotonic()
    frames_read = written = 0
    warmed = False
    next_progress = started + PROGRESS_EVERY_S
    try:
        with out.open("a", encoding="utf-8") as fh:
            idx = -1
            while True:
                t_read = time.perf_counter_ns()
                if not cap.grab():
                    break
                idx += 1
                frames_read += 1
                if idx % step or (last_done is not None and idx <= last_done):
                    continue
                ok, image = cap.retrieve()
                if not ok or image is None:
                    raise RuntimeError(f"Кадр {idx} не декодируется — запись остановлена")
                t0 = time.perf_counter_ns()
                tensor, info = yolo.letterbox(image, input_size)
                if not warmed:
                    for _ in range(warmup):  # прогрев: в запись и статистику не идёт
                        runtime.run(tensor)
                    warmed = True
                t1 = time.perf_counter_ns()
                raw = runtime.run(tensor)  # синхронно: ждём результат
                t2 = time.perf_counter_ns()
                dets = yolo.decode(
                    yolo.to_float_output(raw, None),
                    info,
                    conf_threshold=conf,
                    iou_threshold=iou,
                    class_ids=class_ids,
                )
                t3 = time.perf_counter_ns()
                detections = [
                    {
                        "label": names.get(c, str(c)),
                        "confidence": round(score, 4),
                        "bbox": [round(v, 2) for v in box],
                    }
                    for c, score, box in dets
                ]
                fh.write(
                    fmt.frame_line(
                        idx,
                        idx / fps if fps else 0.0,
                        detections,
                        inference_ms=_ms(t2 - t1),
                        preprocess_ms=_ms(t1 - t0),
                        postprocess_ms=_ms(t3 - t2),
                        frame_ms=_ms(t3 - t_read),
                        device_ms=runtime.device_ms,
                    )
                )
                fh.flush()
                written += 1
                if written % FSYNC_EVERY == 0:
                    os.fsync(fh.fileno())
                if time.monotonic() >= next_progress:
                    next_progress += PROGRESS_EVERY_S
                    log(f"кадр {idx}{f' из {total}' if total else ''}, записано {written}")
            fh.flush()
            os.fsync(fh.fileno())
        summary = summarize(out, warmup, started, frames_read)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(fmt.summary_line(summary))
            fh.flush()
            os.fsync(fh.fileno())
        return summary
    finally:
        cap.release()


def model_meta(program: str) -> dict[str, Any]:
    path = Path(program)
    return {
        "name": path.name,
        "path": program,
        "sha256": fmt.sha256_file(path) if path.is_file() else None,
        "version": f"yolov8s-linq:{path.name}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Программа TPU: /auto/cache/<логин>/*.tpu")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--step", type=int, default=1, help="Каждый k-й кадр (по умолчанию все)")
    parser.add_argument(
        "--warmup", type=int, default=5, help="Прогревочных вызовов (не в статистике)"
    )
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--conf", type=float, default=0.1)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--classes", default="person")
    parser.add_argument("--input-size", type=int, default=640)
    args = parser.parse_args()
    if args.step < 1:
        parser.error("--step ≥ 1")
    runtime = LinqRuntime(args.model, float(os.environ.get("LINQ_TIMEOUT_S", "60")))
    try:
        summary = run(
            args.video,
            args.out,
            runtime,
            model=model_meta(args.model),
            step=args.step,
            warmup=args.warmup,
            input_size=args.input_size,
            conf=args.conf,
            iou=args.iou,
            classes=tuple(c.strip() for c in args.classes.split(",") if c.strip()),
            source_id=args.source_id,
        )
    finally:
        runtime.close()
    inf = summary["inference_ms"]
    print(
        f"Готово: {args.out} — кадров {summary['frames_written']}, вызов runtime avg "
        f"{inf['avg']} / p50 {inf['p50']} / p95 {inf['p95']} мс, прогон {summary['duration_s']} с"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
