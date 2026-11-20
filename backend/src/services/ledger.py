"""跨站结算库合并：一致快照导出、整包校验导入、断点续跑与幂等。

合并两个站点时，各自报告与交付的原号会重叠。导入保留来源站与站内原号，
本站为新记录分配新编号；交付通过 run_id 指向同站报告的新编号，并保留已发内容。
"""
import hashlib,json,threading,time
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from src.config.database import Session
from src.config.settings import STATION_NAME
from src.utils.checksum import snapshot_checksum
from src.models.run import Run,Delivery,LedgerImport
from src.services.audit import log

FORMAT='tangyuan-carbon-ledger'
VERSION=1
_MS_LOCK=threading.Lock()

class PackageError(ValueError):
 """整包校验失败：消息面向操作者，导入不留下任何可见数据。"""

class PackageBusy(PackageError):
 """另一批导入尚未完成：稍后重试即可续跑，不算内容冲突。"""

def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest_of(value): return hashlib.sha256(canonical(value).encode()).hexdigest()

def report_content(run):
 return {'station':run.station,'origin_id':run.origin_id,'name':run.name,'status':run.status,'snapshot':run.snapshot,'checksum':run.checksum,'result':run.result,'error':run.error,'created':run.created,'approved':bool(run.approved),'review':run.review or ''}

def delivery_content(item):
 return {'station':item.station,'origin_id':item.origin_id,'created':item.created,'payload':item.payload or {},'report_station':item.report_station,'report_origin_id':item.report_origin_id}

def report_fingerprint(content): return digest_of(content)
def delivery_fingerprint(content): return digest_of(content)

def package_digest(source_station,reports,deliveries):
 # 摘要只取 (站, 原号, 内容指纹)，同一批数据重复导出得到同一摘要。
 return digest_of({'format':FORMAT,'version':VERSION,'source_station':source_station,'reports':[[r['station'],r['origin_id'],r['fingerprint']] for r in reports],'deliveries':[[d['station'],d['origin_id'],d['fingerprint']] for d in deliveries]})

def _read_snapshot(db):
 # 打包与定时发件并发时取同一时点：PostgreSQL 只读可重复读事务。
 # 非 PostgreSQL（演示/测试用 SQLite）退化为单事务读取，语义仍是不跨请求复用。
 if db.get_bind().dialect.name=='postgresql':
  db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))

def export_package(station=None,session_factory=None):
 """导出已复核报告和已发交付；单事务一致快照，只读，不触发补发。

 必须使用独立的新会话：认证依赖可能已在请求会话上开启事务，
 那样 SET TRANSACTION 会失败，且读不到同一时点的数据。
 """
 station=station or STATION_NAME
 factory=session_factory or Session
 with factory() as db:
  _read_snapshot(db)
  try: return _collect_package(db,station)
  finally: db.rollback()

def _collect_package(db,station):
 runs=db.query(Run).filter(Run.visible==True,Run.approved==True,Run.status=='completed').order_by(Run.station,Run.origin_id).all()
 items=db.query(Delivery).filter(Delivery.visible==True).order_by(Delivery.station,Delivery.origin_id).all()
 reports=[];deliveries=[];report_keys=set()
 for run in runs:
  content=report_content(run)
  if content['snapshot'] is None or content['result'] is None: continue
  reports.append({**content,'fingerprint':report_fingerprint(content)});report_keys.add((content['station'],content['origin_id']))
 for item in items:
  content=delivery_content(item)
  if content['payload'].get('result') is None: continue
  # 父报告未随包导出时略过该交付，避免生成缺父报告的包。
  if (content['report_station'],content['report_origin_id']) not in report_keys: continue
  deliveries.append({**content,'fingerprint':delivery_fingerprint(content)})
 reports.sort(key=lambda r:(r['station'],r['origin_id']))
 deliveries.sort(key=lambda d:(d['station'],d['origin_id']))
 package={'format':FORMAT,'version':VERSION,'source_station':station,'exported_at':time.time(),'reports':reports,'deliveries':deliveries}
 package['digest']=package_digest(station,reports,deliveries)
 return package

