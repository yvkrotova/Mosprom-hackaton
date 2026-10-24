"""Воркер анализа одного источника.

Обычно его запускает backend (POST /api/v1/sources/{id}/start). Ручной запуск из каталога backend:

    python workers/run_pipeline.py --source-id <uuid> [--backend-url http://localhost:8000]
                                   [--detector mock|replay|linq] [--realtime]

Для ручного запуска у backend и воркера должен совпадать INTERNAL_TOKEN (в .env).
Остановка: Ctrl+C или SIGTERM — активные эпизоды корректно завершаются, статус stopped.
"""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # для запуска как скрипта

from app.config import get_settings  # noqa: E402
from app.pipeline.backend_client import BackendClient  # noqa: E402
from app.pipeline.detectors import DETECTOR_CHOICES  # noqa: E402
from app.pipeline.worker import run_worker  # noqa: E402


def _watch_stdin(stop_event: threading.Event) -> None:
    """Backend просит остановиться, закрывая stdin (работает и на Windows, где нет SIGTERM)."""
    try:
        while sys.stdin.read(1024):
            pass
    except (OSError, ValueError):
        pass
    stop_event.set()


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Воркер анализа видео LinQ Safety")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--backend-url", default=settings.backend_url)
    parser.add_argument("--detector", default=settings.detector, choices=DETECTOR_CHOICES)
    parser.add_argument("--realtime", action="store_true", help="Не быстрее реального времени")
    parser.add_argument("--stdin-control", action="store_true", help="Стоп по закрытию stdin")
    args = parser.parse_args()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s worker %(name)s: %(message)s",
    )
    if not settings.internal_token:
        logging.error("INTERNAL_TOKEN не задан: воркеру нужен тот же токен, что и backend")
        return 2

    stop_event = threading.Event()
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop_event.set())
    if args.stdin_control:
        threading.Thread(target=_watch_stdin, args=(stop_event,), daemon=True).start()

    client = BackendClient(args.backend_url, settings.internal_token)
    try:
        return run_worker(
            args.source_id, client, settings, args.detector, stop_event, realtime=args.realtime
        )
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
