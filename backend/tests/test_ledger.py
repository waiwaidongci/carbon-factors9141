import copy, json
from types import SimpleNamespace
import pytest

from src.models.run import Run, Delivery, LedgerImport


def make_snapshot(period='2026-11', qty=10.0, version='因子-v1'):
    return {'period': period,
            'policy': {'version': version, 'scope': '合成研究用活动量；仅覆盖电力、柴油、通勤', 'factors': {'电力': {'unit': 'kWh', 'kg_per_unit': 0.57}}},
            'sources': [{'period': period, 'department': '材料试验室', 'activity': '电力', 'quantity': qty, 'unit': 'kWh'}]}


def make_report(station, origin, qty=10.0, approved=True, review='已核对口径与业务结论'):
    from src.services.ledger import report_content, report_fingerprint
    from src.utils.checksum import snapshot_checksum
    snapshot = make_snapshot(qty=qty)
    result = {'summary': {'活动记录数': 1, '核算kgCO2e': qty * 0.57, '因子版本': '因子-v1'},
              'rows': [{'月份': '2026-11', '部门': '材料试验室', '活动': '电力', '排放kgCO2e': qty * 0.57}],
              'method': 'Decimal 换算', 'warnings': []}
    content = {'station': station, 'origin_id': origin, 'name': f'{station}月报{origin}', 'status': 'completed',
               'snapshot': snapshot, 'checksum': snapshot_checksum(snapshot), 'result': result, 'error': None,
               'created': 1700000000.0 + origin, 'approved': approved, 'review': review}
    return {**content, 'fingerprint': report_fingerprint(content)}


def make_delivery(report_raw, origin, created=1700000100.0):
    from src.services.ledger import delivery_content, delivery_fingerprint
    content = {'station': report_raw['station'], 'origin_id': origin, 'created': created,
               'payload': {'name': report_raw['name'], 'checksum': report_raw['checksum'],
                           'result': report_raw['result'], 'review': report_raw['review'],
                           'policy': report_raw['snapshot']['policy']},
               'report_station': report_raw['station'], 'report_origin_id': report_raw['origin_id']}
    return {**content, 'fingerprint': delivery_fingerprint(content)}


def make_package(source_station, reports, deliveries):
    from src.services.ledger import package_digest
    reports = sorted(reports, key=lambda r: (r['station'], r['origin_id']))
    deliveries = sorted(deliveries, key=lambda d: (d['station'], d['origin_id']))
    return {'format': 'tangyuan-carbon-ledger', 'version': 1, 'source_station': source_station,
            'exported_at': 1700000200.0, 'reports': reports, 'deliveries': deliveries,
            'digest': package_digest(source_station, reports, deliveries)}


def counts(engine):
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        return db.query(Run).count(), db.query(Delivery).count()


def visible_counts(engine):
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        return db.query(Run).filter(Run.visible == True).count(), db.query(Delivery).filter(Delivery.visible == True).count()


def seed_station_a(engine):
    """Station A's own ledger: two approved runs, one delivery, one unapproved run."""
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        snap1 = make_snapshot(qty=10.0)
        snap2 = make_snapshot(qty=20.0, period='2026-10')
        from src.utils.checksum import snapshot_checksum
        r1 = Run(name='十一月核算', status='completed', snapshot=snap1, checksum=snapshot_checksum(snap1),
                 result={'summary': {'核算kgCO2e': 5.7}, 'rows': [{'a': 1}]}, created=1700000001.0,
                 approved=True, review='已核对十一月口径', station='station-a', origin_id=1, imported=False, visible=True)
        r2 = Run(name='十月核算', status='completed', snapshot=snap2, checksum=snapshot_checksum(snap2),
                 result={'summary': {'核算kgCO2e': 11.4}, 'rows': [{'a': 2}]}, created=1700000002.0,
                 approved=True, review='已核对十月口径', station='station-a', origin_id=2, imported=False, visible=True)
        r3 = Run(name='未复核草稿', status='completed', snapshot=snap1, checksum=snapshot_checksum(snap1),
                 result={'rows': [{'a': 3}]}, created=1700000003.0, approved=False, review='',
                 station='station-a', origin_id=3, imported=False, visible=True)
        db.add_all([r1, r2, r3]); db.flush()
        db.add(Delivery(schedule_id=7, run_id=r1.id, due_key='7:1700000100', created=1700000100.0,
                        payload={'name': r1.name, 'checksum': r1.checksum, 'result': r1.result,
                                 'review': r1.review, 'policy': snap1['policy']},
                        station='station-a', origin_id=1, report_station='station-a', report_origin_id=1,
                        imported=False, visible=True))
        db.commit()


