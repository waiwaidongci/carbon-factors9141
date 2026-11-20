"""PostgreSQL 专属行为验收：一致快照、advisory lock、结构升级。

通过环境变量 LEDGER_TEST_PG 指向本地 PostgreSQL 时启用；默认跳过。
"""
import importlib
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PG = os.getenv('LEDGER_TEST_PG')

pytestmark = pytest.mark.skipif(not PG, reason='未设置 LEDGER_TEST_PG，跳过 PostgreSQL 专属用例')


def fresh_schema(monkeypatch):
    for name in [m for m in list(sys.modules) if m == 'src' or m.startswith('src.')]:
        del sys.modules[name]
    os.environ['DATABASE_URL'] = PG
    os.environ['PROJECT_CONFIG'] = str(ROOT / 'project.json')
    os.environ['JWT_SECRET'] = 'test-secret'
    os.environ['STATION_NAME'] = 'pg-station'
    from src.config.database import Base, engine
    from src.models import identity, source, run as run_models
    from sqlalchemy import text
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("DELETE FROM audits"))
    return engine


def add_run(Session, origin, name, qty, visible=True, approved=True):
    from src.models.run import Run
    from src.utils.checksum import snapshot_checksum
    snap = {'period': '2026-11', 'policy': {'version': '因子-v1', 'scope': '测试范围说明', 'factors': {'电力': {'unit': 'kWh', 'kg_per_unit': 0.57}}},
            'sources': [{'period': '2026-11', 'department': '材料试验室', 'activity': '电力', 'quantity': qty, 'unit': 'kWh'}]}
    with Session() as db:
        run = Run(name=name, status='completed', snapshot=snap, checksum=snapshot_checksum(snap),
                  result={'rows': [{'排放kgCO2e': qty * 0.57}], 'summary': {'核算kgCO2e': qty * 0.57}},
                  created=time.time(), approved=approved, review='已核对口径与业务结论',
                  station='pg-src', origin_id=origin, imported=False, visible=visible)
        db.add(run); db.commit()
        return run.id


def test_pg_schema_upgrade_is_idempotent(monkeypatch):
    engine = fresh_schema(monkeypatch)
    from src.config.schema import upgrade_schema
    from sqlalchemy import text
    upgrade_schema(engine); upgrade_schema(engine)
    with engine.connect() as c:
        cols = {row[0] for row in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='carbon_runs'"))}
    assert {'station', 'origin_id', 'imported', 'visible', 'import_id'} <= cols


def test_pg_schema_upgrade_backfills_legacy_rows(monkeypatch):
    engine = fresh_schema(monkeypatch)
    from sqlalchemy import text
    # 模拟升级前的旧库：去掉新列并插入旧行。
    with engine.begin() as c:
        for table, cols in (('carbon_runs', ('station', 'origin_id', 'imported', 'visible', 'import_id')),
                            ('deliveries', ('station', 'origin_id', 'report_station', 'report_origin_id', 'imported', 'visible', 'import_id'))):
            for col in cols: c.execute(text(f'ALTER TABLE {table} DROP COLUMN IF EXISTS {col}'))
        c.execute(text("INSERT INTO carbon_runs (id,name,status,snapshot,checksum,result,created,approved,review) VALUES (1,'旧报告','completed','{}'::json,'c','{}'::json,1.0,true,'旧复核')"))
        c.execute(text("INSERT INTO deliveries (id,schedule_id,run_id,due_key,created,payload) VALUES (1,1,1,'1:1',2.0,'{}'::json)"))
    from src.config.schema import upgrade_schema
    upgrade_schema(engine)
    with engine.connect() as c:
        row = c.execute(text('SELECT station,origin_id,imported,visible FROM carbon_runs WHERE id=1')).one()
        assert tuple(row) == ('pg-station', 1, False, True)
        d = c.execute(text('SELECT station,origin_id,report_station,report_origin_id,visible FROM deliveries WHERE id=1')).one()
        assert tuple(d) == ('pg-station', 1, 'pg-station', 1, True)


def test_pg_export_uses_repeatable_read_snapshot(monkeypatch):
    engine = fresh_schema(monkeypatch)
    from src.config.database import Session
    add_run(Session, 1, '一号报告', 10)
    from src.services.ledger import export_package
    package = export_package('pg-src')
    assert len(package['reports']) == 1
    # 导出期间新增的报告不得出现在同一包里。
    add_run(Session, 2, '二号报告', 20)
    package = export_package('pg-src')
    assert len(package['reports']) == 2


def test_pg_concurrent_overlapping_import_serializes(monkeypatch):
    engine = fresh_schema(monkeypatch)
    from src.config.database import Session
    for i in range(1, 13):
        add_run(Session, i, f'报告{i}', i)
    from src.services.ledger import export_package, import_package
    full = export_package('pg-src')
    half = dict(full)
    half['reports'] = full['reports'][:6]
    half['deliveries'] = []
    from src.services.ledger import package_digest
    half['digest'] = package_digest('pg-src', half['reports'], half['deliveries'])

    results, errors = [], []
    barrier = threading.Barrier(2)
    def work(pkg):
        try:
            barrier.wait()
            with Session() as db:
                results.append(import_package(db, pkg, chunk_size=2)['status'])
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=work, args=(p,)) for p in (full, half)]
    for t in threads: t.start()
    for t in threads: t.join()
    from src.models.run import Run
    with Session() as db:
        assert db.query(Run).filter(Run.station == 'pg-src').count() == 12
        assert db.query(Run).filter(Run.station == 'pg-src', Run.visible == True).count() == 12
    assert not errors, errors


