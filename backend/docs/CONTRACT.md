# Контракт API backend LinQ Safety

Документ для фронтенда. Интерактивная версия — Swagger UI на `http://localhost:8000/docs`
(OpenAPI JSON на `/openapi.json`). Если этот файл и OpenAPI расходятся, прав OpenAPI — сообщите нам.

## Общие соглашения

| Что | Как |
|---|---|
| Базовый URL REST | `http://localhost:8000/api/v1` |
| WebSocket | `ws://localhost:8000/ws/events` |
| Файлы (кадры-доказательства) | `http://localhost:8000/media/...`; в ответах приходят относительные URL вида `/media/evidence/<event_id>/<file>.jpg`, к ним нужно добавить адрес backend |
| ID | UUID-строки |
| «Настенное» время | ISO 8601 в UTC с суффиксом `Z`: `2026-10-23T09:15:02.120Z`. В query-параметрах без пояса — считается UTC |
| Время внутри видео | отдельные поля `video_ts*`, секунды (float) от начала видео |
| Координаты | доли кадра `0..1`: bbox `[x1, y1, x2, y2]`, полигон `[[x, y], ...]`; `(0,0)` — левый верхний угол |
| Тело запросов | JSON; неизвестные поля в теле — ошибка 422 |
| Авторизация | нет (публичный API); внутренний API воркера — не для фронта |

Обновлять экран: при загрузке страницы берите состояние из REST, затем слушайте WS только для новых изменений.

## Перечисления

| Enum | Значения |
|---|---|
| `ScenarioType` | `danger_zone` (человек в опасной зоне), `run_fast_movement` (бег / резкое перемещение), `fall_immobility` (падение/неподвижность — **пока не реализован**, в `GET /scenarios` не появляется) |
| `EpisodeState` | `active` (нарушение продолжается), `ended` (закончилось) |
| `ReviewStatus` | `unreviewed`, `confirmed`, `false_positive`, `escalated` |
| `OperatorActionType` | `confirm`, `mark_false`, `escalate`, `comment`, `reset` |
| `Priority` | `low`, `medium`, `high`, `critical` |
| `SourceKind` | `file`, `rtsp`, `images` (`images` пока не запускается) |
| `SourceStatus` | `idle` (ни разу не запускался), `running`, `stopped`, `error`, `disconnected` (нет heartbeat от воркера) |
| `EvidenceKind` | `frame_raw` (исходный кадр), `frame_annotated` (кадр с рамкой, зоной и подписью), `clip` (видеофрагмент, пока не создаётся) |

`state` и `review_status` — **два независимых поля**. `state` меняет только конвейер, `review_status` — только оператор.
Событие может быть `active` и уже `confirmed`, или `ended` и всё ещё `unreviewed`.

Статусы `error` и `disconnected` означают «анализ не идёт». Это не то же самое, что «нарушений нет»:
показывайте их явно.

## Формат ошибок

Все ошибки (400, 401, 404, 405, 409, 413, 415, 422, 500) приходят в одном формате:

```json
{
  "error": {
    "code": "EVENT_NOT_FOUND",
    "message": "Событие не найдено",
    "details": {"event_id": "7d3c..."}
  }
}
```

Ориентируйтесь на `code`; `message` можно показывать пользователю. Для ошибок валидации (`VALIDATION_ERROR`)
`details.errors` — список `{"loc": ["body", "polygon", 0], "msg": "...", "type": "..."}`.

| HTTP | code | Когда |
|---|---|---|
| 400 | `BAD_REQUEST` | Некорректный запрос, не подпадающий под другие коды |
| 401 | `UNAUTHORIZED` | Внутренний API без верного `X-Internal-Token` |
| 404 | `NOT_FOUND` | Неизвестный путь |
| 404 | `SOURCE_NOT_FOUND` / `ZONE_NOT_FOUND` / `RULE_NOT_FOUND` / `EVENT_NOT_FOUND` | Объекта с таким id нет |
| 405 | `METHOD_NOT_ALLOWED` | Метод не поддерживается |
| 409 | `SOURCE_ALREADY_RUNNING` | `start` для уже запущенного источника |
| 409 | `SOURCE_RUNNING` | Удаление/изменение `uri` запущенного источника — сначала `stop` |
| 409 | `INVALID_TRANSITION` | Действие оператора недопустимо из текущего `review_status` |
| 409 | `EPISODE_ALREADY_ENDED` | (внутренний API) попытка вернуть завершённый эпизод в `active` |
| 413 | `FILE_TOO_LARGE` | Загружаемое видео больше лимита |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Загружен не видеофайл |
| 422 | `VALIDATION_ERROR` | Тело/параметры не прошли валидацию (в т.ч. полигон: <3 точек, координаты вне 0..1, нулевая площадь) |
| 422 | `INVALID_VIDEO` | Загруженный файл не читается как видео |
| 422 | `UNKNOWN_SCENARIO` | `scenario_type` не зарегистрирован |
| 422 | `INVALID_SCENARIO_PARAMS` | `params` не прошли схему сценария; в `details.errors` — подробности |
| 422 | `ZONE_SOURCE_MISMATCH` | Зона правила принадлежит другому источнику |
| 422 | `ZONE_REQUIRED` | Сценарию нужна зона, а у источника нет ни одной |
| 422 | `UNSUPPORTED_SOURCE_KIND` | Запуск источника вида, который пока не поддержан (`images`) |
| 422 | `PREVIEW_UNAVAILABLE` | Не удалось получить кадр источника (файл не открылся, поток недоступен) |
| 500 | `INTERNAL_ERROR` | Непредвиденная ошибка сервера (подробности только в логе сервера) |