def _positive_int(value,label):
 if isinstance(value,bool) or not isinstance(value,int) or value<1: raise PackageError(label+'必须为正整数')
 return value

def _text(value,label,max_length=200):
 if not isinstance(value,str) or not value.strip(): raise PackageError(label+'不能为空')
 if len(value)>max_length: raise PackageError(label+'过长')
 return value

def _timestamp(value,label):
 if isinstance(value,bool) or not isinstance(value,(int,float)): raise PackageError(label+'不合法')
 return value

def _normalize_report(raw,index):
 label=f'报告 #{index}'
 if not isinstance(raw,dict): raise PackageError(label+'结构不合法')
 for key in ('station','origin_id','name','status','snapshot','checksum','result','error','created','approved','review','fingerprint'):
  if key not in raw: raise PackageError(label+f'缺少字段 {key}')
 station=_text(raw['station'],label+' 来源站',120);origin=_positive_int(raw['origin_id'],label+' 原号')
 name=_text(raw['name'],label+' 名称',120);ident=f'{station}#{origin}'
 if raw['status']!='completed': raise PackageError(f'报告 {ident} 未完成，不得迁入')
 if raw['approved'] is not True: raise PackageError(f'报告 {ident} 未复核，不得迁入')
 if not isinstance(raw['snapshot'],dict) or not isinstance(raw['result'],dict): raise PackageError(f'报告 {ident} 缺少冻结底稿或核算结果')
 checksum=_text(raw['checksum'],f'报告 {ident} 校验和',64)
 if snapshot_checksum(raw['snapshot'])!=checksum: raise PackageError(f'报告 {ident} 冻结底稿校验和不符')
 if raw['error'] is not None and not isinstance(raw['error'],str): raise PackageError(f'报告 {ident} 错误信息不合法')
 if not isinstance(raw['review'],str) or len(raw['review'])>2000: raise PackageError(f'报告 {ident} 复核说明不合法')
 content={'station':station,'origin_id':origin,'name':name,'status':'completed','snapshot':raw['snapshot'],'checksum':checksum,'result':raw['result'],'error':raw['error'],'created':_timestamp(raw['created'],f'报告 {ident} 创建时间'),'approved':True,'review':raw['review']}
 if raw['fingerprint']!=report_fingerprint(content): raise PackageError(f'报告 {ident} 内容指纹不符')
 return content

def _normalize_delivery(raw,index):
 label=f'交付 #{index}'
 if not isinstance(raw,dict): raise PackageError(label+'结构不合法')
 for key in ('station','origin_id','created','payload','report_station','report_origin_id','fingerprint'):
  if key not in raw: raise PackageError(label+f'缺少字段 {key}')
 station=_text(raw['station'],label+' 来源站',120);origin=_positive_int(raw['origin_id'],label+' 原号')
 report_station=_text(raw['report_station'],label+' 报告来源站',120);report_origin=_positive_int(raw['report_origin_id'],label+' 报告原号')
 ident=f'{station}#{origin}';payload=raw['payload']
 if not isinstance(payload,dict) or 'result' not in payload or 'checksum' not in payload: raise PackageError(f'交付 {ident} 缺少已发内容')
 content={'station':station,'origin_id':origin,'created':_timestamp(raw['created'],f'交付 {ident} 发送时间'),'payload':payload,'report_station':report_station,'report_origin_id':report_origin}
 if raw['fingerprint']!=delivery_fingerprint(content): raise PackageError(f'交付 {ident} 内容指纹不符')
 return content

