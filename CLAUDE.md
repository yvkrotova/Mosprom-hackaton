# CLAUDE.md — контекст для следующей сессии

## Проект
Хакатон «Открой Моспром 2026», кейс LinQ: видеоконтроль промбезопасности. Видео → YOLOv8s → ByteTrack →
сценарии (`danger_zone` — ноги в полигоне; `run_fast_movement` — бег, скорость в «ростах в секунду») →
EpisodeManager: ОДНО событие на эпизод → тревога в браузер по WebSocket → журнал, реакция оператора, статистика.
Обязательное требование кейса — инференс на LinQ TPU — **ещё не выполнено** (см. «Статус»).

## Структура
- `backend/` — FastAPI (Python 3.12, venv `backend/.venv`). API — код + `backend/docs/CONTRACT.md`,
  CV-контракт и трекер — `backend/docs/CV_CONTRACT.md`.
  - `app/pipeline/`: воркер на источник: `sources → detectors → tracker → scenarios → episodes → backend_client`.
    - Детекторы `detectors/{mock,mock_run,replay,onnx,linq}.py`, общий YOLO-код `yolo_common.py` (letterbox,
      postprocess, quantize/dequantize). `DETECTOR→kind`: onnx→`onnx_cpu`, linq→`linq_tpu`, replay файла
      с `detector_kind=linq_tpu` → `linq_tpu_replay`.
    - Трекер `tracker.py`: свой ByteTrack (Калман, 2 этапа + этап 1b «по расстоянию в ростах»), IoU для
      сравнения. В правила идут только `state=confirmed`. `Track.history` — точки ног в пикселях.
    - Сценарии `scenarios/{danger_zone,run_fast_movement}.py`. Хуки сценария: `track_info()`, `finished()`,
      `confirms_itself`; `RuleHit.extra` (`details` → `event.details`, `label`, `trail` → кадр-доказательство).
  - `annotate.py` — кадр-доказательство: кириллица через Pillow, маленькие кадры увеличиваются до 320 px.
  - `tools/`: seed (`--demo`, `--videos`), video_inventory, track_videos, dump_track_speeds, evaluate_episodes,
    export_detections, benchmark_detector, validate_detections; общий `tools/videos.py` (манифест, кэш детекций).
  - `data/videos/`: в git только `manifest.json`, `labels.json` (ЧЕРНОВИК), `inventory.json`. Ролики, превью,
    кэш, рендеры — не в git. Ролики: `data/videos/*.avi|mp4` и `data/storage/videos/0_te*.mp4`.
- `frontend/` — ванильный JS (ES-модули, без сборки), раздаётся backend на `/`. Экраны: `live.js` (мониторинг,
  интерполяция рамок, панель треков), `journal.js`, `territory.js` (зоны, правила по JSON Schema, правила
  «на весь кадр»), `analytics.js`, `event-modal.js` (`#…?event=<id>` открывает карточку).
- `linq/` (на Windows = `LinQ/`, в git — `linq/`): fetch_materials, check_onnx_ops (окружение
  `linq/.venv-tools` с onnx), prepare_calibration (199 кадров в `linq/calibration/`, не в git), compile.py (каркас),
  export_onnx, compare_with_onnx, benchmark_linq, `notebooks/run_linq_detections.ipynb`. `linq/extracted/` —
  PDF организаторов и **секрет VPN** — не в git.
- `docs/`: STATUS, BENCHMARKS, LINQ_SDK_NOTES, LINQ_TODO_FOR_HUMAN, VIDEO_LABELING.

## Команды
```powershell
cd backend; $env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe -m uvicorn app.main:app           # http://localhost:8000
.\.venv\Scripts\python.exe -m pytest -q                       # 167 тестов + 1 skip, ~25 с
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m ruff check ..\linq              # свои правила в linq/ruff.toml
cd ..\frontend; npm run build                                 # = check: синтаксис + импорты
```
Windows: PowerShell 5.1 портит JSON-аргументы и кириллицу. Для JSON-параметров используйте bash,
для запросов — Python/httpx.

## Соглашения
- Время в видео — `video_ts`; все длительности и скорости — по нему. Координаты — 0..1 (история треков — пиксели).
- Одно нарушение = один эпизод = одна тревога; `state` и `review_status` независимы.
- Ошибка источника/модели/сервера ≠ «нарушений нет». `detector_kind`, не начинающийся с `linq`, всегда помечен «не LinQ».
- **API LinQ и команды SDK не придумывать**: только по материалам (SDK/API пока не скачаны). Секреты — только `.env`.
- Новый эндпоинт/поле → тест + CONTRACT.md; изменение моделей БД → миграция (последняя 0003 `events.details`).
- Фронт — без React/сборщика (решение: продолжать существующий код).