---

## Sources — источники видео

### Объект `Source`

```json
{
  "id": "0b6f3a52-6a3e-4b8e-9a0f-4f2a8f2f6c11",
  "name": "Камера 3 — цех штамповки",
  "kind": "file",
  "uri": "data/storage/videos/0b6f...mp4",
  "site": "Цех №2",
  "status": "running",
  "last_error": null,
  "last_heartbeat_at": "2026-10-23T09:15:04.002Z",
  "analysis_fps": 10.0,
  "created_at": "2026-10-23T09:00:00.000Z",
  "updated_at": "2026-10-23T09:15:04.002Z"
}
```

### `GET /sources` → `200 Source[]`
Сортировка по `created_at`.

### `POST /sources` → `201 Source`
```json
{"name": "Камера 1", "kind": "rtsp", "uri": "rtsp://10.0.0.5/stream1", "site": "Склад", "analysis_fps": 5}
```
`kind` по умолчанию `file`, `analysis_fps` — по умолчанию из настроек сервера (10). Статус нового источника — `idle`.

### `POST /sources/upload` → `201 Source`
`multipart/form-data`: `file` (видео: mp4, avi, mov, mkv, webm), `name`, `site` (необязательно), `analysis_fps` (необязательно).
Файл сохраняется на сервере и проверяется (должен открываться как видео), создаётся источник `kind=file`.
Ошибки: `415 UNSUPPORTED_MEDIA_TYPE` (расширение), `422 INVALID_VIDEO` (не читается), `413 FILE_TOO_LARGE`.
При удалении источника загруженный файл тоже удаляется.

```bash
curl -F "file=@shift.mp4" -F "name=Камера 3" -F "site=Цех №2" http://localhost:8000/api/v1/sources/upload
```

### `GET /sources/{id}` → `200 Source`  · `404 SOURCE_NOT_FOUND`

### `PATCH /sources/{id}` → `200 Source`
Любое подмножество полей `name`, `uri`, `site`, `analysis_fps`. `409 SOURCE_RUNNING` при смене `uri` у запущенного.

### `DELETE /sources/{id}` → `204`
Удаляет источник, его зоны и правила. События в журнале **остаются** (у них `source_name` станет `null`).
`409 SOURCE_RUNNING`, если источник запущен.

### `POST /sources/{id}/start` → `202 Source`
Запускает анализ, источник переходит в `running` (статус придёт и в WS `source.status`).
`409 SOURCE_ALREADY_RUNNING`, `422 UNSUPPORTED_SOURCE_KIND`.
Конфигурация (зоны, правила) читается в момент запуска; изменения зон и правил применяются после перезапуска.

### `POST /sources/{id}/stop` → `200 Source`
Останавливает анализ, активные эпизоды завершаются, статус `stopped`. Для незапущенного источника — тоже `200`, без изменений.

### `GET /sources/{id}/preview` → `200 image/jpeg`
Один кадр источника (для файла — кадр из начала видео) в исходном разрешении. Поверх него фронт рисует зоны
в долях `0..1`. `422 PREVIEW_UNAVAILABLE`, если кадр получить не удалось.

---

## Zones — зоны на кадре

### Объект `Zone`
```json
{
  "id": "5a1e...",
  "source_id": "0b6f...",
  "name": "Зона пресса",
  "polygon": [[0.3, 0.4], [0.7, 0.4], [0.7, 0.95], [0.3, 0.95]],
  "created_at": "2026-10-23T09:01:00.000Z",
  "updated_at": "2026-10-23T09:01:00.000Z"
}
```

Требования к полигону: от 3 до 100 точек, обе координаты в `0..1`, ненулевая площадь. Замыкать (повторять первую точку) не нужно.

| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| GET | `/sources/{id}/zones` | — | `200 Zone[]` |
| POST | `/sources/{id}/zones` | `{"name", "polygon"}` | `201 Zone` |
| PATCH | `/zones/{id}` | `{"name"?, "polygon"?}` | `200 Zone` |
| DELETE | `/zones/{id}` | — | `204`; правила этой зоны удаляются, события остаются (`zone_name` = `null`) |

---

## Scenarios и Rules — правила анализа

### `GET /scenarios` → `200 ScenarioInfo[]`
Зарегистрированные сценарии. По `params_schema` (JSON Schema) можно построить форму параметров.

```json
[
  {
    "scenario_type": "danger_zone",
    "title": "Человек в опасной зоне",
    "description": "Тревога, если ноги человека (центр нижней кромки рамки) внутри полигона зоны",
    "requires_zone": true,
    "params_schema": {"type": "object", "properties": {"min_confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.4}, "...": {}}},
    "defaults": {"min_confidence": 0.4, "confirm_window_s": 1.0, "gap_tolerance_s": 2.0, "labels": ["person"]}
  }
]
```

Параметры `danger_zone`:

| Параметр | По умолчанию | Смысл |
|---|---|---|
| `min_confidence` | 0.4 | Детекции с меньшей уверенностью игнорируются |
| `confirm_window_s` | 1.0 | Сколько секунд видео нарушение должно продолжаться до тревоги (отсекает единичные ложные кадры) |
| `gap_tolerance_s` | 2.0 | Потеря человека из вида короче этого не завершает эпизод (перекрытие, мигание детекции) |
| `labels` | `["person"]` | Классы объектов, которые считаются нарушителями |

Параметры `run_fast_movement` (`requires_zone: false`):

| Параметр | По умолчанию | Смысл |
|---|---|---|
| `speed_threshold` | 1.5 | Порог скорости, **«ростов человека в секунду»** (смещение ног / высота рамки / Δt), **не м/с**. Ходьба ≈ 0.6–1.0, бег ≈ 2–3. Камера должна быть неподвижной |
| `end_ratio` | 0.7 | Гистерезис: эпизод продолжается, пока скорость ≥ порог × end_ratio |
| `confirm_window_s` | 0.8 | Скорость выше порога должна держаться столько секунд видео до тревоги |
| `end_window_s` | 1.0 | Скорость ниже порога дольше этого — эпизод завершается |
| `speed_window_s`, `ema_alpha` | 0.6, 0.4 | Окно регрессии скорости и сглаживание |
| `min_track_age_s`, `min_points`, `max_height_change` | 0.5, 4, 0.35 | Когда скорость «неизвестна» (молодой трек, мало точек, скачет высота рамки); рамка у края кадра — тоже неизвестна |
| `min_confidence`, `gap_tolerance_s`, `labels` | 0.4, 2.0, `["person"]` | Как у danger_zone |

Правило `run_fast_movement` с `zone_id` — только когда ноги внутри зоны; **без `zone_id` — весь кадр**
(у `danger_zone` правило без зоны действует во всех зонах источника).

### Объект `Rule`
```json
{
  "id": "c3d9...",
  "source_id": "0b6f...",
  "zone_id": "5a1e...",
  "scenario_type": "danger_zone",
  "enabled": true,
  "priority": "high",
  "params": {"min_confidence": 0.4, "confirm_window_s": 1.0, "gap_tolerance_s": 2.0, "labels": ["person"]},
  "version": 1,
  "created_at": "2026-10-23T09:02:00.000Z",
  "updated_at": "2026-10-23T09:02:00.000Z"
}
```
`params` в ответе всегда полные (с дефолтами). `version` растёт при каждом изменении `params` или `enabled`;
в событии сохраняется `rule_version`, по которому оно создано.

| Метод | Путь | Тело / параметры | Ответ |
|---|---|---|---|
| GET | `/rules?source_id=` | `source_id` необязателен | `200 Rule[]` |
| POST | `/rules` | `{"source_id", "zone_id"?, "scenario_type", "enabled"?, "priority"?, "params"?}` | `201 Rule` |
| PATCH | `/rules/{id}` | `{"zone_id"?, "enabled"?, "priority"?, "params"?}` | `200 Rule` |
| DELETE | `/rules/{id}` | — | `204` |

`zone_id = null` — правило действует во всех зонах источника. При PATCH `params` объединяются с текущими
(переданные ключи заменяют старые). Ошибки: `422 UNKNOWN_SCENARIO`, `422 INVALID_SCENARIO_PARAMS`,
`422 ZONE_SOURCE_MISMATCH`, `404 SOURCE_NOT_FOUND` / `ZONE_NOT_FOUND`.

