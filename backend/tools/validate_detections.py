"""Проверка JSONL-файла детекций на соответствие CV-контракту (docs/CV_CONTRACT.md).

    python tools/validate_detections.py detections.jsonl [--max-errors 50]

Код выхода 0 — ошибок нет (предупреждения допустимы), 1 — есть ошибки.
"""

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas.cv import FrameDetections  # noqa: E402


@dataclass
class Report:
    errors: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[tuple[int, str]] = field(default_factory=list)
    frames: int = 0
    detections: int = 0
    labels: Counter = field(default_factory=Counter)
    model_versions: Counter = field(default_factory=Counter)
    inference_ms: list[float] = field(default_factory=list)
    first_ts: float | None = None
    last_ts: float | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def _format_validation(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(объект)'}: {e['msg']}" for e in exc.errors()
    )


def validate_file(path: Path) -> Report:
    report = Report()
    prev: FrameDetections | None = None
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                rec = FrameDetections.model_validate(json.loads(line))
            except json.JSONDecodeError as exc:
                report.errors.append((lineno, f"не JSON: {exc.msg}"))
                continue
            except ValidationError as exc:
                report.errors.append((lineno, _format_validation(exc)))
                continue

            if prev is not None:
                if rec.video_ts < prev.video_ts:
                    report.errors.append(
                        (lineno, f"video_ts убывает: {rec.video_ts} < {prev.video_ts}")
                    )
                elif rec.video_ts == prev.video_ts:
                    report.warnings.append((lineno, f"повтор video_ts={rec.video_ts}"))
                if rec.frame_idx <= prev.frame_idx:
                    report.warnings.append((lineno, "frame_idx не возрастает"))
                if (rec.frame_w, rec.frame_h) != (prev.frame_w, prev.frame_h):
                    report.warnings.append((lineno, "размер кадра изменился"))
            with_ids = sum(d.track_id is not None for d in rec.detections)
            if 0 < with_ids < len(rec.detections):
                report.warnings.append(
                    (lineno, "track_id есть не у всех детекций кадра — трекер backend их заменит")
                )
            if any(d.confidence > 0 and d.confidence < 0.01 for d in rec.detections):
                report.warnings.append((lineno, "очень низкий confidence — забыт порог?"))

            report.frames += 1
            report.detections += len(rec.detections)
            report.labels.update(d.label for d in rec.detections)
            report.model_versions[rec.model_version] += 1
            report.inference_ms.append(rec.inference_ms)
            report.first_ts = rec.video_ts if report.first_ts is None else report.first_ts
            report.last_ts = rec.video_ts
            prev = rec
    if report.frames == 0 and not report.errors:
        report.errors.append((0, "файл пуст"))
    if report.frames and "person" not in report.labels:
        report.warnings.append((0, 'нет ни одной детекции label="person" — сценарии её ждут'))
    return report


def print_report(path: Path, report: Report, max_errors: int) -> None:
    print(f"Файл: {path}")
    for title, items in (("ОШИБКИ", report.errors), ("Предупреждения", report.warnings)):
        if items:
            print(f"\n{title} ({len(items)}):")
            for lineno, msg in items[:max_errors]:
                print(f"  строка {lineno}: {msg}" if lineno else f"  {msg}")
            if len(items) > max_errors:
                print(f"  ... и ещё {len(items) - max_errors}")
    if report.frames:
        ms = np.array(report.inference_ms)
        print("\nСводка:")
        print(f"  кадров: {report.frames}, детекций: {report.detections} "
              f"({report.detections / report.frames:.2f} на кадр)")  # fmt: skip
        print(f"  video_ts: {report.first_ts:.3f} – {report.last_ts:.3f} с")
        print(f"  классы: {dict(report.labels.most_common())}")
        print(f"  model_version: {dict(report.model_versions)}")
        print(f"  inference_ms: среднее {ms.mean():.2f}, p95 {np.percentile(ms, 95):.2f}")
    print("\nИТОГ: " + ("OK" if report.ok else f"есть ошибки ({len(report.errors)})"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-errors", type=int, default=50)
    args = parser.parse_args()
    if not args.path.is_file():
        print(f"Файл не найден: {args.path}")
        return 1
    report = validate_file(args.path)
    print_report(args.path, report, args.max_errors)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
