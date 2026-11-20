"""已部署数据库的前向结构升级：只加列/索引并回填，不删除既有数据。"""
from sqlalchemy import inspect,text
from src.config.settings import STATION_NAME

COLUMNS={
 'carbon_runs':[
  ('station','VARCHAR(120)'),
  ('origin_id','INTEGER'),
  ('imported','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('visible','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('import_id','INTEGER'),
 ],
 'deliveries':[
  ('station','VARCHAR(120)'),
  ('origin_id','INTEGER'),
  ('report_station','VARCHAR(120)'),
  ('report_origin_id','INTEGER'),
  ('imported','BOOLEAN NOT NULL DEFAULT FALSE'),
  ('visible','BOOLEAN NOT NULL DEFAULT TRUE'),
  ('import_id','INTEGER'),
 ],
}
INDEXES=[
 ('uq_carbon_runs_station_origin','carbon_runs','station, origin_id',True),
 ('uq_deliveries_station_origin','deliveries','station, origin_id',True),
 ('ix_carbon_runs_station','carbon_runs','station',False),
 ('ix_deliveries_station','deliveries','station',False),
 ('ix_carbon_runs_import_id','carbon_runs','import_id',False),
 ('ix_deliveries_import_id','deliveries','import_id',False),
]

def upgrade_schema(engine):
 """幂等升级：缺列则加列，缺索引则建索引，最后回填来源站与原号。"""
 inspector=inspect(engine)
 tables=set(inspector.get_table_names())
 if 'carbon_runs' not in tables or 'deliveries' not in tables: return
 existing={table:{col['name'] for col in inspector.get_columns(table)} for table in COLUMNS}
 with engine.begin() as c:
  for table,columns in COLUMNS.items():
   for name,ddl in columns:
    if name not in existing[table]: c.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
  c.execute(text("UPDATE carbon_runs SET station=:s WHERE station IS NULL OR station=''"),{'s':STATION_NAME})
  c.execute(text('UPDATE carbon_runs SET origin_id=id WHERE origin_id IS NULL'))
  c.execute(text('UPDATE carbon_runs SET imported=false WHERE imported IS NULL'))
  # 迁入交付的 due_key 含包摘要，需要比旧字段更宽。
  if engine.dialect.name=='postgresql':
   c.execute(text('ALTER TABLE deliveries ALTER COLUMN due_key TYPE VARCHAR(160)'))
  c.execute(text('UPDATE carbon_runs SET visible=true WHERE visible IS NULL'))
  c.execute(text("UPDATE deliveries SET station=:s WHERE station IS NULL OR station=''"),{'s':STATION_NAME})
  c.execute(text('UPDATE deliveries SET origin_id=id WHERE origin_id IS NULL'))
  c.execute(text('UPDATE deliveries SET imported=false WHERE imported IS NULL'))
  c.execute(text('UPDATE deliveries SET visible=true WHERE visible IS NULL'))
  # 旧交付按 run_id 回填所指向报告的来源站与原号。
  c.execute(text('UPDATE deliveries SET report_station=(SELECT station FROM carbon_runs WHERE carbon_runs.id=deliveries.run_id),report_origin_id=(SELECT origin_id FROM carbon_runs WHERE carbon_runs.id=deliveries.run_id) WHERE report_station IS NULL'))
  # 回填后补齐非空约束（新库由模型定义直接建为非空）。
  for table in ('carbon_runs','deliveries'):
   c.execute(text(f'ALTER TABLE {table} ALTER COLUMN station SET NOT NULL'))
   c.execute(text(f'ALTER TABLE {table} ALTER COLUMN imported SET NOT NULL'))
   c.execute(text(f'ALTER TABLE {table} ALTER COLUMN visible SET NOT NULL'))
  for name,table,columns,unique in INDEXES:
   c.execute(text(f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS {name} ON {table} ({columns})"))