---

## Events — журнал нарушений

Одно событие = один **эпизод** нарушения: пока человек остаётся в зоне (с допуском кратких потерь),
это одно событие и одна тревога; выход и новый вход — новое событие. Разные люди — разные события.

### Объект `Event`
```json
{
  "id": "e2b4c8a0-1f5d-4c33-9a7e-3b0d5f9c2a11",
  "scenario_type": "danger_zone",
  "source_id": "0b6f...",
  "source_name": "Камера 3 — цех штамповки",
  "zone_id": "5a1e...",
  "zone_name": "Зона пресса",
  "track_id": 4,
  "priority": "high",
  "state": "active",
  "review_status": "unreviewed",
  "confidence": 0.91,
  "started_at": "2026-10-23T09:15:02.000Z",
  "last_seen_at": "2026-10-23T09:15:09.500Z",
  "ended_at": null,
  "video_ts_start": 12.0,
  "video_ts_end": 19.5,
  "video_ts_detected": 13.0,
  "detected_at": "2026-10-23T09:15:03.210Z",
  "received_at": "2026-10-23T09:15:03.240Z",
  "notified_at": "2026-10-23T09:15:03.245Z",
  "model_version": "mock-1.0",
  "rule_id": "c3d9...",
  "rule_version": 1,
  "description": "Человек в зоне «Зона пресса»",
  "details": null,
  "thumbnail_url": "/media/evidence/e2b4.../frame_annotated_12.000.jpg",
  "evidence": [
    {
      "id": "9f0a...",
      "event_id": "e2b4...",
      "kind": "frame_raw",
      "url": "/media/evidence/e2b4.../frame_raw_12.000.jpg",
      "video_ts": 13.0,
      "frame_time": "2026-10-23T09:15:03.000Z",
      "bboxes": [{"label": "person", "confidence": 0.91, "bbox": [0.42, 0.35, 0.55, 0.88], "track_id": 4}],
      "created_at": "2026-10-23T09:15:03.300Z"
    },
    {"id": "...", "kind": "frame_annotated", "url": "/media/evidence/e2b4.../frame_annotated_12.000.jpg", "...": "..."}
  ],
  "created_at": "2026-10-23T09:15:03.240Z",
  "updated_at": "2026-10-23T09:15:09.600Z"
}
```

`details` — данные сценария, `null` у `danger_zone`. У `run_fast_movement`:
`{"speed": 2.1, "speed_max": 2.6}` — текущая (у идущего эпизода) и максимальная скорость за эпизод,
«ростов человека в секунду» (не м/с).

Поля времени:
- `started_at`, `last_seen_at`, `ended_at` — «настенное» время эпизода. Для видеофайла = время запуска анализа + `video_ts`.
- `video_ts_start`, `video_ts_end` — то же в секундах от начала видео (для перемотки плеера).
- `video_ts_detected` — момент подтверждения эпизода в секундах видео (обычно `video_ts_start + confirm_window_s`).
- `detected_at` — когда конвейер подтвердил эпизод; `received_at` — когда backend принял; `notified_at` — когда
  ушла тревога в WS.
- `evidence` может быть пустым первые доли секунды после `event.created` — кадры догружаются следом
  (придёт `event.updated`).

### `GET /events` → `200 EventPage`

| Параметр | Тип | Смысл |
|---|---|---|
| `scenario_type` | ScenarioType | |
| `source_id` | UUID | Камера |
| `zone_id` | UUID | |
| `from`, `to` | datetime | Эпизод **пересекается** с интервалом: `started_at <= to` и (`ended_at >= from` или эпизод ещё активен) |
| `state` | EpisodeState | |
| `review_status` | ReviewStatus | |
| `priority` | Priority | |
| `limit` | int 1..500, по умолчанию 50 | |
| `offset` | int ≥ 0, по умолчанию 0 | |

Все фильтры необязательны и объединяются через И. Сортировка: `started_at` по убыванию.

```bash
curl "http://localhost:8000/api/v1/events?scenario_type=danger_zone&source_id=0b6f...&from=2026-10-23T00:00:00Z&limit=20"
```
```json
{"items": [ /* Event */ ], "total": 137, "limit": 20, "offset": 0}
```

### `GET /events/{id}` → `200 EventDetail`
`Event` + `actions` — история действий оператора по возрастанию времени:
```json
{
  "...": "все поля Event",
  "actions": [
    {
      "id": "a1...",
      "event_id": "e2b4...",
      "action": "confirm",
      "prev_status": "unreviewed",
      "new_status": "confirmed",
      "comment": "Подтверждаю, рабочий без допуска",
      "author": "Иванова А.С.",
      "created_at": "2026-10-23T09:16:00.000Z"
    }
  ]
}
```

