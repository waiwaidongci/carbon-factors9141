import os
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

# 收集阶段导入 src 时需要有配置；每个用例再通过 fixture 换成自己的数据库。
os.environ.setdefault('PROJECT_CONFIG',str(ROOT/'project.json'))
os.environ.setdefault('DATABASE_URL','sqlite://')
os.environ.setdefault('JWT_SECRET','test-secret')
os.environ.setdefault('STATION_NAME','station-a')


@pytest.fixture()
def ledger(tmp_path,monkeypatch):
    """Each test gets an isolated ledger module bound to a fresh SQLite database."""
    import json, os
    cfg=tmp_path/'project.json'
    cfg.write_text((ROOT/'project.json').read_text(),encoding='utf-8')
    monkeypatch.setenv('PROJECT_CONFIG',str(cfg))
    monkeypatch.setenv('DATABASE_URL',f"sqlite:///{tmp_path/'ledger.db'}")
    monkeypatch.setenv('JWT_SECRET','test-secret')
    monkeypatch.setenv('STATION_NAME','station-a')
    for name in [m for m in sys.modules if m=='src' or m.startswith('src.')]:
        del sys.modules[name]
    from src.config.database import Base,engine,Session
    from src.models import identity,source,run
    Base.metadata.create_all(engine)
    return {'engine':engine,'Session':Session,'models':run,'config':json.loads(cfg.read_text())}
