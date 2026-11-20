"""跨站合并的 HTTP 级验收：两个独立站点通过真实 API 导出/导入。"""
import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('PROJECT_CONFIG', str(ROOT / 'project.json'))


class FakeRedis:
    """测试用队列桩：替代真实 Redis，其余 API 走真实实现。"""
    def __init__(self):
        self.items = []

    def rpush(self, key, value):
        self.items.append((key, value))

    def lpop(self, key):
        for i, (k, v) in enumerate(self.items):
            if k == key:
                return self.items.pop(i)[1]
        return None

    def ping(self):
        return True


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    import redis
    fake = FakeRedis()
    monkeypatch.setattr(redis, 'Redis', type('R', (), {'from_url': staticmethod(lambda url, **kw: fake)}))
    return fake


def load_app(tmp_path, station, fake):
    """独立加载一个站点进程的模块图，并返回其会话工厂与算法。"""
    db_path = tmp_path / f'{station}.db'
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
    os.environ['PROJECT_CONFIG'] = str(ROOT / 'project.json')
    os.environ['JWT_SECRET'] = 'test-secret'
    os.environ['STATION_NAME'] = station
    for name in [m for m in list(sys.modules) if m == 'src' or m.startswith('src.')]:
        del sys.modules[name]
    import redis
    redis.Redis = type('PatchedRedis', (), {'from_url': staticmethod(lambda url, **kw: fake)})
    from src.config.database import Base, engine, Session
    from src.models import identity, source, run
    Base.metadata.create_all(engine)
    import scripts.seed_demo as seed_demo
    importlib.reload(seed_demo)
    seed_demo.seed()
    import src.main as main
    from src.services.calculation import calculate
    from src.services.delivery import tick
    return {'app': main.app, 'Session': Session, 'calculate': calculate, 'tick': tick, 'redis': redis, 'fake': fake}