### `POST /events/{id}/actions` → `200 EventDetail`
```json
{"action": "confirm", "comment": "Подтверждаю", "author": "Иванова А.С."}
```
Меняет `review_status` по таблице ниже, пишет запись в историю, рассылает WS `event.reviewed`.
`state` эпизода не меняется. `comment` обязателен для `action=comment`, для остальных — по желанию.

Таблица переходов `review_status`:

| action | Разрешено из | Переводит в |
|---|---|---|
| `confirm` | `unreviewed`, `escalated` | `confirmed` |
| `mark_false` | `unreviewed`, `escalated`, `confirmed` | `false_positive` |
| `escalate` | `unreviewed`, `confirmed` | `escalated` |
| `reset` | `confirmed`, `false_positive`, `escalated` | `unreviewed` |
| `comment` | любой | не меняет |

Недопустимый переход → `409 INVALID_TRANSITION`, `details: {"action", "current_status", "allowed_from"}`.

---

## Stats — сводка для дашборда

### `GET /stats?from=&to=` → `200 Stats`
Те же правила пересечения интервала, что в журнале. Без параметров — за всё время.
```json
{
  "from": "2026-10-23T00:00:00Z",
  "to": null,
  "total": 12,
  "by_scenario_type": [{"key": "danger_zone", "label": null, "count": 12}],
  "by_source": [{"key": "0b6f...", "label": "Камера 3 — цех штамповки", "count": 12}],
  "by_zone": [{"key": "5a1e...", "label": "Зона пресса", "count": 9}, {"key": "77aa...", "label": "Проход", "count": 3}],
  "by_hour": [{"hour": "2026-10-23T09:00:00Z", "count": 7}, {"hour": "2026-10-23T10:00:00Z", "count": 5}],
  "review": {"unreviewed": 4, "confirmed": 6, "false_positive": 1, "escalated": 1}
}
```
`by_hour` — по `started_at`, часы в UTC (переводите в местное время на фронте); часы без событий не выводятся.

## Metrics — замеры для отчёта

### `GET /metrics?source_id=` → `200 Metrics`
Без `source_id` — по всем источникам.
```json
{
  "sources": [
    {
      "source_id": "0b6f...",
      "source_name": "Камера 3 — цех штамповки",
      "ts": "2026-10-23T09:15:04.002Z",
      "fps": 24.6,
      "inference_ms_avg": 18.4,
      "inference_ms_p95": 22.9,
      "preprocess_ms_avg": 3.1,
      "postprocess_ms_avg": 1.4,
      "frames_processed": 1830,
      "detector_kind": "linq_tpu",
      "model_version": "yolov8n-linq-int8-2026.10"
    }
  ],
  "latency": {
    "events_count": 12,
    "detection_delay_real_s_avg": 1.31,
    "detection_delay_real_s_p95": 1.62,
    "detection_delay_video_ts_s_avg": 1.0,
    "detection_delay_video_ts_s_p95": 1.1,
    "notify_delay_ms_avg": 6.2,
    "notify_delay_ms_p95": 11.0,
    "alerts_per_episode": 1.0
  }
}
```
- `fps` — фактическая скорость обработки кадров воркером; `inference_ms_*` — время только вызова нейросети;
  `preprocess_ms_avg` / `postprocess_ms_avg` — letterbox и декодирование + NMS (`null`, если детектор их не меряет).
- `detector_kind` — что реально считало кадры: `mock` (заглушка), `replay` (детекции из файла), `onnx_cpu`
  (YOLOv8s на CPU компьютера — **не LinQ**), `linq_tpu` (ускоритель LinQ), `linq_tpu_replay` (запись детекций,
  посчитанных на LinQ в Jupyter, проигранная через `DETECTOR=replay`). Фронт явно помечает всё, что не начинается с `linq`.
- `detection_delay_real_s` — `detected_at − started_at`: реальное время отклика от начала нарушения до подтверждения
  (включает окно подтверждения `confirm_window_s`). Для живой камеры это и есть время реакции системы. Для видеофайла
  `started_at` = время запуска анализа + `video_ts_start`, поэтому при обработке быстрее реального времени значение
  может быть меньше окна подтверждения и даже отрицательным.
- `detection_delay_video_ts_s` — `video_ts_detected − video_ts_start`: то же по таймлайну видео. Не зависит от скорости
  обработки и показывает, сколько секунд нарушения нужно системе для подтверждения (≈ `confirm_window_s`).
