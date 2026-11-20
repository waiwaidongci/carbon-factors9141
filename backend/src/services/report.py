import time
from fastapi import HTTPException
from src.config.settings import REDIS_URL,STATION_NAME
from src.models.source import Source,Policy
from src.models.run import Run
from src.utils.origins import local_origin_id
from src.utils.checksum import snapshot_checksum
from src.services.audit import log
import redis
def submit(db,user,body):
 policy=db.get(Policy,body.policy_id)
 if not policy: raise HTTPException(404,'口径版本不存在')
 sources=[x.data for x in db.query(Source).order_by(Source.id).all() if body.period=='全部' or x.data.get('period')==body.period]
 if not sources: raise HTTPException(422,'所选期间没有底稿')
 snapshot={'policy':policy.data,'sources':sources,'period':body.period}
 checksum=snapshot_checksum(snapshot)
 obj=Run(name=body.name,status='queued',snapshot=snapshot,checksum=checksum,created=time.time(),approved=False,station=STATION_NAME,imported=False,visible=True)
 db.add(obj);db.flush();obj.origin_id=local_origin_id(db,Run,STATION_NAME,obj.id)
 log(db,user.email,'run.queue',body.name);db.commit()
 redis.Redis.from_url(REDIS_URL).rpush('report-jobs',obj.id)
 return obj
def approve(db,user,id,note):
 obj=db.get(Run,id)
 if not obj: raise HTTPException(404,'报告不存在')
 if obj.status!='completed': raise HTTPException(409,'仅可复核已完成报告')
 if obj.approved: raise HTTPException(409,'报告已经复核')
 obj.approved=True;obj.review=note;log(db,user.email,'run.approve',id);db.commit();return obj