## Рефакторинг сценариев (промпт «A/B/C»: правила без состояния, автобег, blocked_exit)
Этап 0 — готово: эталон `backend/docs/baseline_before_refactor.json`.
Этап A — готово: FrameContext, HitLevel (VIOLATION/HOLD/UNKNOWN), единая машина эпизодов в
EpisodeManager (end_window_s в BaseScenarioParams), track_id=None для зонных эпизодов, zone_states в
live.frame, kinematics.py, бег без состояния, shapes в annotate.py. Регрессия: danger_zone — без
изменений; бег — те же эпизоды, концы на 0.1–0.2 с раньше (нет хвоста EMA).
Этап B — готово (backend): ensure_default_rules (auto_enable, sources.defaults_applied, rules.auto_created,
миграция 0004) при POST /sources, upload, WorkerManager.start, seed; AUTO_START_ON_UPLOAD; изменения
зон/правил — горячая перезагрузка в воркере (ConfigWatcher, CONFIG_RELOAD_S, Pipeline.apply_config);
scenario_types у зоны (по умолчанию danger_zone). Дефолты бега: порог 1.7, confirm 0.6, min_track_age 0.3,
край кадра — только верх/низ. Новый ролик coils в manifest.
Этап C — готово (backend): `pipeline/scene.py` (SceneAnalyzer: один на поток, только зоны сценариев с
requires_scene_analysis, ROI 320 px, 4 Гц, авто-эталон за SCENE_BOOTSTRAP_S или сохранённый «пустой проход»,
быстрый фон, отсев «призраков», скачок яркости и сдвиг камеры → UNKNOWN), сценарий `blocked_exit`
(confirm 10 с, ≥ 5 % зоны), POST/DELETE /zones/{id}/reference, scene_ms_avg в heartbeat/метриках
(миграция 0005). Синтетика 11 тестов; реальные ролики: статичные камеры — 0 ложных, accident — FN из-за
монтажа (честно «сдвиг камеры»). Замеры — docs/BENCHMARKS.md.
Этап D — готово: фронт без списков сценариев (подписи, иконки, details_fields, zone_default — из
GET /scenarios), зоны рисуются по zone_states (кандидат мигает жёлтым, нарушение — красная заливка,
«оценить нельзя» — серый пунктир с причиной), галочки сценариев зоны → scenario_types, «Запомнить пустой
проход», значок «по умолчанию», после загрузки — сразу живой просмотр. Этап E — контракт в CONTRACT.md.
Сквозные проверки (временная БД, ONNX CPU): kth загрузка → авто-правило бега → 4 события «Бег» из 4,
9.3 кадр/с; accident + зона blocked_exit → зона применилась на лету, scene_ms 4.3, событий нет (монтаж/зум).

### Архитектурные правила (обязательны)
- Один цикл конвейера `Pipeline.run`; детектор, трекер и анализ сцены создаются один раз на поток.
- Общая математика — `geometry.py`, `kinematics.py`, `scene.py`; в сценариях её не дублировать.
- Правило — чистая функция кадра (Strategy): без состояния инцидентов; эпизоды — только `EpisodeManager`.
- Единый контракт события и единая отрисовка (`annotate.py`); фронт берёт сценарии из GET /scenarios.
- Всё время — по `video_ts`. Новый сценарий: файл в `pipeline/scenarios/` по `_template.py` + регистрация.

## Статус (2026-10-24)
Готово: ByteTrack, `run_fast_movement` (порог 1.5, подтверждён частично), живые треки во фронте, проверка
на 4 роликах (docs/BENCHMARKS.md: зона 2/3, бег 4/4, 0 FP, 1.0 тревоги на эпизод), подготовка к LinQ без SDK.

Осталось (по приоритету):
1. LinQ: человек скачивает SDK и API (docs/LINQ_TODO_FOR_HUMAN.md). Затем прочитать `tpu_framework.pdf`,
   заполнить LINQ_SDK_NOTES, `linq/compile.py`, `LinqDetector._run`, ячейку TPU в notebook; замеры — в BENCHMARKS.
2. Проверить черновую разметку (docs/VIDEO_LABELING.md), перезапустить `tools/evaluate_episodes.py`.
3. Ролик с боковой ходьбой и быстрым шагом — подтвердить порог бега.
4. В `backend/.env` пользователя стоит `CONF_THRESHOLD=0.35` (старое имя) — для ByteTrack нужно 0.1.
