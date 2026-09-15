# 升级与应用回滚

本手册用于单机 Linux/Compose 部署。升级必须绑定一份已完成、已校验且来自同一数据库的备份；迁移失败后不自动覆盖源数据库。不可逆数据变化使用 expand/contract 或恢复到新数据库与新对象目录。

## 发布前准备

1. 冻结旧版和新版应用、解析 worker、解析器、代理镜像的 tag 与 digest。
2. 停止 Web 和所有 worker，确认没有 `running` job。
3. 使用 [备份恢复手册](backup-restore.md) 创建并验证 PostgreSQL + objects 备份。
4. 保留上一版 `.env.production`、Compose 渲染结果和镜像 digest。

先生成只读计划：

```powershell
./.venv/Scripts/python.exe scripts/upgrade_database.py --database-env <restricted-env> --expect-database <database> --archive <verified-backup> --report artifacts/upgrade-plan-<run-id>.json
```

计划会核对数据库身份、当前 Alembic revision、备份 source identity、迁移路径和在途任务。应用迁移需要额外显式开关：

```powershell
./.venv/Scripts/python.exe scripts/upgrade_database.py --database-env <restricted-env> --expect-database <database> --archive <verified-backup> --trusted-backup --apply --report artifacts/upgrade-apply-<run-id>.json
```

## 切换与观察

迁移成功后启动新版 Web，再依次启动 parse、index、query、gc worker，最后启动 TLS proxy。只有新版 `/readyz` 返回 `READY`，并完成登录、ACL、上传、来源、证据查询和任务 smoke 后才切流。观察期保留旧镜像，不清理旧索引代次和备份。

## 失败处理

- 迁移失败：事务回滚，Alembic revision 不前移；保留报告和数据库现场，修复后使用新 run id。不要启动新版应用。
- 新版启动或 smoke 失败且 schema 与旧版明确兼容：重新部署上一版不可变镜像并复验 `/readyz` 与固定查询。
- schema 与旧版不兼容或新版已写入旧版不理解的数据：停止全部服务，用受信备份恢复到新的数据库和新的对象目录，再部署上一版镜像。不要对唯一生产副本执行危险 downgrade。

本机隔离测试使用真实 `0005 → 0006` 迁移，验证成功路径保留既有数据；注入 DDL 冲突后 revision 保持 `0005`，且没有创建其余 `0006` 对象。真实 Compose 双镜像切换、域名/TLS 和目标主机观察窗口仍须由部署操作者执行。