def test_export_only_reviewed_reports_and_sent_deliveries(ledger):
    seed_station_a(ledger['engine'])
    from src.services.ledger import export_package
    pkg = export_package('station-a')
    assert [r['origin_id'] for r in pkg['reports']] == [1, 2]
    assert [d['origin_id'] for d in pkg['deliveries']] == [1]
    assert all(r['approved'] and r['status'] == 'completed' for r in pkg['reports'])
    assert pkg['digest'] == make_package('station-a', pkg['reports'], pkg['deliveries'])['digest']
    # 打包只读：不新增交付，也不改变可见性。
    assert counts(ledger['engine']) == (3, 1)


def test_export_is_repeatable_and_ignores_staged_rows(ledger):
    seed_station_a(ledger['engine'])
    from src.services.ledger import export_package
    with ledger['Session']() as db:
        db.add(Run(name='暂存', status='completed', snapshot=make_snapshot(), checksum='x', result={'rows': []},
                   created=1.0, approved=True, review='已核对', station='station-b', origin_id=9,
                   imported=True, visible=False, import_id=1))
        db.commit()
    first = export_package('station-a')
    second = export_package('station-a')
    assert first['digest'] == second['digest']
    assert first['reports'] == second['reports'] and first['deliveries'] == second['deliveries']
    assert all(r['station'] == 'station-a' for r in first['reports'])


def test_import_assigns_new_numbers_and_keeps_origin_and_content(ledger):
    reports = [make_report('station-a', 1, qty=10.0), make_report('station-a', 2, qty=20.0)]
    deliveries = [make_delivery(reports[0], 1)]
    pkg = make_package('station-a', reports, deliveries)
    # 目标站已有同号报告（原号 1），来自另一个站，故不冲突。
    with ledger['Session']() as db:
        snap = make_snapshot(qty=99)
        from src.utils.checksum import snapshot_checksum
        local = Run(name='本站一号', status='completed', snapshot=snap, checksum=snapshot_checksum(snap),
                    result={'rows': []}, created=1700000000.0, approved=True, review='本站复核',
                    station='station-local', origin_id=1, imported=False, visible=True)
        db.add(local); db.commit()
        local_id = local.id
    from src.services.ledger import import_package
    with ledger['Session']() as db:
        result = import_package(db, pkg, actor='admin@demo.local')
    assert result['status'] == 'completed'
    assert result['counts'] == {'reports': 2, 'deliveries': 1, 'reused_reports': 0, 'reused_deliveries': 0}
    from sqlalchemy.orm import Session
    with Session(ledger['engine']) as db:
        imported = db.query(Run).filter(Run.imported == True).order_by(Run.origin_id).all()
        assert [(r.station, r.origin_id, r.name) for r in imported] == [('station-a', 1, reports[0]['name']), ('station-a', 2, reports[1]['name'])]
        assert all(r.id != 1 and r.visible and r.approved for r in imported)
        assert imported[0].snapshot == pkg['reports'][0]['snapshot']
        assert imported[0].checksum == pkg['reports'][0]['checksum']
        assert imported[1].id != local_id
        delivery = db.query(Delivery).filter(Delivery.imported == True).one()
        assert delivery.origin_id == 1 and delivery.station == 'station-a'
        assert delivery.report_station == 'station-a' and delivery.report_origin_id == 1
        assert delivery.run_id == imported[0].id          # 指向同站报告的新编号
        assert delivery.schedule_id is None                # 迁入交付不建计划
        assert delivery.payload == pkg['deliveries'][0]['payload']  # 已发内容不变
        assert db.query(__import__('src.models.run', fromlist=['Schedule']).Schedule).count() == 0


