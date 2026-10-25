"""Демо-данные: источник по видеофайлу, зона-полигон и правило danger_zone.

    python tools/seed.py путь/к/видео.mp4 [--name "Камера 3"] [--site "Цех №2"]
    python tools/seed.py --demo        # сгенерировать демо-видео под MockDetector и засеять
    python tools/seed.py --videos      # 4 тестовых ролика из data/videos/manifest.json
    python tools/seed.py --history [30] [--clear]   # смены 5 начальников и инциденты за N дней

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
from app.services import demo_service, media  # noqa: E402
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
                source = Source(
                    name=name,
                    kind=SourceKind.FILE,
                    uri=str(video),
                    site=site,
                    analysis_fps=settings.default_analysis_fps,
                )
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


def seed_videos(
    settings: Settings, manifest_path: Path | None = None
) -> list[tuple[str, str, bool]]:
    """Источники из data/videos/manifest.json. Правила создаются тем же путём, что в интерфейсе:
    зоны со сценариями из zones[].scenario_types (по умолчанию danger_zone) и правила по умолчанию
    (ensure_default_rules — бег на весь кадр). Без дублей. Возвращает [(id ролика, source_id,
    создан ли)]; ролики без файла пропускаются."""
    import json

    from app.config import BACKEND_DIR
    from app.services import rule_service

    manifest_path = manifest_path or BACKEND_DIR / "data" / "videos" / "manifest.json"
    videos = json.loads(manifest_path.read_text(encoding="utf-8"))["videos"]
    if settings.auto_migrate:
        run_migrations(settings.database_url)
    engine = make_engine(settings.database_url)
    out = []
    try:
        with make_session_factory(engine)() as session:
            for v in videos:
                path = (BACKEND_DIR / v["file"]).resolve()
                if not path.is_file():
                    print(f"  пропуск {v['id']}: нет файла {v['file']}")
                    continue
                source = session.scalars(select(Source).where(Source.uri == str(path))).first()
                if source is not None:
                    out.append((v["id"], source.id, False))
                    continue
                source = Source(
                    name=v["title"],
                    kind=SourceKind.FILE,
                    uri=str(path),
                    site="Тестовые видео",
                    analysis_fps=settings.default_analysis_fps,
                )
                session.add(source)
                session.commit()
                for z in v.get("zones", []):
                    zone = Zone(source_id=source.id, name=z["name"], polygon=z["polygon"])
                    session.add(zone)
                    session.commit()
                    rule_service.sync_zone_rules(
                        session, zone, z.get("scenario_types"), creating=True
                    )
                rule_service.ensure_default_rules(session, source.id)
                out.append((v["id"], source.id, True))
    finally:
        engine.dispose()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("video", nargs="?", type=Path, help="Путь к видеофайлу")
    parser.add_argument("--demo", action="store_true", help="Сгенерировать демо-видео")
    parser.add_argument(
        "--videos",
        action="store_true",
        help="4 тестовых ролика из data/videos/manifest.json с зонами и правилами",
    )
    parser.add_argument(
        "--history",
        nargs="?",
        const=30,
        type=int,
        metavar="ДНЕЙ",
        help="Демо-история для «Статистики»: 4 демо-камеры, смены 5 начальников, инциденты",
    )
    parser.add_argument("--clear", action="store_true", help="С --history: удалить демо-историю")
    parser.add_argument("--name", default="Камера 3 — цех штамповки")
    parser.add_argument("--site", default="Цех №2")
    args = parser.parse_args()

    settings = get_settings()
    if args.history is not None:
        run_migrations(settings.database_url)
        with make_session_factory(make_engine(settings.database_url))() as session:
            if args.clear:
                print("Удалено:", demo_service.clear(session))
            else:
                info = demo_service.generate(session, days=args.history)
                print(
                    f"Демо-история за {info['days']} дн.: смен {info['shifts']}, "
                    f"инцидентов {info['events']}, демо-камер {info['sources']}"
                )
                print("Начальники смен:", ", ".join(n for n, _a, _b in demo_service.SUPERVISORS))
        return
    if args.videos:
        for vid, source_id, created in seed_videos(settings):
            print(f"  {vid:12} {source_id} {'создан' if created else 'уже был'}")
        print("Зоны предложены по превью — поправьте их в «Источники и зоны», если нужно.")
        return
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
