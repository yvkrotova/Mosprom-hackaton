"""Настройки из переменных окружения и файла .env (см. .env.example)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/app.db"
    storage_dir: Path = Path("./data/storage")
    # Пустой токен: backend сгенерирует случайный при старте и передаст его своим воркерам.
    internal_token: str = ""
    backend_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:5173"  # через запятую
    auto_migrate: bool = True  # применять миграции Alembic при старте backend
    # Каталог фронтенда (index.html): backend отдаёт его на http://localhost:8000/.
    frontend_dir: Path | None = BACKEND_DIR.parent / "frontend"

    detector: Literal["mock", "replay", "onnx", "linq"] = "mock"
    replay_path: str = ""
    # Сценарий заглушки: danger_zone (вход в зону) или run_fast_movement (ходьба, бег, помехи).
    mock_scenario: Literal["danger_zone", "run_fast_movement"] = "danger_zone"
    # YOLOv8 (onnx и linq): путь к модели, размер входа, пороги, классы через запятую.
    model_path: str = "./models/yolov8s.onnx"
    model_input_size: int = 640
    # Низкий порог: слабые детекции нужны ByteTrack, чтобы не терять частично закрытых людей.
    # min_confidence сценариев отсекает их уже в правилах. Старое имя CONF_THRESHOLD тоже читается.
    detector_conf_threshold: float = Field(
        0.1, validation_alias=AliasChoices("detector_conf_threshold", "conf_threshold")
    )
    iou_threshold: float = 0.45
    detect_classes: str = "person"

    # Трекер: bytetrack (по умолчанию) или iou (прежний, для сравнения).
    tracker: Literal["bytetrack", "iou"] = "bytetrack"
    track_high_thresh: float = 0.5
    track_low_thresh: float = 0.1
    track_match_thresh: float = 0.8
    track_buffer_s: float = 2.0
    track_min_hits: int = 3
    # Не чаще стольких снимков треков в секунду для живого просмотра.
    tracks_push_hz: float = 8.0
    # Анализ сцены (модель фона) для правил вроде blocked_exit: частота по video_ts и сколько
    # первых секунд видео собирать эталон пустого прохода, если он не сохранён.
    scene_analysis_hz: float = 4.0
    scene_bootstrap_s: float = 3.0
    # Видеофайл анализируется не быстрее реального времени (для демо); иначе — как можно быстрее.
    realtime_playback: bool = False
    # После загрузки видео анализ запускается сам (правила по умолчанию уже созданы).
    auto_start_on_upload: bool = True
    # Как часто воркер перечитывает конфигурацию (зоны, правила): изменения применяются на лету.
    config_reload_s: float = 2.0

    heartbeat_interval_s: float = 2.0
    heartbeat_timeout_s: float = 10.0
    episode_update_interval_s: float = 1.0
    default_analysis_fps: float = 10.0
    max_upload_mb: int = 2048

    log_level: str = "INFO"

    # LinQ: только адреса и имена, значения — исключительно в .env.
    linq_api_url: str = ""
    linq_login: str = ""
    linq_password: str = ""
    linq_program: str = ""
    linq_timeout_s: float = Field(default=60.0, gt=0, le=300)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def tracker_config(self):
        from app.pipeline.tracker import TrackerConfig

        return TrackerConfig(
            kind=self.tracker,
            high_thresh=self.track_high_thresh,
            low_thresh=self.track_low_thresh,
            match_thresh=self.track_match_thresh,
            buffer_s=self.track_buffer_s,
            min_hits=self.track_min_hits,
        )

    @property
    def detect_class_list(self) -> list[str]:
        return [c.strip() for c in self.detect_classes.split(",") if c.strip()]

    @property
    def videos_dir(self) -> Path:
        return self.storage_dir / "videos"

    @property
    def evidence_dir(self) -> Path:
        return self.storage_dir / "evidence"


@lru_cache
def get_settings() -> Settings:
    return Settings()
