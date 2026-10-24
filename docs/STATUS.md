# Статус работ (обновляется после каждого этапа)

Последнее обновление: 2026-10-24, все этапы задания пройдены, кроме реальной компиляции и запуска на LinQ.

## Сделано
- [x] 1. Инвентаризация видео: `backend/tools/video_inventory.py`, `backend/data/videos/{inventory,manifest,labels}.json`,
      контактные листы `previews/`. Все 4 ролика — H.264/FMP4, читаются OpenCV, поворота нет — конвертация не нужна.
- [x] A1. Материалы: `LinQ.zip` → `linq/extracted/`. В архиве только PDF и конфиг VPN; SDK и API — по ссылкам
      `cloud.lin-q.ru`, листинг через `linq/fetch_materials.py`. Заметки — `docs/LINQ_SDK_NOTES.md`.
- [x] A3/A4 без SDK: `check_onnx_ops.py` (отчёт `linq/reports/onnx_ops.md`), `prepare_calibration.py` (199 кадров),
      `export_onnx.py`, `compile.py` (каркас + `build/manifest.json`).
- [x] A5 частично: `LinqDetector` с quantize/dequantize и kind `linq_tpu`, понятные ошибки; вызов TPU — TODO.
- [x] A6: notebook `linq/notebooks/run_linq_detections.ipynb`, replay с `detector_kind` → «LinQ TPU (запись)».
- [x] A7 без TPU: `compare_with_onnx.py`, `benchmark_linq.py`; ONNX CPU замерен по всем роликам.
- [x] B1/B2: свой ByteTrack + этап 1b, `TRACKER=bytetrack|iou`; сравнение на роликах, рендеры и скриншоты.
- [x] B3: живые треки в канале `live.frame` (track_state, flags, speed, detector_kind, ≤ 8 Гц); фронт: цвет по ID,
      интерполяция 120 мс, lost полупрозрачно, панель треков, бейдж детектора.
- [x] C1–C6: сценарий `run_fast_movement`, кадр с траекторией и кириллицей, калибровка порога (1.5),
      фронт (журнал, карточка, статистика, правило на весь кадр), MockRunDetector, тесты.
- [x] 5: `tools/evaluate_episodes.py`, `tools/seed.py --videos`, README «Демо», сквозная проверка в браузере.

## Блокеры
- SDK `tpu_framework_2026.9.45.zip` (505 МБ) и `usage_example_python_api.zip` (22 МБ) не скачиваются с этой
  машины (~100 байт/с). Нужен человек: docs/LINQ_TODO_FOR_HUMAN.md, шаг 1.
- Инференс в Jupyter требует VPN и логина капитана — шаги 4–6 там же.
