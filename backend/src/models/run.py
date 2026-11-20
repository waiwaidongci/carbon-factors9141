from sqlalchemy import Column,Integer,String,JSON,Text,Float,Boolean,UniqueConstraint
from src.config.database import Base
class Run(Base):
 __tablename__='carbon_runs'
 id=Column(Integer,primary_key=True); name=Column(String(120)); status=Column(String(20)); snapshot=Column(JSON); checksum=Column(String(64)); result=Column(JSON); error=Column(Text); created=Column(Float); approved=Column(Boolean,default=False); review=Column(Text,default='')
 # 来源站与站内原号：跨站合并后本站分配新编号（id），原号只用于溯源与幂等。
 station=Column(String(120),nullable=False,index=True); origin_id=Column(Integer); imported=Column(Boolean,default=False,nullable=False)
 # 导入行先暂存不可见，整包核对通过后才激活。
 visible=Column(Boolean,default=True,nullable=False); import_id=Column(Integer,index=True)
 __table_args__=(UniqueConstraint('station','origin_id',name='uq_carbon_runs_station_origin'),)
class Schedule(Base):
 __tablename__='delivery_schedules'
 id=Column(Integer,primary_key=True); name=Column(String(120)); run_id=Column(Integer); interval_seconds=Column(Integer); next_at=Column(Float); enabled=Column(Boolean,default=True)
class Delivery(Base):
 __tablename__='deliveries'
 id=Column(Integer,primary_key=True); schedule_id=Column(Integer); run_id=Column(Integer); due_key=Column(String(160),unique=True); created=Column(Float); payload=Column(JSON)
 # 已发交付保留来源站、原号与已发内容；report_station/report_origin_id 指向来源站报告原号。
 station=Column(String(120),nullable=False,index=True); origin_id=Column(Integer); report_station=Column(String(120)); report_origin_id=Column(Integer); imported=Column(Boolean,default=False,nullable=False)
 visible=Column(Boolean,default=True,nullable=False); import_id=Column(Integer,index=True)
 __table_args__=(UniqueConstraint('station','origin_id',name='uq_deliveries_station_origin'),)
class LedgerImport(Base):
 __tablename__='ledger_imports'
 id=Column(Integer,primary_key=True); digest=Column(String(64),unique=True); source_station=Column(String(120),index=True)
 status=Column(String(20)); checkpoint=Column(JSON); counts=Column(JSON); reason=Column(Text); created=Column(Float); updated=Column(Float)
