import os,json
from pathlib import Path
CONFIG_PATH=Path(os.getenv('PROJECT_CONFIG','/app/project.json'))
if not CONFIG_PATH.exists():
 CONFIG_PATH=Path(__file__).resolve().parents[2]/'project.json'
CONFIG=json.loads(CONFIG_PATH.read_text())
DATABASE_URL=os.environ['DATABASE_URL']
REDIS_URL=os.getenv('REDIS_URL','redis://redis:6379/0')
SECRET=os.environ['JWT_SECRET']
# 本站标识：跨站结算库合并时用于区分报告/交付原号空间。
STATION_NAME=os.getenv('STATION_NAME') or 'main'
