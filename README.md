# LinQ Safety — видеоконтроль промышленной безопасности

Хакатон «Открой Моспром 2026», кейс LinQ. Веб-приложение для специалиста по охране труда, диспетчера и
мастера смены: видео → нейросеть находит людей → правило сценария (человек в опасной зоне) → **одно**
событие на эпизод → тревога с кадром в браузере → журнал.

```
backend/    FastAPI + конвейер анализа (воркер на источник), SQLite, WebSocket. Подробно: backend/README.md
frontend/   интерфейс оператора: статические ES-модули без сборки, их раздаёт backend
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
(`onnx_cpu`). Детектор на ускорителе LinQ (`DETECTOR=linq`) ждёт Python API организаторов.

### Демо за минуту

1. «Добавить видео» → загрузите запись с людьми (или `python tools/seed.py --demo` для синтетического ролика с `DETECTOR=mock`).
2. «Источники и зоны» → «Новая зона» → кликами обведите опасный участок по полу → «Сохранить» (правило включается сразу).
3. «Мониторинг» → «Запустить». Человек в зоне дольше окна подтверждения → тревога со звуком и тостом, карточка в ленте
   обновляется, пока эпизод идёт.
4. В карточке: «Подтвердить» / «Ложное срабатывание» / «Передать мастеру», комментарий. «Журнал» — фильтры в адресе
   страницы, «Статистика» — замеры скорости и тип детектора.

## Проверки

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest                 # тесты (включая YOLO и сквозной с настоящим воркером)
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format --check .
cd ..\frontend
npm run check                                        # синтаксис модулей и импорты (без зависимостей)
```

> Логины и пароли (VPN, Jupyter, LinQ) — только в `backend/.env`, никогда в коде и в git.
