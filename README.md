# 棠原试验室碳因子结算册

合成用电/燃料/出行活动→因子核算与差异说明→站内周期结算；范围声明与缺因子拒绝

## 业务场景与工作流
模拟客户：棠原研究协作组，12位业务与分析人员。每月研究明细先从`carbon-factors-202611.csv`录入底稿，经口径冻结、Python核算、负责人复核后向业务阅读者交付站内CSV。原有流程依靠人工表格抄算，痛点为口径版本难追溯、重复计算易错、复核与交付脱节。

模块：业务总览、业务底稿、新建口径版本、异步核算与复核、定时交付、站内收件箱、审计。底稿可新增/修改/删除；口径仅新增版本；报告提交时冻结来源与版本并保存SHA256。定时交付引用已复核报告快照，worker到期写入数据库，业务阅读者下载。

## 算法口径
单位转换、因子版本冻结、用量×因子与部门透视。kWh/MWh单位换算手算；缺单位因子拒绝；因子更新旧报告不变。

活动量 × 因子 · 单位与范围随报告保存。输入：业务底稿与口径JSON；输出：summary、透视rows、method、warnings。算法实现在`backend/src/services/calculation.py`，输入校验在`validation.py`。固定seed=42，240条合成数据；未对接真实业务系统。修改源数据和新建因子/口径不会改变已提交报告。

## 一天的验收剧本
1. 09:00 admin查看240条seed底稿与具名业务期间。
2. 09:10 analyst添加`carbon-factors-202611.csv`的一条具名业务记录。
3. 09:20 admin在口径版本页登记新版本；旧版本保留。
4. 09:30 analyst命名“十一月核算”并选择期间/版本排队。
5. 09:31 Redis worker真实执行Python计算，展示汇总/透视与口径边界。
6. 09:40 admin核对数值后填写至少4字复核说明。
7. 09:45 admin建立60秒周期计划，worker到期生成站内持久化交付。
8. 09:46 viewer在站内收件箱下载交付CSV；审计页查看操作者与时间。

## 快速启动
```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```
前端 http://localhost:18254 ，后端 http://localhost:19254/docs ，数据库宿主端口17754。中文父目录可用。恰好5服务frontend/backend/worker/db/redis；均有真实healthcheck。停止并清空演示数据：`docker compose down -v --remove-orphans`。

|角色|邮箱|密码|权限|
|---|---|---|---|
|admin|admin@demo.local|Demo2026!|全部底稿、口径版本、核算、复核、调度|
|analyst|analyst@demo.local|Demo2026!|底稿增改删、核算、导出；无复核/调度/口径写权限|
|viewer|viewer@demo.local|Demo2026!|只读；仅可下载已复核报告与站内交付|

## 跨站结算库合并
异地站点各自维护同一套结算册时，报告与交付编号只在本站唯一，直接汇总会撞号。合并按“来源站 + 站内原号”识别记录：

- `python -m scripts.ledger export --out ledger-a.json`（或负责人页面“跨站合并→导出本站结算包”）只导出**已复核报告**与**已发交付**。导出在独立只读事务中完成（PostgreSQL `REPEATABLE READ, READ ONLY`），打包与 worker 定时发件并发时取同一时点，且不产生任何发送。
- `python -m scripts.ledger import --file ledger-a.json`（或页面上传）先整包校验格式、内容指纹、包摘要、父报告与同号冲突，全部通过才写入。导入保留来源站、原号、冻结底稿（`snapshot`+`checksum`）与已发内容；本站为报告和交付分配**新编号**，迁入交付的 `run_id` 指向同站报告的新编号，`schedule_id` 为空，因此**不触发补发**。
- 同站同号同内容沿用现有记录并计入“沿用”；同号不同内容、或交付缺少同站父报告，**整包拒绝**，已写暂存行回滚，界面与 API 均不可见。
- 导入按批提交并保存检查点（`ledger_imports.checkpoint`）。中断后用同一命令重试，从已核对处续跑；同一包摘要只登记一次，重试、以及并发导入重叠包都不会新增副本（PostgreSQL 用 advisory lock 串行化）。新行先以不可见状态暂存，整包核对通过后一次性激活，所以**核对通过前查不到**。
- 核对对照：`GET /ledger/origins` 列出每条报告/交付的“本站新编号 ← 来源站#原号”，页面“跨站合并”同步展示导入批次与检查点。仅 admin 可导出/导入；analyst、viewer 可查看对照。
- 两个站部署时给各自的 `STATION_NAME` 取不同值（默认取 `COMPOSE_PROJECT_NAME`）；同一份数据重复导出得到相同包摘要。

## 工程结构与开发
Python3.11/FastAPI/SQLAlchemy/Redis，PostgreSQL16；前端原生HTML/CSS/JS ES modules，Nginx代理`/api/`剥前缀至后端。`frontend/src`按api/stores/pages/components/hooks/utils/router/constants/types拆分；`backend/src`按models/schemas/services/controllers/routes/middlewares/config/utils/constants拆分；`backend/scripts/seed_demo.py`自然键幂等且不覆盖用户修改。SQLAlchemy启动创建初始结构，迁移说明在backend/migrations。

后端开发：在backend安装requirements，设置DATABASE_URL/REDIS_URL/JWT_SECRET，`uvicorn src.main:app --reload`；前端可用Nginx或带同等代理的静态开发服务器。前后端全部业务计算在Python。

环境变量：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、JWT_SECRET（部署替换）、DATABASE_URL、REDIS_URL、SEED_DEMO、STATION_NAME（本站标识，跨站合并时区分原号空间；默认取 COMPOSE_PROJECT_NAME）。`.env`不入Git。数据库/Redis/共享产物为命名卷。共享角色枚举admin/analyst/viewer与状态queued/running/completed/failed分别定义于backend/src/constants/enums.py和frontend/src/constants/enums.js。两个中间件：统一可恢复校验错误、每IP每分钟360请求限流。

## 已知边界
定时分发为站内收件箱；一名worker轮询执行，无外部邮件。调度发送已复核固定报告快照，不自动重算新数据。时间显示Asia/Shanghai。报告导出适合小型数据；底稿全量加载240行演示数据。Google Fonts不可用时回退serif。后端API以专属字段对象收发，页面通过业务输入控件编辑，不要求用户写JSON。新增来源期间自动可选；专属字段和算法严格校验。JWT演示有效期2小时。

MIT License。

## 实际实体映射
来源记录（Source，专属业务字段校验）、口径版本（Policy）、核算报告（Run）、交付计划（Schedule）、交付（Delivery）均经数据库→模型→请求schema→service→route/controller→frontend API→store→page。Period为已有来源期间字典，在任务期间选择器使用；不单独作为复杂度计数。底稿JSON为小型多字段单记录存储，不虚构未落库的子实体。

实际表：期间字典 activity_periods；专属来源 activities；冻结口径 emission_factors；异步报告 carbon_runs；交付计划 delivery_schedules；站内交付 deliveries；跨站合并批次 ledger_imports。carbon_runs 与 deliveries 以 `(station, origin_id)` 唯一标识记录，`visible` 控制核对前不可见，`import_id` 关联导入批次。

合并测试：`pip install -r requirements-dev.txt && pytest`。PostgreSQL 专属行为（一致快照、advisory lock、结构回填）通过 `LEDGER_TEST_PG=postgresql+psycopg://... pytest tests/test_ledger_pg.py` 启用，例如后端容器内：`docker compose exec -T backend env LEDGER_TEST_PG="$$DATABASE_URL" python -m pytest tests`。
