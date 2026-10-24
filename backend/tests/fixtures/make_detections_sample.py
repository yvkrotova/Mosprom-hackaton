"""Пересоздаёт tests/fixtures/detections_sample.jsonl из MockDetector.

Файл — пример формата FrameDetections для CV-участника и эталон для теста ReplayDetector.
Запускать только при изменении сценария MockDetector:

    python -m tests.fixtures.make_detections_sample
"""

from pathlib import Path

from app.pipeline.detectors.mock import DURATION_S, MockDetector
from app.pipeline.sources import NullFrameSource

SAMPLE_PATH = Path(__file__).with_name("detections_sample.jsonl")
SAMPLE_ZONE = [[0.35, 0.45], [0.75, 0.45], [0.75, 0.95], [0.35, 0.95]]
SAMPLE_FPS = 10.0
SAMPLE_SIZE = (1280, 720)


def make_detections_sample(path: Path = SAMPLE_PATH) -> Path:
    detector = MockDetector(SAMPLE_ZONE)
    frames = NullFrameSource("sample", DURATION_S + 1, SAMPLE_FPS, *SAMPLE_SIZE)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for frame in frames:
            record = detector.infer(frame)
            # source_id в файле не нужен: конвейер подставит свой.
            skip = {"source_id", "preprocess_ms", "postprocess_ms", "detector_kind", "device_ms"}
            out.write(record.model_dump_json(exclude=skip) + "\n")
    return path


if __name__ == "__main__":
    print(make_detections_sample())
