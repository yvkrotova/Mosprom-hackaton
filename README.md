# LinQ Safety — видеоконтроль промышленной безопасности

Хакатон «Открой Моспром 2026», кейс LinQ. Веб-приложение для специалиста по охране труда, диспетчера и
мастера смены: видео → нейросеть находит людей → правило сценария (человек в опасной зоне) → **одно**
событие на эпизод → тревога с кадром в браузере → журнал.

```
backend/    FastAPI + конвейер анализа (воркер на источник), SQLite, WebSocket. Подробно: backend/README.md
frontend/   интерфейс оператора: статические ES-модули без сборки, их раздаёт backend
linq/       подготовка к LinQ TPU: калибровка, notebook для Jupyter, сравнение и замеры
docs/       статус, замеры, заметки по SDK LinQ, шаги для человека, инструкция по разметке
```

## Запуск

Нужны Python 3.11+ (проверено на 3.12) и, по желанию, Node.js 18+ (только для проверки фронта).

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # здесь же выбирается детектор: DETECTOR=mock|replay|onnx|linq
.\.venv\Scripts\python.exe -m uvicorn app.main:app
```

Откройте **http://localhost:8000** — backend отдаёт и API, и интерфейс. Swagger: http://localhost:8000/docs.
(Linux/macOS — те же шаги, см. [backend/README.md](backend/README.md).)

### Нейросеть YOLOv8s

Положите `yolov8s.onnx` (и, если есть, `yolov8s.pt`) в `backend/models/` — веса в git не хранятся. Получить заново:
`yolo export model=yolov8s.pt format=onnx imgsz=640 opset=12 simplify=True` (в отдельном окружении с ultralytics).
Затем в `backend/.env`: `DETECTOR=onnx`. Это **CPU компьютера, не LinQ** — интерфейс и `/metrics` показывают это явно
(`onnx_cpu`).

### LinQ TPU
Маршрут: ONNX → SDK LinQ (квантование, компиляция) → программа `.tpu` → инференс через Jupyter организаторов.
Готово: пре- и постпроцессинг, общий с ONNX; калибровочный набор `linq/prepare_calibration.py`; notebook
`linq/notebooks/run_linq_detections.ipynb`; сравнение с ONNX и сводка замеров; показ записи с TPU в интерфейсе
(`DETECTOR=replay`, «LinQ TPU (запись)»). **Не готово:** сама компиляция и вызов TPU. SDK (505 МБ) и пример API
не скачались с машины разработки, а команды SDK мы не угадываем. Что известно — [docs/LINQ_SDK_NOTES.md](docs/LINQ_SDK_NOTES.md),
шаги для человека с VPN — [docs/LINQ_TODO_FOR_HUMAN.md](docs/LINQ_TODO_FOR_HUMAN.md).

### Демо на тестовых видео (DETECTOR=onnx, TRACKER=bytetrack)

Ролики кладутся в `backend/data/videos/` и `backend/data/storage/videos/` (в git их нет, описание —
`backend/data/videos/manifest.json`). В `backend/.env`: `DETECTOR=onnx`, `TRACKER=bytetrack`,
`DETECTOR_CONF_THRESHOLD=0.1`, `REALTIME_PLAYBACK=true`.
```powershell
cd backend
.\.venv\Scripts\python.exe tools\seed.py --videos     # 4 источника: зоны и правила «зона» + «бег» уже настроены
```
Зоны предложены по превью: поправьте их в «Источники и зоны», если нужно (правка применяется при следующем запуске).

Порядок показа:
1. **«Цех: проход, человек уходит от камеры» (te21)** — человек проходит «Проход у станка». Через ~1 с после
   входа в зону приходит **одна** тревога «Человек в опасной зоне». Карточка обновляется, пока он в зоне, и
   завершается, когда выходит. Бега нет: в панели треков скорость ~0.3–0.6 роста/с.
2. **«KTH: бег поперёк кадра»** — 4 пробежки → 4 тревоги «Бег / резкое перемещение». В карточке кадр с красной
   рамкой, траекторией и скоростью ~2 роста/с (это не м/с).
3. **«Цех: проход, люди вдали» (te1)** — рамки держатся без мерцания у нескольких людей (у каждого свой цвет).
   С ~11 с человек входит в «Проход у пресса» → одна тревога.
4. **«Склад: обрушение стеллажа»** — люди слишком мелкие для YOLOv8s: тревог нет. Честный пример ограничения детектора.
5. «Журнал» → фильтр «Бег / резкое перемещение»; «Статистика» → замеры и плашка «ONNX · CPU (не LinQ)».

Без модели — заглушка: `DETECTOR=mock`, `python tools/seed.py --demo` (вход в зону) или `MOCK_SCENARIO=run_fast_movement` (бег).
Замеры, проверка трекера и порога — [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

### Ограничения
- Инференс пока на CPU (~9 кадров/с), не на LinQ (см. выше).
- Скорость бега — в ростах человека в секунду при неподвижной камере. Порог 1.5 подтверждён на одном ролике с бегом.
- YOLOv8s (COCO) плохо видит очень мелких и сидящих людей (видно на te21 и складе).
- Разметка тестовых роликов — черновик ([docs/VIDEO_LABELING.md](docs/VIDEO_LABELING.md)).

## Проверки

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest                 # тесты (включая YOLO и сквозной с настоящим воркером)
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format --check .
cd ..\frontend
npm run check                                        # синтаксис модулей и импорты (без зависимостей)
```

> Логины и пароли (VPN, Jupyter, LinQ) — только в `backend/.env`, никогда в коде и в git.