def verify_package(package):
 """整包只读校验：格式、字段、指纹、摘要、父报告与包内冲突。"""
 if not isinstance(package,dict): raise PackageError('导入包必须是 JSON 对象')
 if package.get('format')!=FORMAT: raise PackageError('不是跨站结算库导入包')
 if package.get('version')!=VERSION: raise PackageError(f"导入包版本不支持：{package.get('version')}")
 source_station=_text(package.get('source_station'),'导入包来源站',120)
 if not isinstance(package.get('reports'),list) or not isinstance(package.get('deliveries'),list): raise PackageError('导入包缺少报告或交付列表')
 reports={}
 for i,raw in enumerate(package['reports'],1):
  content=_normalize_report(raw,i);key=(content['station'],content['origin_id'])
  if key in reports and report_fingerprint(reports[key])!=report_fingerprint(content): raise PackageError(f'包内报告 {key[0]}#{key[1]} 内容冲突')
  reports[key]=content
 deliveries={}
 for i,raw in enumerate(package['deliveries'],1):
  content=_normalize_delivery(raw,i);key=(content['station'],content['origin_id'])
  if key in deliveries and delivery_fingerprint(deliveries[key])!=delivery_fingerprint(content): raise PackageError(f'包内交付 {key[0]}#{key[1]} 内容冲突')
  deliveries[key]=content
 report_list=[{**reports[k],'fingerprint':report_fingerprint(reports[k])} for k in sorted(reports)]
 delivery_list=[{**deliveries[k],'fingerprint':delivery_fingerprint(deliveries[k])} for k in sorted(deliveries)]
 if package.get('digest')!=package_digest(source_station,report_list,delivery_list): raise PackageError('导入包摘要不符，可能已损坏或被改动')
 for key,item in deliveries.items():
  parent=(item['report_station'],item['report_origin_id'])
  if parent not in reports: raise PackageError(f"交付 {key[0]}#{key[1]} 缺少同站父报告 {parent[0]}#{parent[1]}")
  if item['payload']['checksum']!=reports[parent]['checksum']: raise PackageError(f"交付 {key[0]}#{key[1]} 的已发内容与父报告 {parent[0]}#{parent[1]} 不一致")
 return source_station,reports,deliveries

def _import_due_key(digest,key):
 # 迁入交付的幂等键：包摘要 + 来源站 + 原号，定长，不与计划生成的键冲突。
 return 'import:'+digest_of([digest,key[0],key[1]])[:48]

def _find_run(db,key): return db.query(Run).filter(Run.station==key[0],Run.origin_id==key[1]).first()
def _find_delivery(db,key): return db.query(Delivery).filter(Delivery.station==key[0],Delivery.origin_id==key[1]).first()
def _existing_fingerprint(existing):
 if isinstance(existing,Run): return report_fingerprint(report_content(existing))
 return delivery_fingerprint(delivery_content(existing))

def _is_reusable(db,existing,fingerprint,label,batch):
 """同站同号同内容沿用现有记录；内容不同整包拒绝；同号为未完成导入则稍后重试。"""
 if existing is None: return False
 if existing.import_id==batch.id: return True
 other=db.get(LedgerImport,existing.import_id) if existing.import_id else None
 if other is not None and other.status!='completed': raise PackageBusy(f'{label} 被未完成的导入批次 #{other.id} 占用；请先用原导入包重试该批次（续跑）后再导入本包')
 if _existing_fingerprint(existing)!=fingerprint: raise PackageError(f'{label} 已存在且内容不同，整包拒绝')
 return True

def _acquire_lock(db):
 """并发导入重叠包串行化：PostgreSQL 用独立连接上的会话级 advisory lock。"""
 if db.get_bind().dialect.name=='postgresql':
  bind=db.get_bind();bind=getattr(bind,'engine',bind)
  conn=bind.connect()
  conn.execute(text("SELECT pg_advisory_lock(hashtext('carbon-ledger-import'))"))
  return conn
 _MS_LOCK.acquire()
 return None

def _release_lock(handle):
 try:
  if handle is not None:
   handle.execute(text("SELECT pg_advisory_unlock(hashtext('carbon-ledger-import'))"))
   handle.rollback();handle.close()
  else: _MS_LOCK.release()
 except Exception: pass