def test_reimport_same_package_is_idempotent(ledger):
    seed_station_a(ledger['engine'])
    from src.services.ledger import export_package, import_package
    pkg = export_package('station-a')
    with ledger['Session']() as db:
        first = import_package(db, pkg)
    with ledger['Session']() as db:
        second = import_package(db, pkg)
    assert first['status'] == 'completed'
    assert second['status'] == 'already-imported'
    base = counts(ledger['engine'])
    with ledger['Session']() as db:
        assert import_package(db, pkg)['status'] == 'already-imported'
    assert counts(ledger['engine']) == base


def test_identical_existing_records_are_reused_without_duplicates(ledger):
    seed_station_a(ledger['engine'])
    from src.services.ledger import export_package, import_package, report_fingerprint, report_content
    pkg = export_package('station-a')
    with ledger['Session']() as db:
        import_package(db, pkg)
    # 同一站同号同内容再次进入另一个包（新增一张报告），应沿用现有记录。
    extra_report = make_report('station-a', 5)
    extra_delivery = make_delivery(extra_report, 5)
    pkg2 = make_package('station-a', pkg['reports'] + [extra_report], pkg['deliveries'] + [extra_delivery])
    with ledger['Session']() as db:
        result = import_package(db, pkg2)
    assert result['status'] == 'completed'
    assert result['counts']['reports'] == 1
    assert result['counts']['deliveries'] == 1
    assert result['counts']['reused_reports'] == 2
    assert result['counts']['reused_deliveries'] == 1
    assert counts(ledger['engine'])[0] == 4   # 站内 3 张 + 迁入 3 张 = 已有 3 + 新增 1


def test_conflicting_content_rejects_whole_package(ledger):
    seed_station_a(ledger['engine'])
    from src.services.ledger import export_package, import_package, PackageError
    pkg = export_package('station-a')
    with ledger['Session']() as db:
        import_package(db, pkg)
    before = counts(ledger['engine'])
    # 报告 A#1 内容被改写（合法指纹），同号不同内容。
    changed = make_report('station-a', 1, qty=999.0)
    extra = make_report('station-a', 8)
    pkg2 = make_package('station-a', [changed, extra], [])
    with ledger['Session']() as db:
        with pytest.raises(PackageError, match='内容不同'):
            import_package(db, pkg2)
    assert counts(ledger['engine']) == before
    from sqlalchemy.orm import Session
    with Session(ledger['engine']) as db:
        assert db.query(Run).filter(Run.station == 'station-a', Run.origin_id == 8).count() == 0
        assert db.query(LedgerImport).filter(LedgerImport.status == 'rejected').count() == 1


def test_missing_parent_report_rejects_whole_package(ledger):
    from src.services.ledger import import_package, PackageError
    report = make_report('station-b', 1)
    delivery = make_delivery(report, 1)
    orphan = make_delivery(make_report('station-b', 77), 2)
    pkg = make_package('station-b', [report], [delivery, orphan])
    with ledger['Session']() as db:
        with pytest.raises(PackageError, match='缺少同站父报告'):
            import_package(db, pkg)
    assert counts(ledger['engine']) == (0, 0)
    # 缺少父报告在整包校验阶段即拒绝，不写入任何暂存行。
    from sqlalchemy.orm import Session
    with Session(ledger['engine']) as db:
        assert db.query(Run).count() == 0 and db.query(Delivery).count() == 0
        assert db.query(LedgerImport).count() == 0


def test_tampered_package_is_rejected(ledger):
    report = make_report('station-b', 1)
    pkg = make_package('station-b', [report], [])
    pkg['reports'][0]['name'] = '被篡改名称'
    from src.services.ledger import import_package, PackageError
    with ledger['Session']() as db:
        with pytest.raises(PackageError):
            import_package(db, pkg)
    assert counts(ledger['engine']) == (0, 0)


