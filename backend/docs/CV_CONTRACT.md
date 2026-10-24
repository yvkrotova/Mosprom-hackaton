# CV-контракт: как подключить модель к конвейеру

Документ для участника, который делает нейросеть. Детектор выбирается в `.env` (`DETECTOR=...`),
остальной код от выбора не зависит:

| Способ | Когда | Что сделать |
|---|---|---|
| **ONNX на CPU** | Эталон и демо без LinQ: YOLOv8s (`models/yolov8s.onnx`) через onnxruntime на процессоре компьютера. **Это не LinQ** — `detector_kind=onnx_cpu` | `DETECTOR=onnx` (параметры — `MODEL_PATH`, `CONF_THRESHOLD`, `IOU_THRESHOLD`, `DETECT_CLASSES`) |
| **(а) Replay** | Модель работает локально (ноутбук, Jupyter), LinQ ещё не готов | Прогнать видео моделью, сохранить детекции в JSONL по формату ниже, запустить backend с `DETECTOR=replay`, `REPLAY_PATH=<файл>` |
| **(б) LinQ** | Модель скомпилирована под LinQ | Реализовать вызов ускорителя в `LinqDetector` (`app/pipeline/detectors/linq.py`, места `TODO(CV)`), запустить с `DETECTOR=linq` |

JSONL для replay и для сравнения с LinQ делает `tools/export_detections.py` (видео → детекции тем же
детектором, что и в конвейере), скорость меряет `tools/benchmark_detector.py` — см. раздел «YOLOv8s» ниже.

Схема конвейера:

```
FrameSource ──Frame──▶ Detector ──FrameDetections──▶ Tracker ──Track[]──▶ ScenarioRule(s) ──RuleHit[]──▶ EpisodeManager ──▶ backend
 (видео)               (ВЫ ЗДЕСЬ)                    (IoU)               (danger_zone, ...)              (подтверждение, эпизоды)
```

Детектор отвечает только за «что и где на кадре». Решение «это нарушение», подтверждение по времени,
подавление повторов и тревоги делает конвейер.

## Формат `FrameDetections`

Pydantic-модель: `app/schemas/cv.py`. Один объект на кадр; в JSONL — одна строка на кадр.

```json
{
  "source_id": null,
  "frame_idx": 120,
  "video_ts": 4.8,
  "frame_w": 1920,
  "frame_h": 1080,
  "inference_ms": 17.6,
  "model_version": "yolov8n-linq-int8-2026.10",
  "detections": [
    {
      "label": "person",
      "confidence": 0.87,
      "bbox": [0.412, 0.305, 0.498, 0.861],
      "track_id": null,
      "keypoints": null
    },
    {
      "label": "helmet",
      "confidence": 0.62,
      "bbox": [0.431, 0.305, 0.470, 0.352],
      "track_id": null,
      "keypoints": null
    }
  ]
}
```

| Поле | Тип | Требования |
|---|---|---|
| `source_id` | str \| null | В replay-файле можно `null` или не указывать — конвейер подставит свой |
| `frame_idx` | int ≥ 0 | Номер кадра **исходного** видео, с нуля (не номер обработанного кадра) |
| `video_ts` | float ≥ 0 | Время кадра от начала видео, секунды. Возрастает от строки к строке |
| `frame_w`, `frame_h` | int > 0 | Размер **исходного** кадра в пикселях (не входа сети) |
| `inference_ms` | float ≥ 0 | Время **только вызова сети** (session.run / программа LinQ), мс |
| `preprocess_ms`, `postprocess_ms` | float ≥ 0 \| null | Необязательно: letterbox и декодирование + NMS, мс. Попадают в `/metrics` отдельно |
| `device_ms` | float ≥ 0 \| null | Необязательно: время на самом ускорителе, если API его отдаёт (сеть = `inference_ms − device_ms`) |
| `detector_kind` | str \| null | Необязательно: чем посчитано (`onnx_cpu`, `linq_tpu`). Файл с `linq_tpu`, проигранный через replay, показывается как «LinQ TPU (запись)» |
| `model_version` | str | Любая строка, однозначно определяющая модель и её сборку; попадает в каждое событие |
| `detections` | list | Пустой список = на кадре ничего нет. Кадр с ошибкой — **не** пустой список, а исключение |

