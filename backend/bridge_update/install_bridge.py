"""Run this installer using the backend virtual environment."""
from pathlib import Path
from datetime import datetime
import shutil

base=Path.cwd()
config=base/'app/config.py'
factory=base/'app/pipeline/detectors/__init__.py'
if not config.is_file() or not factory.is_file():
    raise SystemExit('Run this script from the backend folder.')
config_text=config.read_text(encoding='utf-8-sig')
factory_text=factory.read_text(encoding='utf-8-sig')
if '    linq_bridge_url:' not in config_text:
    anchor='    linq_program: str = ""'
    if anchor not in config_text:raise SystemExit('Config differs; no files changed.')
    config_text=config_text.replace(anchor,anchor+'\n    linq_bridge_url: str = ""\n    linq_jupyter_token: str = ""',1)
if 'LinqBridgeDetector' not in factory_text:
    anchor='    if kind == "linq":\n'
    if anchor not in factory_text:raise SystemExit('Factory differs; no files changed.')
    factory_text=factory_text.replace(anchor,anchor+'''        if settings.linq_bridge_url:
            from app.pipeline.detectors.linq_bridge import LinqBridgeDetector

            return LinqBridgeDetector(settings)
''',1)
source=Path(__file__).resolve().parent/'linq_bridge.py'
if not source.is_file():raise SystemExit('linq_bridge.py missing next to installer')
backup=base/('linq_backup_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
backup.mkdir()
for p in (config,factory,base/'app/pipeline/detectors/linq_bridge.py'):
    if p.exists():
        target=backup/p.relative_to(base);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(p,target)
config.write_text(config_text,encoding='utf-8')
factory.write_text(factory_text,encoding='utf-8')
shutil.copy2(source,base/'app/pipeline/detectors/linq_bridge.py')
print('Bridge adapter installed. Backup:',backup)
print('Restart backend after bridge v2 is running in Jupyter.')
print('Rollback: clear LINQ_BRIDGE_URL in .env and restart backend (direct RabbitMQ).')
