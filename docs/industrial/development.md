# 工业化开发与验证

本页描述已实现的团队模式能力和本机验证入口；正式发布仍按 [实施状态](status.md) 的外部门禁推进。
所有命令在应用仓库根目录运行。真实数据库、凭据、原文件和 artifacts 不入 Git。

## 开发环境

安装 `requirements-dev.txt`。PostgreSQL 镜像固定为：
`pgvector/pgvector:0.8.6-pg18-trixie@sha256:78bf48b801e792f99e3ac62b5036fd3876e9be48afda16c1e331af1c75ceb2ff`。
本机已实际运行 PostgreSQL 18.6、vector 0.8.6、citext 1.8。
测试使用独立容器、随机密码和动态回环端口；私有 `.env.industrial-test` 保存连接信息。

```powershell
docker build --file deploy/parser.Dockerfile --tag tracedesk-parser:dev .
./.venv/Scripts/python.exe scripts/run_industrial_checks.py --database-env .env.industrial-test --parser-image tracedesk-parser:industrial-secure-20260911 --postgres-container <isolated-pg-container> --output artifacts/check-<new-run-id>
```

每个 PostgreSQL 测试新建 `tracedesk_test_<uuid>` 数据库，测试后清理它自己的数据库。
连接账号需要 CREATE DATABASE 权限；只能使用隔离测试实例。检查器没有测试连接时拒绝运行，
普通 `pytest` 在没有相应连接/镜像变量时会显式跳过 PostgreSQL/容器测试。完整检查器要求二者齐备且禁止 skipped。
CI 已增加独立 PostgreSQL job 和解析镜像构建；
本机通过不能冒充 GitHub CI 已运行。

## Schema 与团队入口

由操作者在进程环境设置 `DATABASE_URL`，格式为 `postgresql+psycopg`。
迁移只对显式指定的目标库执行：

```powershell
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe scripts/check_database.py
```

配置 `DATABASE_URL` 后 `app.main` 选择独立团队入口；schema 落后会阻止启动。
未配置时保留 RC2 本机路径。团队模式主页为独立团队前端；旧 API 只保留鉴权后的来源读取适配，
旧写操作返回 `CLIENT_UPGRADE_REQUIRED` 和弃用 header。
设置 `TRACEDESK_PUBLIC_ORIGIN` 为实际 HTTPS origin，通过 TLS 反向代理访问。
首次 bootstrap 需要操作者设置高熵 `TRACEDESK_BOOTSTRAP_TOKEN`，初始化后不会再次生效。
没有默认管理员/密码。登录返回 HttpOnly Cookie 与 `X-CSRF-Token`；后续写请求携带
精确 Origin 和 CSRF header。登录和 bootstrap 在无会话阶段用 Origin 与限流保护。

## SQLite 迁移

先停止旧服务并由操作者完成备份和 WAL checkpoint。预检拒绝非空 WAL/journal，
用 SQLite immutable/read-only 读取，校验源库前后哈希，绝不自行 checkpoint 用户数据库。

```powershell
./.venv/Scripts/python.exe scripts/migrate_sqlite.py --source <frozen-copy.db> --dry-run --output artifacts/migration-plan-<run-id>
./.venv/Scripts/python.exe scripts/migrate_sqlite.py --source <frozen-copy.db> --apply --admin-id <existing-admin-uuid> --output artifacts/migration-<run-id>
```

每个文档事务提交 checkpoint，重复执行核对已迁移文本后跳过。可用 `--max-documents N`
分批处理；尚有剩余时退出码 2。旧 collection/version 分开建 KB，缺失原文件明确标注，
只保存 legacy extracted text；旧向量不激活，quarantined 资料保持隔离。
旧 conversations/traces 保留在受限源备份中，不绑定新用户；归档保留期由操作者确认。
迁移后的 reindex jobs 由索引 worker 完成；只有 generation 激活成功才切换向量查询。

## 上传和隔离解析

