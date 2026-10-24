"""Настройки из переменных окружения и файла .env (см. .env.example)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    # YOLOv8 (onnx и linq): путь к модели, размер входа, пороги, классы через запятую.
    model_path: str = "./models/yolov8s.onnx"
    model_input_size: int = 640
    conf_threshold: float = 0.35
    iou_threshold: float = 0.45
    detect_classes: str = "person"
    # Видеофайл анализируется не быстрее реального времени (для демо); иначе — как можно быстрее.
    realtime_playback: bool = False

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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

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