def test_interrupted_import_resumes_without_duplicates(ledger):
    reports = [make_report('station-c', i) for i in range(1, 8)]
    deliveries = [make_delivery(r, r['origin_id']) for r in reports]
    pkg = make_package('station-c', reports, deliveries)
    from src.services.ledger import import_package

    calls = {'n': 0}
    def hook(chunks, phase):
        calls['n'] += 1
        if calls['n'] == 2:
            raise RuntimeError('模拟进程中断')

    with ledger['Session']() as db:
        with pytest.raises(RuntimeError):
            import_package(db, pkg, chunk_size=2, interrupt_hook=hook)
    from sqlalchemy.orm import Session
    with Session(ledger['engine']) as db:
        batch = db.query(LedgerImport).one()
        assert batch.status == 'interrupted'
        assert batch.checkpoint['reports'] == 4
        # 中断时已提交的行仍不可见。
        assert db.query(Run).filter(Run.visible == True).count() == 0

    with ledger['Session']() as db:
        result = import_package(db, pkg, chunk_size=2)
    assert result['status'] == 'completed'
    assert result['counts']['reports'] == 7
    assert result['counts']['deliveries'] == 7
    with Session(ledger['engine']) as db:
        assert db.query(Run).count() == 7
        assert db.query(Delivery).count() == 7
        assert db.query(Run).filter(Run.visible == True).count() == 7
        report_ids = {(r.station, r.origin_id): r.id for r in db.query(Run).all()}
        for delivery in db.query(Delivery).all():
            assert delivery.run_id == report_ids[(delivery.report_station, delivery.report_origin_id)]


def test_overlapping_concurrent_imports_do_not_duplicate(ledger):
    import threading
    reports = [make_report('station-d', i) for i in range(1, 11)]
    deliveries = [make_delivery(r, r['origin_id']) for r in reports]
    big = make_package('station-d', reports, deliveries)
    small = make_package('station-d', reports[:5], deliveries[:5])
    from src.services.ledger import import_package

    errors = []
    barrier = threading.Barrier(2)
    def run(pkg):
        try:
            barrier.wait()
            with ledger['Session']() as db:
                import_package(db, pkg, chunk_size=2)
        except Exception as exc:  # 允许稍后重试类的 busy 错误
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=run, args=(pkg,)) for pkg in (big, small)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not [e for e in errors if e not in ('PackageBusy',)]
    from sqlalchemy.orm import Session
    with Session(ledger['engine']) as db:
        assert db.query(Run).count() == 10
        assert db.query(Delivery).count() == 10
        assert db.query(Run).filter(Run.station == 'station-d', Run.origin_id == 1).count() == 1
        assert db.query(Delivery).filter(Delivery.station == 'station-d', Delivery.origin_id == 1).count() == 1


def test_import_does_not_trigger_resend_through_worker_tick(ledger):
    seed_station_a(ledger['engine'])
    from src.services.ledger import export_package, import_package
    from src.services.delivery import tick
    pkg = export_package('station-a')
    # 目标站没有任何计划；迁入交付也不应新建计划或补发。
    with ledger['Session']() as db:
        from src.models.run import Schedule
        db.query(Schedule).delete()
        db.commit()
    with ledger['Session']() as db:
        import_package(db, pkg)
    with ledger['Session']() as db:
        tick(db)
    from sqlalchemy.orm import Session
    with Session(ledger['engine']) as db:
        assert db.query(__import__('src.models.run', fromlist=['Schedule']).Schedule).count() == 0
        assert db.query(Delivery).count() == 1  # 仅迁入的那一条，不补发


def test_unapproved_reports_cannot_be_packaged(ledger):
    bad = make_report('station-e', 1, approved=False)
    pkg = make_package('station-e', [bad], [])
    from src.services.ledger import import_package, PackageError
    with ledger['Session']() as db:
        with pytest.raises(PackageError, match='未复核'):
            import_package(db, pkg)