`Detection`:

| Поле | Тип | Требования |
|---|---|---|
| `label` | str | Класс объекта. Для людей — строго `person`. Остальные классы (`helmet`, `vest`, ...) — на ваше усмотрение, сценарии их пока игнорируют |
| `confidence` | float 0..1 | После sigmoid/softmax, не логиты |
| `bbox` | [x1, y1, x2, y2] | В **долях** исходного кадра 0..1, `x2 > x1`, `y2 > y1`. Левый верхний угол — `(0, 0)` |
| `track_id` | int \| null | Если модель/трекер на вашей стороне даёт стабильные id — укажите, backend их сохранит. Иначе `null`, трекер backend назначит сам. Не смешивайте: либо у всех детекций, либо ни у одной |
| `keypoints` | [[x, y, conf], ...] \| null | Для будущего сценария падения. `x, y` в долях кадра, `conf` 0..1. Пока необязательно |

Лишние поля в объекте — ошибка валидации (так ловятся опечатки вроде `confidance`).

### Координаты: важно

Сеть обычно работает на квадратном входе (например 640×640 с letterbox). Перед отдачей bbox нужно:
1. убрать паддинг letterbox: `x = (x_net − pad_x) / scale`, `y = (y_net − pad_y) / scale` → пиксели исходного кадра;
2. обрезать по границам кадра;
3. поделить на `frame_w` / `frame_h` → доли 0..1.

Проверка на глаз: человек у правого края кадра должен иметь `x2` около 1.0, у нижнего — `y2` около 1.0.
Сценарий опасной зоны смотрит на точку «ноги» — центр нижней кромки bbox `((x1+x2)/2, y2)`,
поэтому нижняя граница рамки должна быть точной.

### `video_ts` и прореживание

Конвейер анализирует видео с частотой `analysis_fps` (по умолчанию 10 кадров/с), пропуская остальные кадры.
Все длительности (подтверждение 1 с, допуск потери 2 с) считаются по `video_ts`, поэтому оно должно быть
временем внутри видео, а не временем на часах компьютера.

Для replay-файла:
- можно сохранять **все** кадры или только прореженные — `ReplayDetector` для каждого кадра конвейера
  берёт запись с ближайшим `video_ts` (допуск — половина интервала между кадрами анализа);
- кадр без записи в файле считается кадром без детекций;
- `video_ts` удобно брать из `cv2.CAP_PROP_POS_MSEC / 1000` или как `frame_idx / fps`.

Пример генерации replay-файла локальной моделью:

```python
import cv2, json, time

cap = cv2.VideoCapture("shift.mp4")
fps = cap.get(cv2.CAP_PROP_FPS)
with open("detections.jsonl", "w", encoding="utf-8") as out:
    idx = 0
    while True:
        ok, img = cap.read()
        if not ok:
            break
        h, w = img.shape[:2]
        t0 = time.perf_counter()
        boxes = my_model(img)  # -> [(label, conf, x1, y1, x2, y2) в пикселях]
        ms = (time.perf_counter() - t0) * 1000
        out.write(json.dumps({
            "frame_idx": idx,
            "video_ts": idx / fps,
            "frame_w": w, "frame_h": h,
            "inference_ms": ms,
            "model_version": "my-model-v1",
            "detections": [
                {"label": lbl, "confidence": c, "bbox": [x1 / w, y1 / h, x2 / w, y2 / h]}
                for lbl, c, x1, y1, x2, y2 in boxes
            ],
        }, ensure_ascii=False) + "\n")
        idx += 1
```

## Проверка файла

```bash
python tools/validate_detections.py detections.jsonl
```
Выводит ошибки по номерам строк (неверный bbox, координаты вне 0..1, убывающий `video_ts`, лишние поля)
и сводку: число кадров, длительность, классы, среднее `inference_ms`. Код выхода ненулевой, если есть ошибки.