def _counts(db,batch,total_reports,total_deliveries):
 created_reports=db.query(Run).filter(Run.import_id==batch.id).count()
 created_deliveries=db.query(Delivery).filter(Delivery.import_id==batch.id).count()
 return {'reports':created_reports,'deliveries':created_deliveries,'reused_reports':total_reports-created_reports,'reused_deliveries':total_deliveries-created_deliveries}

def _result(status,batch):
 return {'status':status,'digest':batch.digest,'source_station':batch.source_station,'checkpoint':batch.checkpoint,'counts':batch.counts or {}}

def _reset_staged(db,batch):
 db.query(Run).filter(Run.import_id==batch.id).delete(synchronize_session=False)
 db.query(Delivery).filter(Delivery.import_id==batch.id).delete(synchronize_session=False)
 batch.checkpoint={'reports':0,'deliveries':0};batch.reason=None;batch.status='importing';db.commit()

def _mark_rejected(db,batch,reason,actor='cli'):
 try:
  db.rollback()
  batch=db.get(LedgerImport,batch.id)
  db.query(Run).filter(Run.import_id==batch.id).delete(synchronize_session=False)
  db.query(Delivery).filter(Delivery.import_id==batch.id).delete(synchronize_session=False)
  batch.status='rejected';batch.reason=reason[:500];batch.updated=time.time()
  log(db,actor,'ledger.reject',reason[:200]);db.commit()
 except Exception:
  db.rollback()

def _preflight(db,batch,reports,deliveries):
 """写库前逐项核对既有记录：内容冲突、未完成同号导入、父报告缺失。

 这一步保证“整包拒绝”发生在任何暂存写入之前；循环内的同款检查只用于兜住并发竞态。
 """
 for key,content in sorted(reports.items()):
  existing=_find_run(db,key)
  if existing is not None:
   _is_reusable(db,existing,report_fingerprint(content),f'报告 {key[0]}#{key[1]}',batch)
 for key,content in sorted(deliveries.items()):
  existing=_find_delivery(db,key)
  if existing is not None:
   _is_reusable(db,existing,delivery_fingerprint(content),f'交付 {key[0]}#{key[1]}',batch)
  parent_key=(content['report_station'],content['report_origin_id'])
  if parent_key in reports: continue
  parent=_find_run(db,parent_key)
  if parent is None or not parent.visible or not parent.approved or parent.status!='completed':
   raise PackageError(f"交付 {key[0]}#{key[1]} 缺少同站父报告 {parent_key[0]}#{parent_key[1]}")

