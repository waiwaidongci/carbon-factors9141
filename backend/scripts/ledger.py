"""跨站结算库合并命令。

导出（只读一致快照，不触发发件）：
  python -m scripts.ledger export --out ledger-station-a.json [--station A]

导入（整包校验，断点续跑，幂等）：
  python -m scripts.ledger import --file ledger-station-a.json [--chunk-size 100] [--actor admin@demo.local]
  python -m scripts.ledger status

退出码：0 成功/已导入，2 整包拒绝，3 中断可重试，4 文件错误，5 有导入批次未完成。
"""
import argparse,json,os,sys
from src.config.database import Base,engine,Session
from src.config.schema import upgrade_schema
# 导入模型以注册建表元数据。
from src.models import identity,source,run  # noqa: F401
from src.services.ledger import export_package,import_package,PackageError,PackageBusy
from src.config.settings import STATION_NAME

def main(argv=None):
 parser=argparse.ArgumentParser(prog='python -m scripts.ledger',description='跨站结算库导出/导入')
 sub=parser.add_subparsers(dest='command',required=True)
 ex=sub.add_parser('export',help='导出已复核报告与已发交付')
 ex.add_argument('--out',required=True,help='输出 JSON 路径')
 ex.add_argument('--station',default=STATION_NAME,help='来源站标识（默认本站）')
 im=sub.add_parser('import',help='导入其他站点的结算包')
 im.add_argument('--file',required=True,help='导入包 JSON 路径')
 im.add_argument('--chunk-size',type=int,default=100,help='每批提交行数，默认 100')
 im.add_argument('--actor',default='cli',help='审计操作者')
 sub.add_parser('status',help='查看导入批次与检查点')
 args=parser.parse_args(argv)
 Base.metadata.create_all(engine);upgrade_schema(engine)
 if args.command=='export':
  pkg=export_package(args.station)
  parent=os.path.dirname(os.path.abspath(args.out))
  if parent: os.makedirs(parent,exist_ok=True)
  with open(args.out,'w',encoding='utf-8') as handle: json.dump(pkg,handle,ensure_ascii=False,indent=2)
  print(json.dumps({'status':'exported','out':args.out,'source_station':pkg['source_station'],'reports':len(pkg['reports']),'deliveries':len(pkg['deliveries']),'digest':pkg['digest']},ensure_ascii=False))
  return 0
 if args.command=='status':
  from src.models.run import LedgerImport
  with Session() as db:
   rows=[{'id':x.id,'source_station':x.source_station,'status':x.status,'checkpoint':x.checkpoint,'counts':x.counts,'reason':x.reason,'digest':x.digest} for x in db.query(LedgerImport).order_by(LedgerImport.id).all()]
  print(json.dumps(rows,ensure_ascii=False,indent=2));return 0
 try:
  with open(args.file,encoding='utf-8') as handle: package=json.load(handle)
 except (OSError,json.JSONDecodeError) as exc:
  print(json.dumps({'status':'error','detail':f'无法读取导入包：{exc}'},ensure_ascii=False));return 4
 with Session() as db:
  try: result=import_package(db,package,actor=args.actor,chunk_size=args.chunk_size)
  except PackageBusy as exc:
   print(json.dumps({'status':'busy','detail':str(exc),'hint':'另有导入未完成；完成或续跑该批次后重试'},ensure_ascii=False));return 5
  except PackageError as exc:
   print(json.dumps({'status':'rejected','detail':str(exc)},ensure_ascii=False));return 2
  except Exception as exc:
   db.rollback()
   print(json.dumps({'status':'interrupted','detail':str(exc),'hint':'重新执行同一导入命令将从已核对处续跑'},ensure_ascii=False));return 3
 print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__': sys.exit(main())