Запуск конвейера на файле:
```bash
# .env: DETECTOR=replay, REPLAY_PATH=./detections.jsonl
python workers/run_pipeline.py --source-id <id> --detector replay
```
Видео источника при этом всё равно читается — из него берутся кадры для доказательств.

## Интерфейс `Detector` и `LinqDetector`

`app/pipeline/interfaces.py`:

```python
class Detector(ABC):
    kind: ClassVar[str]        # "mock" | "replay" | "linq"
    model_version: str

    def infer(self, frame: Frame) -> FrameDetections: ...
    def close(self) -> None: ...
```

`Frame`: `source_id`, `frame_idx`, `video_ts`, `width`, `height`, `image` — `np.ndarray` **BGR** `uint8`
формы `(height, width, 3)`, как отдаёт OpenCV.

Контракт `infer`:
- вызывается последовательно, по одному кадру, из одного потока;
- возвращает `FrameDetections` с тем же `frame_idx` и `video_ts`, что у входного кадра;
- `inference_ms` измеряет сам детектор;
- при любой ошибке (ускоритель недоступен, таймаут, неверный выход) — **бросает исключение**. Воркер
  отправит статус `error` с текстом ошибки, источник станет красным в интерфейсе. Пустой список детекций при ошибке
  недопустим: это выглядело бы как «нарушений нет».

`LinqDetector` (`app/pipeline/detectors/linq.py`): пре- и постпроцессинг уже готовы и общие с `OnnxDetector`
(`yolo_common.py`), заглушка — только вызов ускорителя (`_run` бросает `NotImplementedError`). Что осталось:

1. **`__init__`** — подключение к LinQ и загрузка программы (`LINQ_API_URL`, `LINQ_PROGRAM` из `.env`;
   логины и пароли — только в `.env`, никогда в коде). Здесь же задаётся `model_version`.
2. **Препроцессинг** — resize/letterbox до входа сети, BGR→RGB (если сеть обучена на RGB), порядок осей
   (HWC→CHW), тип и нормализация (`/255`, mean/std или квантование int8 — как при компиляции под LinQ).
3. **Инференс** — вызов программы TPU через Python API LinQ. Сигнатуры берутся из
   `usage_example_python_api.zip` организаторов; в коде заглушки они не выдуманы и помечены `TODO`.
4. **Постпроцессинг** — декодирование выхода (якоря/сетка, sigmoid), порог уверенности, NMS,
   обратное масштабирование в координаты исходного кадра (с учётом letterbox), перевод в доли 0..1,
   сопоставление id класса → `label`.
5. **`close`** — освобождение ускорителя.

Запуск: `DETECTOR=linq` в `.env` или `--detector linq` у воркера.

## Метрики, которые отправляет воркер

Каждые `HEARTBEAT_INTERVAL_S` (2 с) воркер шлёт heartbeat, backend сохраняет его в `processing_stats`;
последние значения видны в `GET /api/v1/metrics`:

| Метрика | Как считается |
|---|---|
| `fps` | Обработанные кадры / прошедшее реальное время, скользящее окно |
| `inference_ms_avg`, `inference_ms_p95` | По полю `inference_ms` ваших `FrameDetections` за последние ~100 кадров |
| `frames_processed` | Всего обработано кадров с начала запуска |
| `detector_kind`, `model_version` | Из детектора |

Плюс задержки по событиям (считает backend): реальное время отклика `detected_at − started_at`,
задержка по таймлайну видео `video_ts_detected − video_ts_start` и `notified_at − detected_at`.
Для отчёта о скорости на LinQ берите `inference_ms_*` и `fps` при `DETECTOR=linq`.

## YOLOv8s: вход, выход, общий код

Проверенные параметры `models/yolov8s.onnx` (экспорт `yolo export model=yolov8s.pt format=onnx imgsz=640 opset=12 simplify=True`):