def login(client, email='admin@demo.local'):
    r = client.post('/auth/login', json={'email': email, 'password': 'Demo2026!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def run_worker_once(ctx):
    """执行一次 worker 循环：完成本站排队中的核算任务，再推进到期交付。"""
    from src.models.run import Run
    with ctx['Session']() as db:
        for job in db.query(Run).filter(Run.status == 'queued').all():
            job.result = ctx['calculate'](job.snapshot['sources'], job.snapshot['policy'])
            job.status = 'completed'
        db.commit()
    with ctx['Session']() as db:
        ctx['tick'](db)


def make_report_and_delivery(client, headers, ctx, name):
    """Create → worker → approve → schedule → worker tick: one sent delivery."""
    policies = client.get('/policies', headers=headers).json()
    submit = client.post('/runs', headers=headers, json={'name': name, 'policy_id': policies[0]['id'], 'period': '2026-11'})
    assert submit.status_code == 200, submit.text
    run_id = submit.json()['id']
    run_worker_once(ctx)
    run = client.get(f'/runs/{run_id}', headers=headers).json()
    assert run['status'] == 'completed', run
    approve = client.post(f'/runs/{run_id}/approve', headers=headers, json={'note': '已核对十一月口径与部门透视'})
    assert approve.status_code == 200, approve.text
    schedule = client.post('/schedules', headers=headers, json={'name': name + '计划', 'run_id': run_id, 'interval_seconds': 60})
    assert schedule.status_code == 200, schedule.text
    from src.models.run import Schedule
    with ctx['Session']() as db:
        sc = db.get(Schedule, schedule.json()['id'])
        sc.next_at = time.time() - 1
        db.commit()
    run_worker_once(ctx)
    return run_id


@pytest.fixture()
def stations(tmp_path):
    ctx_a = load_app(tmp_path, 'station-a', FakeRedis())
    client_a = TestClient(ctx_a['app'])
    ctx_b = load_app(tmp_path, 'station-b', FakeRedis())
    client_b = TestClient(ctx_b['app'])
    return {'a': client_a, 'b': client_b, 'ctx': {'station-a': ctx_a, 'station-b': ctx_b}}


def test_two_station_merge_round_trip(stations):
    a, b = stations['a'], stations['b']
    ha = login(a); hb = login(b)

    run_a = make_report_and_delivery(a, ha, stations['ctx']['station-a'], 'A站十一月核算')
    run_b = make_report_and_delivery(b, hb, stations['ctx']['station-b'], 'B站十一月核算')
    assert run_a == run_b == 1  # 两站原号相同，正是撞号场景

    pkg = a.get('/ledger/export', headers=ha)
    assert pkg.status_code == 200, pkg.text
    package = pkg.json()
    assert len(package['reports']) == 1 and len(package['deliveries']) == 1
    assert package['reports'][0]['approved'] is True

    before_b_runs = len(b.get('/runs', headers=hb).json())
    imported = b.post('/ledger/import', headers=hb, json=package)
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body['status'] == 'completed' and body['counts'] == {'reports': 1, 'deliveries': 1, 'reused_reports': 0, 'reused_deliveries': 0}

    runs_b = b.get('/runs', headers=hb).json()
    assert len(runs_b) == before_b_runs + 1
    imported_run = [r for r in runs_b if r['station'] == 'station-a'][0]
    assert imported_run['id'] != run_b and imported_run['origin_id'] == run_a
    assert imported_run['approved'] and imported_run['checksum'] == package['reports'][0]['checksum']

    deliveries_b = b.get('/deliveries', headers=hb).json()
    imported_delivery = [d for d in deliveries_b if d['station'] == 'station-a'][0]
    assert imported_delivery['run_id'] == imported_run['id']       # 交付指向同站报告新编号
    assert imported_delivery['schedule_id'] is None               # 迁入交付不挂计划、不补发
    assert len(b.get('/schedules', headers=hb).json()) == 1       # 只有 B 站自己的计划

    dl = b.get(f"/deliveries/{imported_delivery['id']}/csv", headers=hb)
    assert dl.status_code == 200 and '排放' in dl.text

    # 再导一次同一包：无新增副本。
    again = b.post('/ledger/import', headers=hb, json=package)
    assert again.json()['status'] == 'already-imported'
    assert len(b.get('/runs', headers=hb).json()) == before_b_runs + 1
    assert len(b.get('/deliveries', headers=hb).json()) == len(deliveries_b)


def test_import_conflict_rejects_package_at_api(stations):
    a, b = stations['a'], stations['b']
    ha = login(a); hb = login(b)
    make_report_and_delivery(a, ha, stations['ctx']['station-a'], 'A站十一月核算')
    package = a.get('/ledger/export', headers=ha).json()
    # B 站先占用来源站 station-a 的原号 1，但内容不同。
    make_report_and_delivery(b, hb, stations['ctx']['station-b'], 'B站十一月核算')
    from src.models.run import Run
    with stations['ctx']['station-b']['Session']() as db:
        clash = db.get(Run, 1)
        clash.station = 'station-a'; clash.origin_id = 1
        db.commit()
    before = b.get('/ledger/origins', headers=hb).json()
    r = b.post('/ledger/import', headers=hb, json=package)
    assert r.status_code == 422 and '内容不同' in r.json()['detail']
    after = b.get('/ledger/origins', headers=hb).json()
    assert after == before


def test_import_requires_admin_and_export_is_admin_only(stations):
    a, b = stations['a'], stations['b']
    ha = login(a)
    viewer_a = login(a, 'viewer@demo.local')
    viewer_b = login(b, 'viewer@demo.local')
    package = a.get('/ledger/export', headers=ha).json()
    assert b.post('/ledger/import', headers=viewer_b, json=package).status_code == 403
    assert a.get('/ledger/export', headers=viewer_a).status_code == 403
    assert b.get('/ledger/origins', headers=viewer_b).status_code == 200


def test_local_records_get_nonconflicting_origin_after_import(stations):
    a, b = stations['a'], stations['b']
    ha = login(a); hb = login(b)
    make_report_and_delivery(a, ha, stations['ctx']['station-a'], 'A站十一月核算')
    package = a.get('/ledger/export', headers=ha).json()
    # B 站先导入 A#1（本站原号 1 空着），再新建本站报告。
    assert b.post('/ledger/import', headers=hb, json=package).status_code == 200
    new_id = make_report_and_delivery(b, hb, stations['ctx']['station-b'], 'B站十二月核算')
    runs = b.get('/runs', headers=hb).json()
    own = [r for r in runs if r['station'] == 'station-b']
    imported = [r for r in runs if r['station'] == 'station-a']
    assert len(own) == 1 and len(imported) == 1
    # 本站记录原号保持为自身编号；来源站不同，原号空间互不覆盖。
    assert own[0]['origin_id'] == own[0]['id']
    assert imported[0]['origin_id'] == package['reports'][0]['origin_id']
    assert {r['station'] for r in b.get('/runs', headers=hb).json()} == {'station-a', 'station-b'}
