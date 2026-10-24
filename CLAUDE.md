# CLAUDE.md — контекст для следующей сессии

## Проект
Хакатон «Открой Моспром 2026», кейс LinQ: видеоконтроль промбезопасности. Видео → детектор людей →
сценарий `danger_zone` (ноги человека в полигоне зоны) → EpisodeManager: ОДНО событие на эпизод → тревога в
браузер по WebSocket → журнал, реакция оператора, статистика и замеры скорости.

## Структура
- `backend/` — FastAPI (Python 3.12, venv в `backend/.venv`). Источник правды по API — код и
  `backend/docs/CONTRACT.md` (+ OpenAPI `/docs`); CV-контракт — `backend/docs/CV_CONTRACT.md`.
  - `app/api/v1` REST, `app/api/ws.py` WS `/ws/events` и `/ws/live/{source_id}` (живые кадры с треками).
  - `app/pipeline/` конвейер в отдельном процессе-воркере на источник: `sources → detectors → tracker →
    scenarios → episodes → backend_client`. Детекторы: `detectors/{mock,replay,onnx,linq}.py`, общий
    YOLO-код `detectors/yolo_common.py` (letterbox/postprocess, без onnxruntime). Фабрика и
    `DETECTOR→kind` (`onnx→onnx_cpu`) — `detectors/__init__.py`.
  - `tools/`: seed, make_demo_video, validate_detections, export_detections, benchmark_detector.
  - `models/` — веса YOLOv8s, НЕ в git (`yolov8s.onnx` используется, `.pt` только для реэкспорта).
- `frontend/` — ванильный JS (ES-модули, без сборки и npm-зависимостей), раздаётся backend на `/`.
  `js/api.js` — единственный REST-клиент; `state.js` — состояние и шина событий (`on` возвращает отписку);
  экраны: `live.js` (мониторинг+лента), `journal.js` (журнал, фильтры в `#journal?…`), `territory.js`
  (камеры, редактор зон, правила по JSON Schema), `analytics.js` (статистика и замеры), `event-modal.js`
  (карточка; `#…?event=<id>` открывает её). Тема — `data-theme` на `<html>`, токены в `css/app.css`.

## Команды
```powershell
cd backend; .\.venv\Scripts\python.exe -m uvicorn app.main:app      # http://localhost:8000 (API + фронт)
.\.venv\Scripts\python.exe -m pytest -q                              # 137 тестов, ~25 с
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format --check .
cd frontend; npm run check                                           # синтаксис + импорты/экспорты
```
Windows: перед запуском `$env:PYTHONIOENCODING="utf-8"`. PowerShell 5.1 портит кириллицу в
`Invoke-RestMethod`/аргументах — для проверок используйте Python/httpx.

## Соглашения
- ID — UUID; время — ISO 8601 UTC с `Z`, во фронте — местное (`fmtDateTime`); координаты bbox/зон — доли 0..1.
- У события независимы `state` (active/ended, меняет конвейер) и `review_status` (меняет оператор).
- Ошибки API: `{"error": {"code","message","details"}}`; фронт показывает `message`.
- Ошибка источника/модели/сервера никогда не выглядит как «нарушений нет»: детектор бросает исключение →
  источник `error`; фронт различает «нет связи с сервером», error/disconnected, «остановлено», «идёт, нарушений нет», «загрузка».
- `detector_kind != "linq"` всегда помечается в UI как «не LinQ».
- Новый эндпоинт/поле → тест + обновить `backend/docs/CONTRACT.md`. Изменение моделей БД → миграция alembic
  (`test_migrations_match_models` проверяет).
- **Не выдумывать API LinQ**: вызов ускорителя — только по `usage_example_python_api.zip` организаторов
  (`LinqDetector._run`, `_to_device_input`, `_dequantize` — TODO). Секреты — только в `backend/.env`.
- Код простой, комментарии и UI на русском, стиль — как в соседних файлах.

## Статус (2026-10-24)
Готово: backend + конвейер; YOLOv8s через onnxruntime CPU (`DETECTOR=onnx`, проверено на bus.jpg и сквозным
прогоном: 1 тревога на эпизод, обновления той же карточки, действия, журнал, метрики `onnx_cpu` ~70 мс/кадр);
пре-/постпроцессинг в `/metrics` (миграция 0002); фронт: мониторинг, журнал, карточка с увеличением,
источники/зоны/правила, статистика, светлая/тёмная тема, адаптив до 390px, баннер «нет связи с сервером».

Осталось:
- `LinqDetector`: вызов TPU, квантование входа/деквантование выхода — по API организаторов.
- Видео-клипы как доказательства (`EvidenceKind.clip`) не создаются; `kind=images` не запускается.
- Реальное видео с людьми для демо (в репо нет; синтетический `make_demo_video` YOLO не распознаёт — для него `DETECTOR=mock`).
- Изменения зон/правил применяются только после перезапуска анализа (есть кнопка «Перезапустить»).
