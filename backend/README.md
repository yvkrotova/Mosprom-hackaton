# LinQ Safety — backend

Backend веб-приложения видеоконтроля промышленной безопасности для специалиста по охране труда,
диспетчера и мастера смены. Система получает видео, нейросеть на ускорителе LinQ находит людей,
правила сценариев определяют нарушение (например, человек в опасной зоне). На каждый эпизод
нарушения система создаёт **одно** событие, отправляет тревогу с кадром-доказательством и сохраняет
событие в журнал.

Документы:
- [docs/CONTRACT.md](docs/CONTRACT.md) — API для фронтенда: эндпоинты, примеры, коды ошибок, WebSocket;
- [docs/CV_CONTRACT.md](docs/CV_CONTRACT.md) — формат детекций и как подключить модель;
- Swagger UI: http://localhost:8000/docs (когда backend запущен).

> **Секреты — только в `.env`.** Логины и пароли VPN, Jupyter и LinQ никогда не пишутся в код и
> не коммитятся. `.env` уже в `.gitignore`; в `.env.example` значения пустые.

## Что реализовано

| Требование кейса | Где |
|---|---|
| Приём данных | Загрузка видеофайла (`POST /sources/upload`), источник по пути или `rtsp://` (`POST /sources`) |
| Распознавание нарушений | Конвейер `Detector → Tracker → сценарии → EpisodeManager`; детектор выбирается через `DETECTOR=mock\|replay\|linq` |
| Оперативное оповещение | WebSocket `/ws/events`: `event.created` с кадром, камерой, зоной, временем и типом нарушения |
| Подавление повторов | Нарушение подтверждается `confirm_window_s`; потеря короче `gap_tolerance_s` не завершает эпизод; одна тревога на эпизод |
| Журнал с фильтрами | `GET /events`: тип, камера, зона, интервал времени, статус, приоритет; карточка события и действия оператора |
| Замеры скорости | `GET /metrics`: FPS, время инференса (среднее и p95), задержка обнаружения, задержка оповещения, тревог на эпизод |
| Новые сценарии без переделки | Реестр плагинов `@register_scenario`, параметры с JSON Schema (`GET /scenarios`), шаблон `_template.py` |
| Ошибки видны | Ошибка детектора или камеры → источник `error` с текстом; пропал heartbeat → `disconnected`; `GET /health` |
| Данные переживают перезапуск | SQLite/PostgreSQL + файлы в постоянном `STORAGE_DIR`; при старте зависшие эпизоды закрываются |