| Что | Значение |
|---|---|
| Вход `images` | `[1, 3, 640, 640]` float32, RGB, `0..1`, статичный размер |
| Выход `output0` | `[1, 84, 8400]` float32, **без NMS**: `cx, cy, w, h` в пикселях входа 640 + 80 оценок классов COCO (уже после sigmoid, objectness нет) |
| Классы | `custom_metadata_map["names"]` модели: 0 person, 2 car, 3 motorcycle, 5 bus, 7 truck, … |

Имена входа/выхода, размер входа и классы `OnnxDetector` читает из модели. Общие функции —
`app/pipeline/detectors/yolo_common.py` (без onnxruntime):
- `letterbox(image_bgr, 640)` → тензор + `LetterboxInfo` (масштаб с сохранением пропорций, паддинг 114, BGR→RGB, `/255`, CHW);
- `postprocess(output, info, names, conf_threshold, iou_threshold, class_ids)` → транспонирование, лучший класс,
  порог, фильтр классов, `cxcywh→xyxy`, NMS по классам, снятие паддинга и масштаба, обрезка, доли `0..1`.

После квантования под LinQ выход может прийти в int8: его нужно деквантовать до float32 той же формы
(`LinqDetector._dequantize`), дальше — тот же `postprocess`.

```bash
python tools/export_detections.py shift.mp4 -o dets_onnx.jsonl --detector onnx --fps 10   # для replay и сравнения
python tools/validate_detections.py dets_onnx.jsonl
python tools/benchmark_detector.py shift.mp4 --detector onnx --frames 200 --out bench_onnx.json
```
`benchmark_detector.py` делает прогрев и выдаёт avg/p50/p95 инференса, пре- и постпроцессинга и FPS; в JSON
записывается `detector_kind` — замер `onnx_cpu` нельзя выдавать за замер LinQ.

## Трекер

После детектора идёт трекер (`app/pipeline/tracker.py`), выбор — `TRACKER`:
- `bytetrack` (по умолчанию) — фильтр Калмана и двухэтапное сопоставление: уверенные детекции
  (≥ `TRACK_HIGH_THRESH` 0.5) и слабые (≥ `TRACK_LOW_THRESH` 0.1) продлевают идущие треки. Новый трек
  подтверждается после `TRACK_MIN_HITS` (3) попаданий, потерянный живёт `TRACK_BUFFER_S` (2 с) видео.
  Этап 1b: если рамки уже не пересекаются (бег при низкой частоте кадров), детекция сопоставляется
  по расстоянию центров в ростах трека;
- `iou` — прежний жадный трекер, для сравнения.

Поэтому детектор отдаёт и **слабые** детекции: `DETECTOR_CONF_THRESHOLD=0.1`. Слабые отсекает
`min_confidence` правил. Если `TRACK_BUFFER_S` меньше `gap_tolerance_s` правил, буфер увеличивается
с предупреждением в логе.

`Track` (то, что видит сценарий): `track_id`, `label`, `bbox` (у ByteTrack — сглаженный), `confidence`,
`first_seen_ts`, `last_seen_ts`, `hits`, `state` (`tentative` / `confirmed` / `lost`), `history` — точки ног
за ~3 с в пикселях исходного кадра (`TrackPoint(video_ts, foot_x, foot_y, bbox_h)`). **В правила
попадают только `confirmed`.**

## Интерфейс сценария — дополнения
- `ScenarioRule.track_info()` — что сценарий знает о треках на кадре (например скорость) для живого просмотра.
- `ScenarioRule.finished()` — `(track_id, zone_id)` нарушений, закончившихся по логике сценария:
  EpisodeManager завершает их эпизоды сразу, не дожидаясь `gap_tolerance_s`.
- `confirms_itself = True` — сценарий сам выдерживает окно подтверждения (гистерезис, как у бега)
  и шлёт hit только для подтверждённого нарушения с `extra["start_ts"]`.
- `RuleHit.extra` — данные для события и кадра-доказательства: `details` (уходит в `event.details`),
  `label` (крупная подпись у нарушителя), `trail` (рисовать хвост траектории ног).
- `requires_zone = False` и правило без `zone_id` — сценарий получает пустой список зон (весь кадр).