def import_package(db,package,actor='cli',chunk_size=100,interrupt_hook=None):
 """整包导入。全部校验通过才写入；写入分批提交并保存检查点。

 中断后重试同一导入包从已核对处续跑；同一摘要的导入只登记一次。
 新行先以不可见状态暂存，最后一次性激活，因此核对通过前查询不到；
 迁入交付不建计划、不触发补发。
 """
 if chunk_size<1: chunk_size=1
 source_station,reports,deliveries=verify_package(package)
 digest=package['digest']
 lock=_acquire_lock(db)
 try:
  batch=db.query(LedgerImport).filter_by(digest=digest).first()
  if batch is None:
   batch=LedgerImport(digest=digest,source_station=source_station,status='importing',checkpoint={'reports':0,'deliveries':0},counts={'reports':0,'deliveries':0,'reused_reports':0,'reused_deliveries':0},created=time.time(),updated=time.time())
   db.add(batch)
   try: db.commit()
   except IntegrityError:
    db.rollback();batch=db.query(LedgerImport).filter_by(digest=digest).first()
    if batch is None: raise
  if batch.status=='completed': return _result('already-imported',batch)
  if batch.source_station!=source_station: raise PackageError('同一导入包摘要对应了不同来源站')
  if batch.status=='rejected': _reset_staged(db,batch)
  batch.status='importing';batch.reason=None;db.commit()
  checkpoint=dict(batch.checkpoint or {'reports':0,'deliveries':0})
  report_keys=sorted(reports);delivery_keys=sorted(deliveries);mapping={}
  try:
   _preflight(db,batch,reports,deliveries)
   start=min(max(int(checkpoint.get('reports',0)),0),len(report_keys))
   for key in report_keys[:start]:
    existing=_find_run(db,key)
    if existing is not None: mapping[key]=existing.id
   index=start;chunks=0
   while index<len(report_keys):
    for key in report_keys[index:index+chunk_size]:
     content=reports[key];fingerprint=report_fingerprint(content);existing=_find_run(db,key)
     if _is_reusable(db,existing,fingerprint,f'报告 {key[0]}#{key[1]}',batch):
      mapping[key]=existing.id;continue
     obj=Run(station=key[0],origin_id=key[1],name=content['name'],status='completed',snapshot=content['snapshot'],checksum=content['checksum'],result=content['result'],error=content['error'],created=content['created'],approved=True,review=content['review'],imported=True,visible=False,import_id=batch.id)
     db.add(obj);db.flush();mapping[key]=obj.id
    index=min(index+chunk_size,len(report_keys))
    batch.checkpoint={'reports':index,'deliveries':0};batch.updated=time.time();db.commit();chunks+=1
    if interrupt_hook: interrupt_hook(chunks,'reports')
   start=min(max(int(batch.checkpoint.get('deliveries',0)),0),len(delivery_keys))
   index=start;chunks=0
   while index<len(delivery_keys):
    for key in delivery_keys[index:index+chunk_size]:
     content=deliveries[key];fingerprint=delivery_fingerprint(content);existing=_find_delivery(db,key)
     if _is_reusable(db,existing,fingerprint,f'交付 {key[0]}#{key[1]}',batch): continue
     parent_key=(content['report_station'],content['report_origin_id'])
     parent_id=mapping.get(parent_key)
     if parent_id is None:
      parent=_find_run(db,parent_key)
      if parent is None or not parent.visible or not parent.approved or parent.status!='completed':
       raise PackageError(f"交付 {key[0]}#{key[1]} 缺少同站父报告 {parent_key[0]}#{parent_key[1]}")
      parent_id=parent.id
     db.add(Delivery(station=key[0],origin_id=key[1],schedule_id=None,run_id=parent_id,due_key=_import_due_key(digest,key),created=content['created'],payload=content['payload'],report_station=content['report_station'],report_origin_id=content['report_origin_id'],imported=True,visible=False,import_id=batch.id))
    index=min(index+chunk_size,len(delivery_keys))
    batch.checkpoint={'reports':len(report_keys),'deliveries':index};batch.updated=time.time();db.commit();chunks+=1
    if interrupt_hook: interrupt_hook(chunks,'deliveries')
   # 核对通过后整包才可查到：一次性激活。
   db.query(Run).filter(Run.import_id==batch.id).update({'visible':True},synchronize_session=False)
   db.query(Delivery).filter(Delivery.import_id==batch.id).update({'visible':True},synchronize_session=False)
   batch.status='completed';batch.checkpoint={'reports':len(report_keys),'deliveries':len(delivery_keys)};batch.updated=time.time()
   batch.counts=_counts(db,batch,len(report_keys),len(delivery_keys))
   log(db,actor,'ledger.import',f'{digest} 来源站 {source_station} 报告 {len(report_keys)} 交付 {len(delivery_keys)}');db.commit()
   return _result('completed',batch)
  except PackageBusy as exc:
   db.rollback()
   try:
    batch.status='interrupted';batch.reason=str(exc)[:500];batch.updated=time.time()
    log(db,actor,'ledger.busy',digest);db.commit()
   except Exception: db.rollback()
   raise
  except PackageError as exc:
   _mark_rejected(db,batch,str(exc),actor);raise
  except Exception as exc:
   db.rollback()
   try:
    batch.status='interrupted';batch.reason=str(exc)[:500];batch.updated=time.time();db.commit()
   except Exception: db.rollback()
   raise
 finally:
  _release_lock(lock)

def import_file(db,path,actor='cli',**kwargs):
 with open(path,encoding='utf-8') as handle: package=json.load(handle)
 return import_package(db,package,actor=actor,**kwargs)
