"""Запуск и остановка воркеров анализа (по одному процессу на источник).

Остановка: закрываем stdin воркера (он завершает эпизоды и шлёт status=stopped); на Linux
дополнительно SIGTERM. Если воркер не вышел за STOP_TIMEOUT_S — kill. Когда процесс завершился
по любой причине, backend закрывает его незавершённые эпизоды, а при аварийном выходе
ставит источнику статус error с хвостом лога.
"""

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import BACKEND_DIR, Settings
from app.errors import ConflictError, UnprocessableError
from app.models import Source
from app.models.base import utcnow
from app.schemas.enums import SourceKind, SourceStatus
from app.services import event_service, rule_service
from app.services.live_hub import LiveHub
from app.services.notifier import WsHub
from app.services.source_service import ACTIVE_STATUSES, set_source_status

logger = logging.getLogger(__name__)

STOP_TIMEOUT_S = 15.0
LOG_TAIL_CHARS = 1500


@dataclass
class _Worker:
    process: subprocess.Popen
    log_path: Path
    stop_requested: bool = False
    exited: threading.Event = field(default_factory=threading.Event)


class WorkerManager:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        hub: WsHub,
        live_hub: LiveHub | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.hub = hub
        self.live_hub = live_hub
        self._workers: dict[str, _Worker] = {}
        self._lock = threading.Lock()

    # --- состояние ----------------------------------------------------------------------------

    def is_running(self, source_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(source_id)
            return worker is not None and worker.process.poll() is None

    def is_stopping(self, source_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(source_id)
            return worker is not None and worker.stop_requested

    def wait(self, source_id: str, timeout: float) -> bool:
        """Ждёт завершения воркера (и обработки его выхода). Для тестов и shutdown."""
        with self._lock:
            worker = self._workers.get(source_id)
        return True if worker is None else worker.exited.wait(timeout)

    # --- start / stop -------------------------------------------------------------------------

    def _command(self, source_id: str, realtime: bool) -> list[str]:
        cmd = [
            sys.executable, "-m", "workers.run_pipeline",
            "--source-id", source_id,
            "--backend-url", self.settings.backend_url,
            "--detector", self.settings.detector,
            "--stdin-control",
        ]  # fmt: skip
        if realtime:
            cmd.append("--realtime")
        return cmd

    def _env(self) -> dict[str, str]:
        # Воркер получает ровно те же настройки, что backend (детектор, replay, LinQ, трекер),
        # а не перечитывает .env сам: иначе backend и воркер могут разойтись.
        env = os.environ.copy()
        for name, value in self.settings.model_dump().items():
            if value is not None:
                env[name.upper()] = str(value)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        return env

    def start(self, session: Session, source: Source, *, realtime: bool | None = None) -> None:
        """realtime=None — по настройке REALTIME_PLAYBACK."""
        if self.is_running(source.id):
            raise ConflictError(
                "Анализ источника уже запущен", code="SOURCE_ALREADY_RUNNING", source_id=source.id
            )
        if source.kind == SourceKind.IMAGES:
            raise UnprocessableError(
                "Источники kind=images пока не поддерживаются",
                code="UNSUPPORTED_SOURCE_KIND",
                kind=source.kind,
            )
        # Правила по умолчанию (бег) — и для старых источников, и для созданных seed'ом.
        rule_service.ensure_default_rules(session, source.id)

        log_dir = self.settings.storage_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{source.id}.log"
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(
                self._command(
                    source.id, self.settings.realtime_playback if realtime is None else realtime
                ),
                cwd=BACKEND_DIR,
                env=self._env(),
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        worker = _Worker(process=process, log_path=log_path)
        with self._lock:
            self._workers[source.id] = worker
        logger.info("Воркер источника %s запущен, pid=%s, лог %s", source.id, process.pid, log_path)

        # Отсчёт таймаута heartbeat — с момента запуска; к запуску относятся замеры /metrics.
        source.last_heartbeat_at = utcnow()
        source.run_started_at = source.last_heartbeat_at
        set_source_status(session, self.hub, source, SourceStatus.RUNNING, None)
        threading.Thread(
            target=self._monitor, args=(source.id, worker), name=f"monitor-{source.id[:8]}",
            daemon=True,
        ).start()  # fmt: skip

    def stop(self, session: Session, source: Source) -> None:
        with self._lock:
            worker = self._workers.get(source.id)
        if worker is not None and worker.process.poll() is None:
            worker.stop_requested = True
            self._signal_stop(worker)
            threading.Timer(STOP_TIMEOUT_S, self._kill_if_alive, args=(worker,)).start()
            set_source_status(session, self.hub, source, SourceStatus.STOPPED, None)
        elif source.status in ACTIVE_STATUSES:
            # Процесса нет (backend перезапускался или воркер запущен вручную) — фиксируем стоп.
            set_source_status(session, self.hub, source, SourceStatus.STOPPED, None)

    @staticmethod
    def _signal_stop(worker: _Worker) -> None:
        try:
            if worker.process.stdin:
                worker.process.stdin.close()  # воркер видит EOF и завершается штатно
            if os.name != "nt":
                worker.process.terminate()  # SIGTERM; на Windows это был бы жёсткий kill
        except OSError:
            pass

    @staticmethod
    def _kill_if_alive(worker: _Worker) -> None:
        if worker.process.poll() is None:
            logger.warning("Воркер pid=%s не остановился за %s с — kill", worker.process.pid,
                           STOP_TIMEOUT_S)  # fmt: skip
            worker.process.kill()

    # --- завершение процесса ------------------------------------------------------------------

    def _log_tail(self, worker: _Worker) -> str:
        try:
            text = worker.log_path.read_bytes()[-LOG_TAIL_CHARS:].decode("utf-8", "replace")
        except OSError:
            return ""
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines[-5:])

    def _monitor(self, source_id: str, worker: _Worker) -> None:
        code = worker.process.wait()
        try:
            self._on_exit(source_id, worker, code)
        except Exception:
            logger.exception("Ошибка обработки выхода воркера %s", source_id)
        finally:
            with self._lock:
                if self._workers.get(source_id) is worker:
                    del self._workers[source_id]
            worker.exited.set()

    def _on_exit(self, source_id: str, worker: _Worker, code: int) -> None:
        logger.info("Воркер источника %s завершился с кодом %s", source_id, code)
        if self.live_hub is not None:
            self.live_hub.end(source_id)
        with self.session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                return
            if worker.stop_requested or code == 0:
                if source.status in ACTIVE_STATUSES:
                    set_source_status(session, self.hub, source, SourceStatus.STOPPED, None)
            elif source.status != SourceStatus.ERROR:
                # Воркер упал, не успев сообщить ошибку сам (например, сбой импорта или kill).
                tail = self._log_tail(worker)
                message = f"Воркер завершился с кодом {code}" + (f":\n{tail}" if tail else "")
                set_source_status(session, self.hub, source, SourceStatus.ERROR, message)
            closed = event_service.end_active_events(session, self.hub, source_id)
            if closed:
                logger.warning("Закрыто незавершённых эпизодов источника %s: %s", source_id,
                               closed)  # fmt: skip

    # --- жизненный цикл backend ---------------------------------------------------------------

    def recover(self) -> None:
        """При старте backend: running без живого процесса -> stopped, их эпизоды закрываются."""
        with self.session_factory() as session:
            stale = session.scalars(
                select(Source).where(Source.status.in_([s.value for s in ACTIVE_STATUSES]))
            ).all()
            for source in stale:
                if not self.is_running(source.id):
                    set_source_status(session, self.hub, source, SourceStatus.STOPPED,
                                      "Backend перезапущен — анализ остановлен")  # fmt: skip
                    event_service.end_active_events(session, self.hub, source.id)

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            workers = list(self._workers.items())
        for _, worker in workers:
            worker.stop_requested = True
            self._signal_stop(worker)
        for source_id, worker in workers:
            if not worker.exited.wait(timeout):
                self._kill_if_alive(worker)
                worker.exited.wait(5)
            logger.info("Воркер %s остановлен при выключении backend", source_id)