- `notify_delay_ms` — `notified_at − detected_at`: от подтверждения конвейером до отправки тревоги в WS.
- `alerts_per_episode` — сколько тревог `event.created` ушло в среднем на эпизод; 1.0 означает, что повторы подавлены.

## Health

### `GET /health` → `200 Health`
```json
{
  "status": "degraded",
  "db": "ok",
  "detector_kind": "mock",
  "sources": [
    {"id": "0b6f...", "name": "Камера 3", "status": "disconnected", "last_heartbeat_at": "2026-10-23T09:15:04.002Z", "last_error": "Нет heartbeat от воркера 10 с"}
  ]
}
```
`detector_kind` — детектор из настройки сервера (`DETECTOR`), те же значения, что в `/metrics` (`DETECTOR=onnx` → `onnx_cpu`, `linq` → `linq_tpu`).
`status=degraded`, если БД недоступна или хотя бы один источник в `error`/`disconnected`.
Источник в `running`, от которого нет heartbeat дольше `HEARTBEAT_TIMEOUT_S` (10 с), переводится в `disconnected`.

---

## WebSocket `/ws/events`

Подключение: `ws://localhost:8000/ws/events`. Сервер только отправляет; входящие сообщения игнорируются.
При обрыве переподключайтесь и перечитайте состояние через REST.

Формат каждого сообщения:
```json
{"type": "event.created", "data": { /* ... */ }, "ts": "2026-10-23T09:15:03.245Z"}
```

| type | data | Когда |
|---|---|---|
| `event.created` | `Event` | Новый эпизод подтверждён — **это и есть тревога** (одна на эпизод) |
| `event.updated` | `Event` | Эпизод продолжается (не чаще раза в секунду) или догрузились кадры-доказательства |
| `event.ended` | `Event` | Эпизод завершён (`state=ended`) |
| `event.reviewed` | `Event` | Оператор изменил `review_status` или оставил комментарий |
| `source.status` | `SourceStatus` | Сменился статус источника |

`SourceStatus`:
```json
{"id": "0b6f...", "name": "Камера 3", "status": "error", "prev_status": "running", "last_error": "Не удалось открыть видео: ...", "last_heartbeat_at": "2026-10-23T09:15:04.002Z"}
```

`data` для `event.*` — тот же объект, что в `GET /events/{id}`, но без `actions`.
Обновляйте карточку события целиком по `data.id`.

---

## Живой просмотр: WebSocket `/ws/live/{source_id}`

Кадр источника вместе со снимком треков — для мониторинга. Не сохраняется в БД. Воркер присылает снимки
не чаще `TRACKS_PUSH_HZ` (8) в секунду; между ними берётся самый свежий кадр. Кадр и треки идут **одним
сообщением**: так рамки всегда соответствуют картинке, под которой их рисуют (отдельный `tracks.update`
без кадра разъезжался бы с изображением).

| type | data | Когда |
|---|---|---|
| `live.frame` | кадр и треки (ниже) | очередной снимок |
| `live.ended` | `{"source_id"}` | анализ источника завершился |

```json
{"type": "live.frame", "data": {
  "source_id": "0b6f...", "frame_idx": 120, "video_ts": 4.8, "width": 1920, "height": 1080,
  "fps": 9.8, "detector_kind": "onnx_cpu", "image": "data:image/jpeg;base64,...",
  "tracks": [{
    "track_id": 3, "label": "person", "confidence": 0.86, "bbox": [0.41, 0.30, 0.47, 0.88],
    "keypoints": null,
    "track_state": "confirmed",
    "state": "alert",
    "flags": ["violation"],
    "scenario_types": ["run_fast_movement"],
    "speed": 2.3,
    "zone_ids": []
  }]
}}
```
- `track_state`: `confirmed` — трек виден; `lost` — пропал недавно (не дольше `TRACK_BUFFER_S`), рамка —
  прогноз фильтра Калмана, рисуйте полупрозрачно. Неподтверждённые треки (шум) не присылаются.
- `flags`: `in_zone` (ноги в зоне правила), `candidate` (нарушение есть, но ещё не подтверждено:
  в зоне или быстро), `violation` (идёт подтверждённый эпизод — тревога уже ушла).
- `state` — то же для обратной совместимости: `normal` / `in_zone` (кандидат) / `alert`.
- `speed` — скорость сценария `run_fast_movement`, «ростов в секунду»; `null`, если неизвестна или правила нет.
- `image` — JPEG до 960 пикселей по ширине; `null`, если источник без картинки (replay без видео).

`GET /api/v1/sources/{id}/live.jpg` → последний живой кадр или `404 LIVE_NOT_AVAILABLE`.

## Изменения рефакторинга сценариев (этапы A–E, 2026-10-24)

