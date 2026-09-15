# 工程、质量与试用验收汇总

`scripts/acceptance.py` 校验一个 repository-relative 的 `acceptance_manifest.json`，覆盖 `FA-01` 至 `FA-21`。每项必须与 `docs/traceability.csv` 的任务映射一致，并引用已存在且 SHA-256 匹配的 evidence。`PASS` 和 `FAIL` 必须保留证据；`SKIPPED-BLOCKED` 必须写明具体阻塞条件。

manifest 顶层字段为 `format_version=1`、唯一 `run_id`、带时区的 `created_at`、`scope`、环境 artifact 和 21 个 gate。artifact 包含 `kind`、仓库相对 `path` 和 `sha256`。环境 JSON 不得含 password、token、session、question、quote、数据库 URL 或 prompt 字段。

`scope=local-readiness` 永远不会产生正式 PASS。`scope=target-release` 除要求 21 项全部 PASS 外，还执行以下硬检查：

- FA-01 必须引用真实 production Compose 报告；
- FA-14 必须引用授权真实副本迁移，而非构造库；
- FA-15 必须引用目标恢复演练并记录 RTO/RPO，合成恢复不够；
- FA-16 必须引用目标环境两个不可变应用镜像的 migration/app failure 回滚；
- FA-18 必须引用至少 30 分钟的 formal capacity PASS；
- FA-19 必须引用阈值事先冻结、无 scope leak/严重错误的 real holdout PASS；
- FA-20 必须引用浏览器 PASS；
- FA-21 必须引用真实用户任务、事先阈值和责任人签字。

```powershell
./.venv/Scripts/python.exe scripts/acceptance.py --manifest acceptance/<run-id>/acceptance_manifest.json --report acceptance/<run-id>/summary.json
```

失败 run 保留原目录；修复后创建新 run id。汇总器只验证证据完整性和关键边界，不能替代四方负责人对内容的审阅与签字。