**Ограничения:**
- **CV-модель ещё не готова.** Сейчас работает заглушка `MockDetector` с известным сценарием. Реальная модель подключается заменой одного класса, см. «[Подключение модели](#подключение-модели-для-cv-участника)».
- **`LinqDetector` — заглушка.** Вызов Python API LinQ и декодирование выхода сети помечены TODO: сигнатуры берутся из `usage_example_python_api.zip` организаторов. letterbox, NMS и перевод координат уже реализованы.
- **Сценарии.** Реализован `danger_zone`. `fall_immobility` есть в enum и как рабочий пример в шаблоне, но не зарегистрирован.
- **Источники.** `kind=images` пока не запускается. RTSP открывается через OpenCV, но на живой камере не проверялся.
- **Клипы.** Видеофрагменты (`EvidenceKind.clip`) пока не создаются — только кадры `frame_raw` и `frame_annotated`.
- **Без авторизации.** Публичный API без логина; внутренний API воркера защищён токеном.
- **Применение настроек.** Изменения зон и правил применяются при следующем запуске анализа источника.
- **Ручной воркер.** Воркер, запущенный вручную, останавливается только Ctrl+C: `stop` из backend лишь меняет статус.

## Архитектура

```
                      ┌──────────────────────── backend (FastAPI, один процесс) ───────────────────────┐
 фронтенд ── REST ───▶│ /api/v1: sources, zones, rules, events, stats, metrics, health                 │
          ◀── WS ─────│ /ws/events (хаб в памяти)        /media (кадры, видео)                         │
                      │ WorkerManager ── start/stop ──┐  фоновая проверка heartbeat                     │
                      │ /internal (X-Internal-Token) ◀┼──────────────┐       SQLite/PostgreSQL + STORAGE_DIR
                      └───────────────────────────────┼──────────────┼──────────────────────────────────┘
                                         subprocess   ▼              │ HTTP: события, кадры, heartbeat
                      ┌─────────── воркер (workers/run_pipeline.py, один на источник) ───────────────┐
                      │ FrameSource ─▶ Detector ─▶ Tracker ─▶ ScenarioRule(s) ─▶ EpisodeManager ─▶ backend_client
                      │ (видео/RTSP)  (mock|replay|   (IoU)    (danger_zone,…)   (эпизоды,          │
                      │                linq)                                     подтверждение,     │
                      │                                                          кадры)             │
                      └──────────────────────────────────────────────────────────────────────────────┘
```

- **Детектор** отвечает только на вопрос «что и где на кадре» (`FrameDetections`).
- **Сценарий** на каждом кадре говорит, какие треки нарушают (`RuleHit`).
- **EpisodeManager** превращает покадровые нарушения в эпизоды по `video_ts`: подтверждение, допуск потери, завершение. Он один для всех сценариев.
- **Воркер** — отдельный процесс: тяжёлый инференс не тормозит API, падение модели не роняет backend.
- **Без брокеров.** Повторная отправка событий безопасна (идемпотентный upsert), поэтому Celery, Kafka и Redis не нужны.

Структура:

```
app/
  main.py, config.py, db.py, errors.py
  models/        SQLAlchemy-модели          schemas/   Pydantic: контракт API и CV-контракт
  api/v1/        публичный API              api/internal.py, api/ws.py
  services/      события, кадры, источники, правила, статистика, WS-хаб, WorkerManager
  pipeline/      interfaces, sources, detectors/, tracker, scenarios/, episodes, annotate,
                 runner (Pipeline), worker, backend_client
workers/run_pipeline.py    tools/ (seed, validate_detections, make_demo_video)
alembic/                   tests/ (+ fixtures/)    docs/
```

## Установка и запуск

Нужен Python **3.11+** (проверено на 3.12) и git. Все команды выполняются из каталога `backend/`.

**Windows (PowerShell):**
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Backend доступен на http://localhost:8000, документация — http://localhost:8000/docs.
Миграции применяются при старте автоматически.

Частые проблемы:
- **`SSL: CERTIFICATE_VERIFY_FAILED` при `pip install`** (корпоративный прокси или антивирус). Используйте
  сертификаты Windows: `python -m pip install --use-feature=truststore -r requirements.txt`.
  Проверку SSL не отключайте.
- **Кракозябры вместо русского в логах на Windows.** Выполните `$env:PYTHONIOENCODING="utf-8"` перед запуском.
- **`Activate.ps1` запрещён политикой.** Выполните `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
  или запускайте `.\.venv\Scripts\python.exe -m uvicorn app.main:app`.

### Быстрое демо

```bash
python tools/seed.py --demo          # демо-видео + источник + зона + правило
uvicorn app.main:app                 # в другом терминале
curl -X POST http://localhost:8000/api/v1/sources/<source_id>/start
curl http://localhost:8000/api/v1/events
```

Для живого показа задайте в `.env` `REALTIME_PLAYBACK=true`: видео анализируется в реальном темпе,
и тревоги в WebSocket приходят в момент нарушения. Демо-сценарий длится 26 с:
- вход в зону на 3 с, тревога на ~4 с;
- перекрытие на 7–8 с — новой тревоги нет;
- выход на ~12 с;
- ложный кадр детектора на 15 с — тревоги нет;
- второй вход на ~20 с — вторая тревога.

## Переменные окружения

Задаются в `.env` (образец с комментариями — `.env.example`) или в окружении процесса.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/app.db` | БД. Для PostgreSQL: `postgresql+psycopg://user:pass@host/db` (+ `pip install psycopg[binary]`) |
| `STORAGE_DIR` | `./data/storage` | Постоянный каталог: `videos/`, `evidence/{event_id}/`, `logs/` |
| `AUTO_MIGRATE` | `true` | Применять миграции при старте |
| `INTERNAL_TOKEN` | пусто | Токен воркер → backend. Пусто — генерируется при старте (подходит, если воркеры запускает backend) |
| `BACKEND_URL` | `http://localhost:8000` | Адрес backend для воркера |
| `CORS_ORIGINS` | `http://localhost:5173` | Адреса фронтенда через запятую |
| `DETECTOR` | `mock` | `mock` \| `replay` \| `linq` |
| `REPLAY_PATH` | пусто | JSONL-файл детекций для `replay` |
| `REALTIME_PLAYBACK` | `false` | Анализ файла в реальном темпе (для демо) |
| `HEARTBEAT_INTERVAL_S` | `2` | Период heartbeat воркера |
| `HEARTBEAT_TIMEOUT_S` | `10` | Нет heartbeat дольше → `disconnected` |
| `EPISODE_UPDATE_INTERVAL_S` | `1` | Не чаще этого — обновления активного эпизода |
| `DEFAULT_ANALYSIS_FPS` | `10` | Частота анализа для новых источников |
| `MAX_UPLOAD_MB` | `2048` | Лимит размера загружаемого видео |
| `LOG_LEVEL` | `INFO` | Уровень логов |
| `LINQ_API_URL`, `LINQ_LOGIN`, `LINQ_PASSWORD`, `LINQ_PROGRAM` | пусто | Доступ к LinQ — **только в `.env`** |

## Миграции

```bash
alembic upgrade head                                   # применить (backend делает это и сам)
alembic revision --autogenerate -m "описание"          # после изменения моделей
```
Адрес БД берётся из `DATABASE_URL`. Тест `test_migrations_match_models` упадёт, если модели
изменены, а миграция не создана.

## Seed

```bash
python tools/seed.py путь/к/видео.mp4 --name "Камера 3" --site "Цех №2"
python tools/seed.py --demo
```
Скрипт создаёт источник, зону-полигон и правило `danger_zone`; повторный запуск дублей не создаёт.
Затем фронт рисует зоны поверх `GET /api/v1/sources/{id}/preview`.

## Запуск воркера вручную

Обычно воркер запускает backend (`POST /sources/{id}/start`). Для отладки модели его можно запустить
вручную. В `.env` должен быть задан одинаковый `INTERNAL_TOKEN` для backend и воркера.

```bash
python workers/run_pipeline.py --source-id <id> [--detector replay] [--realtime]
```
Ctrl+C завершает активные эпизоды и отправляет статус `stopped`. Лог воркера, запущенного из backend:
`STORAGE_DIR/logs/<source_id>.log`.

## Примеры запросов

```bash
API=http://localhost:8000/api/v1

# Загрузить видео
curl -F "file=@shift.mp4" -F "name=Камера 3" -F "site=Цех №2" $API/sources/upload

# Зона и правило
curl -X POST $API/sources/<source_id>/zones -H "Content-Type: application/json" \
     -d '{"name": "Зона пресса", "polygon": [[0.35,0.45],[0.75,0.45],[0.75,0.95],[0.35,0.95]]}'
curl $API/scenarios
curl -X POST $API/rules -H "Content-Type: application/json" \
     -d '{"source_id": "<source_id>", "zone_id": "<zone_id>", "scenario_type": "danger_zone", "params": {"confirm_window_s": 1.5}}'

# Запуск, остановка, состояние
curl -X POST $API/sources/<source_id>/start
curl -X POST $API/sources/<source_id>/stop
curl $API/health

# Журнал и действия оператора
curl "$API/events?scenario_type=danger_zone&source_id=<source_id>&from=2026-10-23T00:00:00Z&limit=20"
curl $API/events/<event_id>
curl -X POST $API/events/<event_id>/actions -H "Content-Type: application/json" \
     -d '{"action": "confirm", "comment": "Подтверждаю", "author": "Иванова А.С."}'

# Сводка и замеры
curl "$API/stats?from=2026-10-23T00:00:00Z"
curl "$API/metrics?source_id=<source_id>"
```
В PowerShell используйте `curl.exe`, а JSON передавайте из файла: `-d "@body.json"`.

## Подключение модели (для CV-участника)

Подробно — [docs/CV_CONTRACT.md](docs/CV_CONTRACT.md). Остальной код менять не нужно.

**(а) Модель пока работает локально → replay.**
1. Прогоните видео моделью и сохраните детекции в JSONL: одна строка — один кадр в формате `FrameDetections`, bbox в долях кадра 0..1. Образец — `tests/fixtures/detections_sample.jsonl`.
2. Проверьте файл: `python tools/validate_detections.py detections.jsonl`.
3. В `.env` задайте `DETECTOR=replay` и `REPLAY_PATH=путь/к/detections.jsonl`, перезапустите backend и запустите источник с тем же видео.

**(б) Модель на LinQ.**
1. Реализуйте в `app/pipeline/detectors/linq.py` места с `TODO(CV)`: подключение и загрузку программы в `__init__`, `_run` (вызов TPU через Python API из `usage_example_python_api.zip`), `_decode` (выход сети → боксы, уверенности, классы) и таблицу `CLASS_NAMES`.
2. Параметры доступа — в `.env` (`LINQ_*`).
3. Задайте `DETECTOR=linq`. Пока методы не реализованы, источник честно уходит в `error` с `NotImplementedError` и не выдаётся за «нарушений нет».

## Новый сценарий

1. Добавьте значение в `ScenarioType` (`app/schemas/enums.py`).
2. Скопируйте `app/pipeline/scenarios/_template.py` в новый модуль, задайте `Params` (у каждого поля должен быть дефолт) и `update()`, который возвращает нарушения кадра. Включите `@register_scenario`.
3. Импортируйте модуль в `app/pipeline/scenarios/__init__.py`.

Сценарий появится в `GET /scenarios` с JSON Schema параметров. Правила для него создаются через
`POST /rules`, а подтверждение, подавление повторов, тревоги, журнал и кадры работают автоматически.

## Замеры для отчёта

`GET /api/v1/metrics?source_id=<id>`:
- `fps`, `inference_ms_avg`, `inference_ms_p95`, `frames_processed`, `detector_kind`, `model_version` — последний heartbeat воркера. **Скорость LinQ снимайте с `DETECTOR=linq` и `REALTIME_PLAYBACK=false`**, иначе FPS упрётся в `analysis_fps`;
- `detection_delay_real_s` — реальное время отклика `detected_at − started_at`. Честное значение — при `REALTIME_PLAYBACK=true` или на живой камере: ≈ `confirm_window_s` + время обработки, на демо 1.01 с. При обработке файла быстрее реального времени оно бывает меньше окна и даже отрицательным;
- `detection_delay_video_ts_s` — то же по таймлайну видео, от скорости обработки не зависит;
- `notify_delay_ms` — от подтверждения конвейером до отправки тревоги в WebSocket;
- `alerts_per_episode` — 1.0 означает, что повторы подавлены.

## Тесты и линтер

```bash
pytest            # каждый тест — своя временная БД и STORAGE_DIR
ruff check . && ruff format --check .
```
Набор включает сквозной тест: настоящий процесс воркера против backend на реальном порту.
Тестовые видео генерируются на лету и в репозиторий не коммитятся
(`python tests/fixtures/make_sample_video.py` — вручную).