- **Зоны:** `ZoneCreate/ZoneUpdate.scenario_types` — какие зонные сценарии включены (при создании без поля —
  сценарии с `zone_default`, сейчас `danger_zone`; снятая галочка удаляет правило зоны; сценарий на весь кадр —
  `422 SCENARIO_NOT_ZONAL`). `ZoneOut.scenario_types`, `ZoneOut.reference_url`.
  `POST /zones/{id}/reference` — текущий кадр источника сохраняется как эталон «пустой зоны»;
  `DELETE /zones/{id}/reference` — сброс (эталон соберётся автоматически за `SCENE_BOOTSTRAP_S`).
- **Правила по умолчанию:** сценарии с `auto_enable` (бег) получают правило на весь кадр при создании/загрузке
  источника и при запуске (`Rule.auto_created = true`, один раз на источник — `sources.defaults_applied`).
  `AUTO_START_ON_UPLOAD=true` — загрузка видео сразу запускает анализ.
- **Горячая перезагрузка:** запущенный воркер раз в `CONFIG_RELOAD_S` перечитывает зоны/правила и применяет
  их без перезапуска (видео не начинается заново).
- **`GET /scenarios`:** добавлены `icon`, `requires_scene_analysis`, `zone_default`,
  `details_fields: [{key, title, unit}]`. Фронт не перечисляет сценарии сам — подписи, иконки, формы
  и поля карточки события берутся отсюда.
- **Event (единый контракт для всех сценариев):** `id`, `source_id`, `zone_id`, `scenario_type`, `track_id`
  (**null** — нарушение зоны целиком, например предмет в проходе), `video_ts_start/detected/end` (всё по
  времени видео), `state`, `details` (значения из `RuleHit.extra.details` и максимумы `<key>_max`), доказательства.
- **Правила (внутренний контракт):** `rule.update(FrameContext, zones, params) -> list[RuleHit]` без состояния;
  `HitLevel`: VIOLATION (нарушение), HOLD (продолжает идущий эпизод), UNKNOWN (оценить нельзя — эпизод не
  открывается, идущий завершается после `gap_tolerance_s`). Всю логику эпизодов ведёт `EpisodeManager`.
- **`/ws/live/{id}` кадр:** `zone_states: [{zone_id, scenario_type, state: clear|candidate|violation|unknown,
  reason}]`; у трека флаг `trail` (правило просит рисовать след).
- **Метрики:** `scene_ms_avg` — анализ сцены, мс на обновление (null — не нужен).

## Replay, экраны оператора, разметка (2026-10-24, этапы A–B)

### Источник и предзаписанные детекции (replay)
- `Source`: `replay_path` (привязанная запись JSONL schema v2), `video_sha256` (кеш SHA-256 видео;
  считается при загрузке файла, пересчитывается, если файл изменился), `run_started_at` (начало
  последнего запуска — к нему относятся замеры и задержки в `/metrics`).
- `POST /sources/upload` — необязательное поле формы `replay` (JSONL). Битая запись → `422
  INVALID_REPLAY` с причиной (номер строки), источник не создаётся.
- `POST /sources/{id}/replay` (multipart `file`) — загрузить запись для существующего источника
  (`STORAGE_DIR/replays/<id>.jsonl`). `PATCH /sources/{id}` `{"replay_path": "..."}` — путь на сервере
  (абсолютный или от `STORAGE_DIR`), `""` — отвязать. У запущенного источника менять нельзя (409).
- Поиск записи: `replay_path` → `REPLAY_DIR` (по SHA-256 видео из заголовка) → `REPLAY_PATH`.
  Формат, проверки, политика пропусков и метрики — `CV_CONTRACT.md`, «Replay v2».
- **Повторный прогон:** для replay `event_id` детерминирован (uuid5, см. CV_CONTRACT). Повтор уже
  известного отрезка завершённого эпизода (`video_ts_end` не больше сохранённого) → `outcome:
  unchanged`, тревога не повторяется. Продолжение после конца (`video_ts_end` больше) — по-прежнему
  `409 EPISODE_ALREADY_ENDED`.

### События
- Новые поля `Event`: `viewed_at` (оператор открыл событие; **не** меняет `review_status`),
  `source_site`, `video_url` + `video_note`. `video_url` — исходный файл источника через `/media/…`
  (эпизод — `video_ts_start..video_ts_end`, интерфейс сам ставит видео на начало); клипы не режутся.
  Для потоков и недоступных файлов `video_url = null`, `video_note` объясняет, что есть только кадр.
- `POST /events/{id}/view` → `200 Event` — идемпотентно, шлёт `event.updated` в WS.
- `GET /events/unviewed?site=` → `{"total", "by_source": [{"source_id", "count"}]}`.
- `GET /events` — новые параметры: `order=desc|asc` (по началу эпизода), `zone_id=none` (только
  события «на весь кадр»), `site`, `viewed=true|false`. Все фильтры совместные.
