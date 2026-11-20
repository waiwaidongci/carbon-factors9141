import time
from fastapi import HTTPException
from src.config.settings import STATION_NAME
from src.models.run import Run,Schedule,Delivery
from src.utils.origins import local_origin_id
from src.services.audit import log
def add_schedule(db,user,body):
 run=db.get(Run,body.run_id)
 if not run or not run.visible or not run.approved or run.status!='completed': raise HTTPException(409,'定时交付需引用已复核报告')
 obj=Schedule(name=body.name,run_id=run.id,interval_seconds=body.interval_seconds,next_at=time.time()+body.interval_seconds,enabled=True)
 db.add(obj);log(db,user.email,'schedule.create',body.name);db.commit();return obj
def tick(db):
 now=time.time()
 for schedule in db.query(Schedule).filter(Schedule.enabled==True,Schedule.next_at<=now).all():
  run=db.get(Run,schedule.run_id)
  if run and run.visible and run.approved and run.status=='completed':
   key=f'{schedule.id}:{schedule.next_at}'
   if not db.query(Delivery).filter_by(due_key=key).first():
    # 交付记录本站站号与站内原号；payload 保留发送时点的已发内容。
    obj=Delivery(schedule_id=schedule.id,run_id=run.id,due_key=key,created=now,payload={'name':run.name,'checksum':run.checksum,'result':run.result,'review':run.review,'policy':run.snapshot['policy']},station=STATION_NAME,report_station=run.station,report_origin_id=run.origin_id,imported=False,visible=True)
    db.add(obj);db.flush();obj.origin_id=local_origin_id(db,Delivery,STATION_NAME,obj.id)
    log(db,'worker','delivery.created',key)
  schedule.next_at=now+schedule.interval_seconds
 db.commit()
