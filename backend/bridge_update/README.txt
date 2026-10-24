1. Upload linq_bridge_server.py to Jupyter /work/linq_yolov8s. Stop previous port 8787 service and run /opt/conda/bin/python -u /home/hackathon76167/work/linq_yolov8s/linq_bridge_server.py
2. Place bridge_update folder inside local backend. Stop backend. From backend run .venv\Scripts\python.exe bridge_update\install_bridge.py
3. Run .venv\Scripts\python.exe bridge_update\check_bridge.py
4. Restart backend normally. Keep VPN and Jupyter server process running.
Requires existing LINQ_BRIDGE_URL and LINQ_JUPYTER_TOKEN in .env. Never share token.
JPEG quality=90; remote thresholds follow project settings. Syntax and mocked HTTP tested; actual end-to-end deployment must be verified.
Rollback: clear LINQ_BRIDGE_URL and restart backend.
