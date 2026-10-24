# LinQ: что сделать человеку (VPN, Jupyter, SDK)

Эти шаги требуют VPN, логинов или скачивания, которое с моей машины не идёт. Всё остальное уже готово:
пре- и постпроцессинг, калибровочный набор, сравнение с ONNX, сводка замеров, notebook, replay в backend.
Логины и пароли — только в `backend/.env` (`LINQ_*`) или прямо в Jupyter. В git, логи и отчёты их не писать.

## 1. Скачать SDK и пример API (без VPN, через браузер)
Ссылки из «Процедура_подготовки…pdf»:
- Framework: https://cloud.lin-q.ru/s/bEqr922zr6NXHy4 → `tpu_framework_2026.9.45.zip` (505 МБ)
- API: https://cloud.lin-q.ru/s/QcFLdkdLb71BOmm → `usage_example_python_api.zip` (22 МБ)

Положить в `linq/downloads/Framework/` и `linq/downloads/API/`. Можно скриптом `python linq/fetch_materials.py`
(докачивает), но у меня сервер отдавал ~100 байт/с. Попробуйте из браузера или с включённым VPN.

Потом распакуйте в `linq/extracted/` и скажите мне: я прочитаю `tpu_framework.pdf` и пример API, заполню
[LINQ_SDK_NOTES.md](LINQ_SDK_NOTES.md), `linq/compile.py` и `LinqDetector._run`.

## 2. Окружение SDK
По требованиям `tpu_framework.pdf` (ОС, версия Python). Отдельно от backend:
```bash
python -m venv linq/.venv-sdk          # или в WSL2, если SDK только под Linux
linq/.venv-sdk/bin/pip install <пакеты из tpu_framework>
```
Если нужен Linux на Windows: `wsl --install -d Ubuntu-22.04` (версию уточнить по PDF), проект доступен
в WSL как `/mnt/c/Users/<вы>/linq-hackathon`.

## 3. Проверить модель и скомпилировать программу TPU
```bash
linq/.venv-tools/Scripts/python linq/check_onnx_ops.py       # операции графа; список поддерживаемых
                                                             # из PDF — в linq/supported_ops.txt
backend/.venv/Scripts/python linq/prepare_calibration.py     # уже сделано: linq/calibration/ (199 кадров)
backend/.venv/Scripts/python linq/compile.py                 # каркас: сюда впишем команды SDK
```
Обратите внимание на квантование выхода: см. «Главный риск» в LINQ_SDK_NOTES.md.
Результат — `linq/build/<имя>.tpu` и `linq/build/manifest.json`.

## 4. Прогнать ролики на TPU в Jupyter
1. Подключить VPN (конфиг `Hakathon.ovpn` из архива, логин/пароль капитана).
2. Открыть http://hackathon.lin-q.ru/, войти логином команды.
3. Загрузить (Upload):
   - `linq/build/<имя>.tpu`;
   - `linq/notebooks/run_linq_detections.ipynb`;
   - ролики `0_te1.mp4`, `0_te21.mp4`, `person15_running_d1_uncomp.avi`.
4. В notebook:
   - задать `LOGIN` и `PROGRAM`;
   - **заполнить ячейку «Вызов TPU»** по `examples/example.ipynb`: загрузка программы, вход, выход,
     `OUTPUT_QUANT`, если выход int8;
   - выполнить все ячейки.
5. Скачать `dets_linq_<ролик>.jsonl` и положить в `backend/data/videos/linq/`.

## 5. Проверить качество и скорость
```bash
cd backend
python tools/export_detections.py data/storage/videos/0_te1.mp4 -o data/videos/linq/dets_onnx_te1.jsonl --fps 10
python ../linq/compare_with_onnx.py --onnx data/videos/linq/dets_onnx_te1.jsonl --linq data/videos/linq/dets_linq_te1.jsonl --name te1
python ../linq/benchmark_linq.py data/videos/linq/dets_linq_*.jsonl
```
Результаты внесите в `docs/BENCHMARKS.md` — колонку LinQ TPU.

## 6. Показать в интерфейсе как «LinQ TPU (запись)»
В `backend/.env`:
```
DETECTOR=replay
REPLAY_PATH=./data/videos/linq/dets_linq_te1.jsonl
```
Запустите источник с тем же роликом. Детекции, посчитанные на TPU, проигрываются, а в шапке, на мониторинге и
в статистике видно «LinQ TPU (запись)» (`detector_kind = linq_tpu_replay`) с замерами из файла.

## 7. Если API доступен с ноутбука через VPN
Тогда `DETECTOR=linq` работает вживую. После шага 1 я реализую `LinqDetector._run` по примеру API,
а параметры подключения будут читаться из `.env` (`LINQ_API_URL`, `LINQ_LOGIN`, `LINQ_PASSWORD`, `LINQ_PROGRAM` —
имена уточню по примеру).
