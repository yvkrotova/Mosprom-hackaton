"""Демо-данные: источник по видеофайлу, зона-полигон и правило danger_zone.

    python tools/seed.py путь/к/видео.mp4 [--name "Камера 3"] [--site "Цех №2"]
    python tools/seed.py --demo        # сгенерировать демо-видео под MockDetector и засеять

Пишет прямо в БД из DATABASE_URL (миграции применяются автоматически), backend может быть
запущен или нет. Повторный запуск с тем же видео не создаёт дублей.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.db import make_engine, make_session_factory, run_migrations  # noqa: E402
from app.models import Rule, Source, Zone  # noqa: E402
from app.pipeline.scenarios.danger_zone import DangerZoneParams  # noqa: E402
from app.schemas.enums import Priority, ScenarioType, SourceKind  # noqa: E402
from app.services import media  # noqa: E402
from tools.make_demo_video import DEMO_ZONE, make_demo_video  # noqa: E402


@dataclass
class SeedResult:
    source_id: str
    zone_id: str
    rule_id: str
    created: bool


def seed(
    settings: Settings,
    video: Path,
    name: str = "Камера 3 — цех штамповки",
    site: str | None = "Цех №2",
    zone: list[list[float]] | None = None,
) -> SeedResult:
    video = video.resolve()
    if media.probe_video(video) is None:
        raise SystemExit(f"Файл не читается как видео: {video}")
    if settings.auto_migrate:
        run_migrations(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        with make_session_factory(engine)() as session:
            source = session.scalars(select(Source).where(Source.uri == str(video))).first()
            if source is not None and source.zones and source.rules:
                return SeedResult(source.id, source.zones[0].id, source.rules[0].id, False)
            if source is None:
                source = Source(name=name, kind=SourceKind.FILE, uri=str(video), site=site,
                                analysis_fps=settings.default_analysis_fps)  # fmt: skip
                session.add(source)
            zone_obj = Zone(source=source, name="Зона пресса", polygon=zone or DEMO_ZONE)
            rule = Rule(
                source=source,
                zone_id=None,
                scenario_type=ScenarioType.DANGER_ZONE,
                priority=Priority.HIGH,
                params=DangerZoneParams().model_dump(mode="json"),
            )
            session.add_all([zone_obj, rule])
            session.flush()
            rule.zone_id = zone_obj.id
            session.commit()
            return SeedResult(source.id, zone_obj.id, rule.id, True)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("video", nargs="?", type=Path, help="Путь к видеофайлу")
    parser.add_argument("--demo", action="store_true", help="Сгенерировать демо-видео")
    parser.add_argument("--name", default="Камера 3 — цех штамповки")
    parser.add_argument("--site", default="Цех №2")
    args = parser.parse_args()

    settings = get_settings()
    if args.demo:
        video = args.video or settings.storage_dir / "videos" / "shift_demo.mp4"
        if not video.exists():
            print(f"Генерирую демо-видео {video} ...")
            make_demo_video(video)
    elif args.video is None:
        parser.error("укажите путь к видео или --demo")
    else:
        video = args.video

    result = seed(settings, video, args.name, args.site)
    print(("Создано" if result.created else "Уже было") + ":")
    print(f"  source_id = {result.source_id}")
    print(f"  zone_id   = {result.zone_id}")
    print(f"  rule_id   = {result.rule_id}")
    print("Запуск анализа (backend должен быть запущен):")
    print(f"  curl -X POST {settings.backend_url}/api/v1/sources/{result.source_id}/start")


if __name__ == "__main__":
    main()
