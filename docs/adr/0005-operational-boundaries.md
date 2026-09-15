# ADR 0005：维护、日志、审计和安全依赖

日期：2026-09-09。状态：本机实现和门禁已通过；目标环境验收以 industrial/status.md 为准。

## 维护与恢复

数据库业务事务取得共享 advisory 锁，备份/恢复/保留策略取得独占锁。已有事务或运行中任务使备份拒绝开始，
操作者需先排空任务。锁期间 HTTP 快速返回 503，livez 保持可用。独占锁随事务退出释放；不得并行运行旧版本
进程、直接 SQL 写入、DDL 或外部对象清理。恢复仅接受可信备份和新的空目标，比较内容指纹后撤销旧会话。
详细步骤见 [备份恢复手册](../industrial/backup-restore.md)。

## 运行日志与安全审计

两类数据分开：运行日志只有白名单字段，异常原文、URL query、凭据、问题和引用均不输出；
请求路由用模板，未知路径归为 unmatched。OTel SDK 产生 span，受限 exporter 仅输出关联 ID、名称与耗时，
不记录 exception events 或任意 span attributes。后台批量 exporter 有队列上限，失败不阻塞业务。

安全审计与高风险业务写入在同一数据库事务提交，审计失败则业务失败。系统管理员读取全局审计；workspace
管理员只可读所属 workspace。viewer/editor 不获得审计或全局指标访问。日志管道不替代这些事务审计。

保留策略默认仅预览，显式 `retain_database.py --apply` 后才清理 30 天前的终态查询内容/trace、
任务尝试、过期会话及 90 天前审计。保留 query tombstone，读取返回 410；排队/运行中任务不清理。
此工具不删除原文件、备份或当前索引；缩短保留周期须单独确认数据政策。

## 安全依赖升级

首次 pip-audit 发现 pypdf 5.9.0 的 41 项、python-multipart 0.0.29 的 3 项和 Starlette 0.50.0 的 7 项
已知漏洞，报告在 `artifacts/pip-audit-runtime-01.json`。单独升级 Starlette 不满足旧 FastAPI 的依赖约束，
因此本轮一并升级至 FastAPI 0.141.1、Starlette 1.6.0、pypdf 6.18.0、python-multipart 0.0.32。
开发工具额外升级 pytest 9.0.3 和 uv 0.11.15；未添加漏洞忽略或风险接受例外。

依据为 [Starlette 官方安全公告](https://github.com/Kludex/starlette/security/advisories)、
[pypdf 官方安全公告](https://github.com/py-pdf/pypdf/security/advisories)、
[multipart 官方安全公告](https://github.com/Kludex/python-multipart/security/advisories)
和 [FastAPI 发布说明](https://fastapi.tiangolo.com/release-notes/)，核验日期 2026-09-09。

解析输出新增真实 pypdf 版本，parse run 的 config hash 包含该版本。旧生成结果与失败记录原样保留；
新镜像、完整回归和浏览器必须复验，不能把安全升级当作答案质量提升。