def test_local_origin_id_skips_taken_values(ledger):
    from sqlalchemy.orm import Session
    from src.utils.origins import local_origin_id
    from src.models.run import Run
    with Session(ledger['engine']) as db:
        base = dict(status='completed', snapshot=make_snapshot(), checksum='c', result={'rows': []},
                    created=1.0, approved=True, review='已核对口径与业务结论', imported=True)
        db.add_all([
            Run(name='迁入1', station='station-x', origin_id=1, imported=True, visible=True, **{k: v for k, v in base.items() if k != 'imported'}),
            Run(name='迁入2', station='station-x', origin_id=2, imported=True, visible=True, **{k: v for k, v in base.items() if k != 'imported'}),
        ])
        db.commit()
        # 若自增编号恰好撞上同站来源原号，则顺延到未占用值。
        assert local_origin_id(db, Run, 'station-x', 1) == 3
        assert local_origin_id(db, Run, 'station-x', 2) == 3
        assert local_origin_id(db, Run, 'station-x', 4) == 4
        assert local_origin_id(db, Run, 'station-y', 2) == 2


def test_rejected_package_can_retry_after_conflict_resolved(ledger):
    seed_station_a(ledger['engine'])
    from src.services.ledger import export_package, import_package, PackageError
    from sqlalchemy.orm import Session
    pkg = export_package('station-a')
    with ledger['Session']() as db:
        import_package(db, pkg)
    conflicting = make_report('station-a', 1, qty=999.0)
    extra = make_report('station-a', 9)
    pkg2 = make_package('station-a', [conflicting, extra], [])
    with ledger['Session']() as db:
        with pytest.raises(PackageError, match='内容不同'):
            import_package(db, pkg2)
    # 冲突解除（删除同号旧记录）后，重试同一包应成功且无副本。
    with Session(ledger['engine']) as db:
        old = db.query(Run).filter(Run.station == 'station-a', Run.origin_id == 1).one()
        db.delete(old); db.commit()
    with ledger['Session']() as db:
        result = import_package(db, pkg2)
    assert result['status'] == 'completed'
    assert result['counts']['reports'] == 2
    with Session(ledger['engine']) as db:
        assert db.query(Run).filter(Run.station == 'station-a', Run.origin_id == 1).count() == 1
        assert db.query(LedgerImport).filter(LedgerImport.status == 'completed').count() == 2


def test_chain_merge_keeps_provenance_and_remaps_delivery(ledger):
    """A→B 后再 B→C：来源站与原号保持 A，交付指向目标站内新报告编号。"""
    from src.services.ledger import export_package, import_package
    from sqlalchemy.orm import Session
    reports = [make_report('station-a', 1), make_report('station-a', 2)]
    deliveries = [make_delivery(reports[0], 1)]
    pkg = make_package('station-a', reports, deliveries)
    with ledger['Session']() as db:
        assert import_package(db, pkg)['status'] == 'completed'
    # B 站再导出（本站标识不同，但迁入记录仍以 station-a 为来源）。
    pkg_b = export_package('station-b')
    assert {r['station'] for r in pkg_b['reports']} == {'station-a'}
    assert {d['station'] for d in pkg_b['deliveries']} == {'station-a'}
    with ledger['Session']() as db:
        db.query(Delivery).delete(); db.query(Run).delete(); db.commit()
        result = import_package(db, pkg_b)
    assert result['status'] == 'completed'
    assert result['counts'] == {'reports': 2, 'deliveries': 1, 'reused_reports': 0, 'reused_deliveries': 0}
    with Session(ledger['engine']) as db:
        runs = {r.id: r for r in db.query(Run).all()}
        delivery = db.query(Delivery).one()
        assert delivery.station == 'station-a' and delivery.origin_id == 1
        assert delivery.report_station == 'station-a' and delivery.report_origin_id == 1
        parent = [r for r in runs.values() if r.station == 'station-a' and r.origin_id == 1]
        assert len(parent) == 1 and delivery.run_id == parent[0].id
        assert delivery.payload == pkg['deliveries'][0]['payload']
        assert delivery.schedule_id is None