登录后通过 `POST /api/v1/knowledge-bases/{kb_id}/documents` 上传，提供 `Idempotency-Key`。
返回修订和 job ID；`GET /api/v1/jobs/{id}` 查看状态。单次 worker 可这样运行：

```powershell
./.venv/Scripts/python.exe -m app.jobs.worker --once --parser-image tracedesk-parser:dev
```

不加 `--once` 时使用 PostgreSQL LISTEN 等待任务并恢复过期租约。worker 在宿主机通过 Docker
启动解析容器，容器不获得数据库凭据，只能读取当前对象。容器默认无网络、只读根目录、
1.5 GiB 内存、1 CPU、64 PID、2 GiB 临时空间，输出最多 16 MiB；宿主监督和容器自身都有墙钟限制。
worker 恢复时只清理带 TraceDesk 标记、名称匹配且已过期的容器。
当前保留 RC2 的 10 MiB/100 页/100 万字符文件限制，放宽需容量证据。

成功解析写入独立 parse run/pages/chunks；来源经授权 API 读取。
解析失败记录 job/parse run/revision 状态，旧 active 不变；陈旧任务不提交。
当 KB 的目标修订都已解析，事务内自动提交 reindex 任务。索引 worker 分批嵌入，核对模型 digest、
lease、资料 manifest 和 parse run；全部成功才原子激活。失败保持上一活动版本。

## 任务基础

`app/jobs/repository.py` 实现领取、heartbeat、恢复、重试、取消与最终事务。
租约 30 秒；重试 5/30 秒；按 owner 和 attempt 双重校验，旧 attempt 不能写回。
parse/index/query/gc handler 及任务 UI 已接入；正式 eval API/质量运行链仍待真实资料和标注输入。
通过 `--kind parse|index|query|gc` 启动对应 worker。首次完整链路须同时运行 parse、index 和 query worker。
GC 只回收已超过 7 天恢复窗口的删除资料，使用系统回收站；系统不支持回收站时保留对象并报告失败。

## 问答与团队浏览器

`POST /api/v1/queries` 返回 query/job ID；读取、trace 和 Markdown 导出均复核用户、权限与资料 epoch。
权限变化会清除失效回答、来源和 trace。模型技术故障可返回 partial 和来源，资料不全仍为 no_evidence。
模型 profile 从现有 Settings 读取，连接地址以 `.env` 实际配置为准。

团队页面支持登录、知识库、上传、任务、问答、来源、导出、删除/恢复及管理员授权。
可选浏览器依赖在 `deploy/browser-requirements.txt`。`scripts/industrial_browser_smoke.py` 使用隔离库、
真实 HTTPS、无界面 Edge 与实际解析容器；`scripts/vllm_protocol_smoke.py` 先验证配置的vLLM协议，
再由 `scripts/industrial_model_smoke.py` 使用隔离库和合成资料执行一次真实 provider 解析/索引/问答。
正式团队环境固定使用vLLM；历史Ollama smoke仅作为兼容基线。两者都不是正式质量或多人容量验收。

## 最新本机门禁

使用完整 runner 时必须提供隔离 PostgreSQL、已构建 parser image 和新输出目录：

```powershell
./.venv/Scripts/python.exe scripts/run_industrial_checks.py --database-env .env.industrial-test --parser-image tracedesk-parser:industrial-secure-20260911 --postgres-container tracedesk-industrial-pg-20260909 --output artifacts/<new-run-id>
```

最近一次 `artifacts/integration-20260911-03/` 为 285 passed、0 skipped。浏览器验收结果在
`artifacts/browser-team-20260911-05/`；本机验收汇总在 `artifacts/acceptance-local-20260911-04/`，状态必须保持
`blocked`，直到目标部署、真实质量、容量和试用证据到位。

## 备份恢复

使用 [备份恢复手册](backup-restore.md)；完整检查会真实调用 PG18 的 pg_dump/pg_restore，
测试连接需要隔离实例的备份与建库权限。可选择已运行的 PG18 容器，或使用本机 PG18 客户端。