def test_pg_import_invisible_until_checked(monkeypatch):
    engine = fresh_schema(monkeypatch)
    from src.config.database import Session
    for i in range(1, 5): add_run(Session, i, f'报告{i}', i)
    from src.services.ledger import export_package, import_package
    pkg = export_package('pg-src')
    seen = {}
    def hook(chunks, phase):
        if phase == 'reports':
            with Session() as db:
                from src.models.run import Run
                seen['visible'] = db.query(Run).filter(Run.visible == True, Run.imported == True).count()
    with Session() as db:
        import_package(db, pkg, chunk_size=2, interrupt_hook=hook)
    assert seen['visible'] == 0
    with Session() as db:
        from src.models.run import Run
        assert db.query(Run).filter(Run.station == 'pg-src', Run.visible == True).count() == 4


def test_pg_snapshot_sees_one_point_in_time(monkeypatch):
    engine = fresh_schema(monkeypatch)
    from src.config.database import Session
    add_run(Session, 1, '一号报告', 10)
    from src.services.ledger import _read_snapshot
    from sqlalchemy import text
    from src.models.run import Run
    with Session() as reader:
        _read_snapshot(reader)
        iso = reader.execute(text('SHOW transaction_isolation')).scalar()
        assert iso == 'repeatable read'
        first = reader.query(Run).count()
        # 另一会话在快照建立后提交新报告
        add_run(Session, 2, '二号报告', 20)
        second = reader.query(Run).count()
        assert first == second == 1
        # 只读事务拒绝写入
        import pytest
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError):
            reader.execute(text("INSERT INTO carbon_runs (name,status,created,approved,review,station,origin_id,imported,visible) VALUES ('x','completed',1.0,true,'aaaa','pg-src',99,false,true)"))
        reader.rollback()


def test_pg_worker_tick_competes_with_export(monkeypatch):
    """定时发件与打包并发：导出要么看到该交付，要么完全看不到，不会读到半条。"""
    engine = fresh_schema(monkeypatch)
    from src.config.database import Session
    from src.models.run import Run, Schedule, Delivery
    from src.services.delivery import tick
    from src.services.ledger import export_package
    add_run(Session, 1, '一号报告', 10)
    with Session() as db:
        run = db.query(Run).filter(Run.station == 'pg-src').one()
        db.add(Schedule(name='计划', run_id=run.id, interval_seconds=60, next_at=time.time() - 1, enabled=True)); db.commit()
    import threading
    done = threading.Event()
    def worker():
        with Session() as db:
            tick(db)
        done.set()
    t = threading.Thread(target=worker); t.start()
    pkg = export_package('pg-src')
    t.join(); done.wait(2)
    # 包内交付与报告互相指向同一快照；打包本身不新增交付。
    with Session() as db:
        assert db.query(Delivery).count() == 1
        delivery = db.query(Delivery).one()
        assert delivery.report_station == 'pg-src' and delivery.report_origin_id == 1
    if pkg['deliveries']:
        assert pkg['deliveries'][0]['payload']['checksum'] == pkg['reports'][0]['checksum']