- **Период `from`/`to` — по началу эпизода** (`from ≤ started_at ≤ to`), одинаково в журнале,
  CSV и статистике (раньше было пересечение интервалов).
- `GET /events/export.csv` — те же фильтры и `order`, **вся выборка**; `tz_offset_min` — смещение
  местного времени браузера от UTC в минутах (−840…840). UTF-8 с BOM, разделитель `;`. Колонки: начало
  (`2026-10-24 11:00:00 +03:00`), камера, зона («Весь кадр»), нарушение (название сценария), длительность
  с (по времени видео, запятая), уверенность %, состояние эпизода, результат проверки, приоритет.

### Результат проверки и «Передано мастеру» (решение)
Результатов проверки три: **не проверено / подтверждено / ложное**. `escalated` («передано мастеру») —
это подтверждённое нарушение с пометкой: фильтр `review_status=confirmed` включает `escalated`,
`review_status=escalated` — только переданные; в статистике `confirmed` включает переданных, а
`escalated` — «из них передано мастеру»; в CSV — «Подтверждено, передано мастеру». Поэтому
`unreviewed + confirmed + false_positive = total` всегда. Переходы — прежние (`REVIEW_TRANSITIONS`).
Имя оператора — поле `author` действия (фронт берёт его из меню пользователя).

### Статистика
`GET /stats?from=&to=&source_id=&site=` — добавлены:
- `summary`: `total`, `active`, `unreviewed`, `confirmed` (с переданными), `escalated`,
  `false_positive`, `avg_zone_time_s` (среднее `video_ts_end − video_ts_start` завершённых эпизодов
  `danger_zone`; `null` — таких нет), `alerts_per_episode` (сумма `alerts_sent` / эпизоды; `alerts_sent`
  растёт только при `event.created`, повторная доставка не считается; `null` — эпизодов нет).
- `demo`: `{"is_demo", "reason"}` — события от заглушки (`model_version` mock…) или камера
  анализировалась `detector_kind=mock`. Интерфейс пишет «Демонстрационные данные» только при `is_demo`.

### Замеры `GET /metrics`
По каждой камере (для «Все камеры» — список, не одна камера за всех): `source_status`,
`run_started_at`, `measurement_state` (`not_started` | `pending` | `running` | `stale` | `finished`),
`ts` (время замера), `fps` (обработка этим процессом), `inference_ms_avg/p95` (живой детектор),
`preprocess_ms_avg`, `postprocess_ms_avg`, `frame_ms_avg`, `frames_processed` (за запуск),
replay: `recorded_inference_ms_avg/p50/p95` (историческое время на LinQ из записи; `inference_ms_*`
при этом `null`), `frames_unanalyzed`, `replay_file`; `latency` — задержки по событиям этого запуска:
обнаружение по видео и по реальному времени (avg и p95), уведомление (avg и p95), тревог на эпизод.
Замер «устарел» (`stale`), если heartbeat не приходит дольше 5 интервалов или замер от прошлого запуска.

### Разметка камеры одной операцией
`PUT /sources/{id}/layout` → `200 {"zones": Zone[], "rules": Rule[]}`:
```json
{"zones": [{"id": "…или null", "name": "Опасная зона", "polygon": [[0.1,0.1],[0.5,0.1],[0.5,0.5]],
            "scenario_types": ["danger_zone"], "enabled": true}],
 "frame_rules": [{"scenario_type": "run_fast_movement", "enabled": true, "params": null}]}
```
Полный набор зон: отсутствующие удаляются (с правилами; журнал событий остаётся). Назначение зоны —
`scenario_types` (сценарии с `requires_zone`), активность — `enabled` всех правил зоны (`Zone.enabled`
в ответе). Сначала проверяется весь черновик, ошибки — сразу все: `422 INVALID_LAYOUT`,
`details.errors = [{"zone_index", "zone_id", "field": "name|polygon|scenario_types|id", "message"}]`
или `{"frame_rule_index", ...}`; при ошибке не меняется ничего. Запущенный анализ подхватывает
изменения сам (воркер перечитывает конфигурацию раз в `CONFIG_RELOAD_S`).

### Проверка контура
Общая функция `app/pipeline/geometry.py: polygon_problems` (та же логика — `frontend/js/geometry.js`):
минимум 3 разные точки, координаты 0..1, ненулевая площадь (≥ 0.0001 кадра), без самопересечений.
Применяется в `POST/PATCH` зон (422 с текстом) и в `PUT layout`.
