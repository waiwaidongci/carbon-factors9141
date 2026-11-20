import json
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from src.config.database import database,Session
from src.controllers.auth import current,admin
from src.models.run import Run,Delivery,LedgerImport
from src.services.ledger import export_package,import_package,PackageError,PackageBusy
from src.utils.serialize import row
router=APIRouter()
MAX_PACKAGE_BYTES=20*1024*1024

@router.get('/ledger/export')
def export(u=Depends(admin)):
 # 独立只读会话读取一致快照，不复用可能已开启事务的请求会话。
 pkg=export_package()
 body=json.dumps(pkg,ensure_ascii=False,indent=2).encode('utf-8')
 name=f"ledger-{pkg['source_station']}-{int(pkg['exported_at'])}.json"
 return Response(body,media_type='application/json',headers={'Content-Disposition':f'attachment; filename="{name}"'})

def _import_in_own_session(package,actor):
 # 在线程池内使用独立会话，避免跨线程复用请求会话。
 with Session() as db: return import_package(db,package,actor=actor)

@router.post('/ledger/import')
async def upload(request:Request,u=Depends(admin)):
 # 先按上限读取原始包体再解析；整包导入放到线程池，避免阻塞事件循环。
 raw=await request.body()
 if len(raw)>MAX_PACKAGE_BYTES: raise HTTPException(413,'导入包过大')
 try: package=json.loads(raw.decode('utf-8'))
 except (UnicodeDecodeError,json.JSONDecodeError): raise HTTPException(422,'导入包不是合法 JSON')
 try: return await run_in_threadpool(_import_in_own_session,package,u.email)
 except PackageBusy as exc: raise HTTPException(409,str(exc))
 except PackageError as exc: raise HTTPException(422,str(exc))

@router.get('/ledger/imports')
def imports(db=Depends(database),u=Depends(admin)):
 return [row(x) for x in db.query(LedgerImport).order_by(LedgerImport.id.desc()).limit(50).all()]

@router.get('/ledger/origins')
def origins(db=Depends(database),u=Depends(current)):
 """报告/交付的来源站与原号对照，便于核对新编号。"""
 return {'reports':[{'id':x.id,'station':x.station,'origin_id':x.origin_id,'imported':x.imported} for x in db.query(Run).filter(Run.visible==True).order_by(Run.id).all()],
         'deliveries':[{'id':x.id,'station':x.station,'origin_id':x.origin_id,'run_id':x.run_id,'report_station':x.report_station,'report_origin_id':x.report_origin_id,'imported':x.imported} for x in db.query(Delivery).filter(Delivery.visible==True).order_by(Delivery.id).all()]}
