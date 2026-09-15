# 团队模式运维配置

本页对应单 Linux 主机试点。Windows/Docker Desktop 已用于开发验证，不能替代目标 Linux/GPU 的容量和部署验收。

## 发布构建和配置

运行环境使用 `requirements-lock.txt`，开发/CI 使用 `requirements-dev-lock.txt`，均含传递依赖和 SHA-256。
解析容器单独使用 `deploy/parser-requirements-lock.txt`。编译工具与命令：

```powershell
./.venv/Scripts/python.exe -m uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes --no-header -o requirements-lock.txt
./.venv/Scripts/python.exe -m uv pip compile requirements-dev.txt deploy/quality-requirements.txt --constraint requirements-lock.txt --universal --python-version 3.11 --generate-hashes --no-header -o requirements-dev-lock.txt
./.venv/Scripts/python.exe -m uv pip compile deploy/parser-requirements.txt --universal --python-version 3.11 --generate-hashes --no-header -o deploy/parser-requirements-lock.txt
./.venv/Scripts/python.exe scripts/check_locks.py --record
```

输入/锁变更必须重新审核并运行 `pip check`、pip-audit、完整测试。`check_locks.py` 防止输入或锁悄悄漂移，
不能代替依赖解析或漏洞检查。[uv 锁文件说明](https://docs.astral.sh/uv/pip/compile/)；
[pip-audit 官方说明](https://github.com/pypa/pip-audit)。当前门禁不忽略漏洞，服务不可达属于未完成，不能当作 0 漏洞。

`pyproject.toml` 从 auth/authz、DB session、errors、model profile、object store 和白名单日志共 10 个文件启用
strict mypy；其他新增模块执行 lint/行为测试，后续逐模块扩大类型检查。没有使用全局 Any 或关闭报错来表示已覆盖。

```powershell
docker build -f deploy/app.Dockerfile -t tracedesk-app:<version> .
docker build -f deploy/parser.Dockerfile -t tracedesk-parser:<version> .
docker build -f deploy/parse-worker.Dockerfile --build-arg APP_IMAGE=tracedesk-app:<version> -t tracedesk-parser-worker:<version> .
```

镜像分发和实际切流由真实部署操作者执行。复制 `.env.production.example` 为受限的部署配置，填入不可变镜像引用、
实际域名、目录和 Docker socket GID。不得直接运行含 REPLACE 的示例。`deploy/compose.yaml` 使用 Linux host networking，
API 仅绑定 loopback，PostgreSQL 只发布回环端口，vLLM generation/embedding 地址只允许同机 loopback。只有 TLS proxy 面向用户网段开放 443。

proxy 在 host networking 下仍以非 root UID 65532 运行。专用宿主先安装 `deploy/host-sysctl.conf` 到
`/etc/sysctl.d/99-tracedesk-proxy.conf` 并执行 `sysctl --system`，确认
`net.ipv4.ip_unprivileged_port_start=0`；否则 Linux 会拒绝 nginx 监听 443。nginx 的五类临时目录均指向
`/tmp` tmpfs，以保持容器根文件系统只读。

所有应用/worker 以 UID/GID 65532 运行，根文件系统只读。操作者预先创建数据目录并授予该 UID 写权限。
parse worker 必须通过 Docker socket 启动资源隔离容器，因此持有宿主 Docker 管理能力；它是可信控制进程，
Web 容器不挂载该 socket。解析子容器仍无网络、无数据库凭据、只读单个对象。对象 bind 路径在宿主和 worker 内必须完全相同。
四类 worker 显式处理 `SIGTERM`/`SIGINT`，使 Python 作为容器 PID 1 时仍会展开事务、parser 和连接清理；
Compose 为这些 worker 设置 120 秒停止宽限期。正常维护使用 `docker compose stop`，不要以 `kill` 代替；
故障注入场景中的强制终止必须单独标记，依靠 30 秒租约恢复，不能作为正常关停证据。

## 密钥与启动

受限 secret 目录包含 `database_password`、`database_url`、`bootstrap_token`、`tls_certificate`、`tls_private_key`。
数据库密码至少 24 字符，URL 使用同一密码及 `postgresql+psycopg` 协议。内容只在 secret 文件中，不放入示例环境文件、
镜像、Git 或日志。配置支持 `DATABASE_URL_FILE` 与 `TRACEDESK_BOOTSTRAP_TOKEN_FILE`，同一来源中不能同时配置直接值和文件。
生产模式拒绝无 DB、弱数据库凭据、localhost HTTPS origin；未知 TRACEDESK 环境变量会失败。

首次 bootstrap 完成后，移除 web 的 bootstrap secret 挂载与环境键，再重新部署。数据库中的 consumed 状态使旧 token
无法重复初始化。默认没有管理员口令；token 不会在 UI、日志或接口读回。

```powershell
docker compose --env-file .env.production -f deploy/compose.yaml config --quiet
docker compose --env-file .env.production -f deploy/compose.yaml up -d
```

模型服务先单独运行并完成协议探针，再执行数据库/解析全链路 smoke。协议探针可先复制
`docs/industrial/vllm-smoke.env.example` 为私有 `.env.vllm-smoke`，只填入固定 revision 的 digest：

vLLM 0.10.2 的临时 GPU 验证使用 Transformers `4.57.6`、Tokenizers `0.22.1` 和 `huggingface-hub 0.36.0`。
这三项需在独立 vLLM 环境中固定，不能按未约束的最新版本安装；否则 Qwen tokenizer 可能在服务启动时失败。
单机 systemd 示例见 `deploy/tracedesk-vllm-generation.service`、`deploy/tracedesk-vllm-embedding.service` 和
`deploy/vllm-systemd.env.example`。示例服务保持 disabled，由操作者在 GPU、权重 revision 和 manifest 核验后按顺序启动。

```powershell
./.venv/Scripts/python.exe scripts/vllm_protocol_smoke.py --env-file .env.vllm-smoke --output artifacts/vllm-protocol-<run-id>
./.venv/Scripts/python.exe scripts/industrial_model_smoke.py --database-env <test-env> --output artifacts/model-smoke-<run-id> --parser-image <pinned-parser-image>
```

第一条只证明 vLLM API、嵌入维度、served model ID、结构化输出和超时边界可用（配置中的 digest
仍需由固定 revision/权重 manifest 另行核验）；第二条才验证 TraceDesk 的
parse/index/query 任务链路。两者都不是语义质量或正式容量证据。

schema migration 成功和 PG healthy 是应用启动前提。proxy 等待 Web healthy。最终检查真实 HTTPS `/readyz`，
执行账号/权限/上传/索引/查询 smoke；不以 compose 进程存在判定部署成功。默认不开放 HTTP，HSTS 在域名和证书稳定后另行配置。

## 指标、日志与告警

`/livez` 仅报告进程响应。`/readyz` 检查数据库/schema、维护状态、object store 读写和任务表；
当前固定为允许 evidence-only 的策略，vLLM 不可用时仍 ready，模型任务产生明确失败/partial；不自动回退到云模型或其他 provider。

系统管理员可通过 `/api/v1/operations/metrics` 读取 Prometheus 格式指标；该路径依赖安全会话，
未配置无人值守抓取凭据前不应宣称监控平台已接通。HTTP 指标为本 Web 进程的 counter/histogram；
worker 任务/错误/查询时延由数据库汇总，24h quantile 是滑动窗口 gauge。CPU/RAM 在 Linux 使用进程 collector，
GPU 显存/利用率需目标主机采样器或 exporter。不要把当前非流式响应时延写成 TTFT。

OTel span 通过受限 JSON 日志 exporter 输出，保留 trace/span/parent ID 与时长，没有外发到云服务。
日志可由组织收集器接入；默认关闭 uvicorn/proxy 原始 access log，应用仅记录 route template。

建议告警：连续 3 次 readiness 失败、5xx 超过 5%/5 分钟、query queued >15/5 分钟、
租约持续过期、parse timeout >10%/15 分钟、磁盘剩余 <15% warning/<8% critical、成功备份超过 26 小时。
具体采集、告警路由、值班责任人仍须在目标环境验证。

## 保留与备份

`scripts/retain_database.py` 默认 dry-run；只对操作者显式选择的数据库使用 `--apply`。默认 30 天查询内容/trace/attempt、
90 天审计，运行中任务不清理。GC 使用系统回收站，Compose 把 XDG 用户目录放在持久数据目录中；
回收站和备份清理由数据 owner 的政策决定，工具不永久删除它们。参见 [备份恢复](backup-restore.md)。

## 升级与回滚

升级前停止 Web/worker，冻结新旧镜像 digest，并创建已验证备份。`scripts/upgrade_database.py` 默认只生成计划；只有同时提供精确数据库名、匹配当前数据库身份的备份、`--trusted-backup` 和 `--apply` 才执行 Alembic 迁移。完整步骤和失败分支见 [升级与应用回滚](upgrade-rollback.md)。
