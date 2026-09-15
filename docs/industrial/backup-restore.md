# 团队模式备份与恢复

备份使用 PostgreSQL custom dump、全部登记的 immutable objects、schema/extensions/model profiles
和逐表内容指纹。备份目录最后出现 `manifest.json` 才表示完成；日志目录与备份都须使用受限磁盘权限，
备份包含原文、密码哈希与会话记录，不能作为公开测试 artifact 上传。

## 一致性边界

先停止接收新任务并排空运行中任务。备份尝试取得独占 advisory 维护锁，现有业务事务未结束时返回
`DATABASE_BUSY`，存在 running job 时返回 `BACKUP_JOBS_RUNNING`，不自行取消任务。
锁期间业务 transaction 快速返回 503 `MAINTENANCE_IN_PROGRESS`，livez 仍可用、readyz 为 false。
所有进程必须使用当前版本，操作者不得同时执行 SQL 写入、DDL、旧版本 worker 或外部对象清理。
工具先核对数据库服务器身份，避免容器与宿主端口指向不同实例。

PostgreSQL 官方说明 [pg_dump custom archive](https://www.postgresql.org/docs/18/app-pgdump.html)
可以提供数据库一致快照；本实现额外用 [advisory transaction locks](https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)
保持数据库引用和对象文件一致。此流程为有维护窗口的试点备份，未实现连续 WAL 归档/PITR。

## 操作示例（PowerShell 7）

受限 dotenv 中只配置操作者选定的 `DATABASE_URL`；命令中的数据库名必须与连接匹配。
`--postgres-container` 用于现有 PG18 容器；省略时使用本机 PG18 tools。

```powershell
./.venv/Scripts/python.exe scripts/backup_database.py backup --database-env .env.backup-source --expect-database <source-name> --objects <source-objects> --archive <new-backup-directory> --postgres-container <source-pg-container> --app-ref <pinned-app-image-digest> --report artifacts/<run>/backup.json
./.venv/Scripts/python.exe scripts/backup_database.py verify --archive <backup-directory> --report artifacts/<run>/verify.json
```

恢复前由操作者新建数据库和受限对象目录的父目录；目标对象目录必须尚不存在，目标数据库必须为空。
只恢复可信备份：[pg_restore 会执行备份中的 SQL](https://www.postgresql.org/docs/18/app-pgrestore.html)，
哈希用于发现损坏，不代替来源信任或签名。

```powershell
./.venv/Scripts/python.exe scripts/backup_database.py restore --database-env .env.restore-target --expect-database <new-target-name> --objects <new-target-objects> --archive <trusted-backup-directory> --postgres-container <target-pg-container> --trusted-backup --report artifacts/<run>/restore.json
```

工具验证每个对象和 dump 的 SHA-256，使用单事务 pg_restore，并比较所有业务表的行数/内容指纹。
旧会话会撤销，恢复环境中须重新登录。失败保留目标供诊断，拒绝覆盖重试；重新演练须另选空目标。
恢复完成仍须用匹配版本应用执行权限、来源和固定查询 smoke，再切换流量。不要把存活进程视为恢复成功。

默认建议每日备份、保留 7 日和 4 周；定时器与保留清理由实际部署者按数据政策配置，工具不删除备份。
26 小时无成功备份应告警。真实 RPO/RTO、目标机器和恢复耗时在部署演练后填写，不能使用合成测试替代。
