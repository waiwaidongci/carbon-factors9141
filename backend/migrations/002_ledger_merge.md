# 跨站结算库合并

为报告与交付增加来源站、站内原号和导入批次，并新增导入批次表。前向兼容：启动时 `upgrade_schema` 只加列/索引并回填，不删数据。

## carbon_runs
- `station VARCHAR(120) NOT NULL`：来源站；旧行回填为部署时的 `STATION_NAME`。
- `origin_id INTEGER`：被合并站内的原报告号；本站记录等于自身 `id`，旧行回填为 `id`。
- `imported BOOLEAN NOT NULL DEFAULT FALSE`
- `visible BOOLEAN NOT NULL DEFAULT TRUE`：导入行先暂存不可见，整包核对通过后激活。
- `import_id INTEGER`：所属导入批次 `ledger_imports.id`。
- 唯一索引 `uq_carbon_runs_station_origin (station, origin_id)`。

## deliveries
- `station VARCHAR(120) NOT NULL`、`origin_id INTEGER`：交付来源站与原号；旧行回填同 `carbon_runs`。
- `report_station VARCHAR(120)`、`report_origin_id INTEGER`：所指向报告的来源站与原号（迁入交付据此解析到本站新 `run_id`）。
- `imported BOOLEAN NOT NULL DEFAULT FALSE`、`visible BOOLEAN NOT NULL DEFAULT TRUE`、`import_id INTEGER`。
- 唯一索引 `uq_deliveries_station_origin (station, origin_id)`。

## ledger_imports
`id, digest(unique), source_station, status(importing/completed/rejected/interrupted), checkpoint(JSON), counts(JSON), reason, created, updated`。
按包摘要幂等登记；检查点分别记录已提交的报告数与交付数，供中断续跑。

## 升级注意
先在副本验证再上线；`upgrade_schema` 会为旧交付按 `run_id` 回填 `report_station/report_origin_id`。旧交付 `payload` 已含 `checksum`，可直接与父报告核对。回滚需先备份。
