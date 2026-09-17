# 容量与混合负载证据

正式容量结论只能来自目标 Linux/GPU 环境。`scripts/capacity_report.py` 不制造负载；它校验由实际负载驱动器和宿主采样器留下的 `capacity_manifest.json`、`environment.json` 与 `samples.jsonl`，并生成不含凭据和业务正文的汇总。

正式模式要求：

- 运行至少 30 分钟，50,000 active chunk、20 个注册用户、5 个并发 query；
- 覆盖稳态查询、解析+查询、重建索引+查询、模型重启、worker 崩溃、数据库重启、队列满、磁盘压力、陈旧写回竞争和稳态资源观察；
- 每类稳态时延至少 50 个成功样本，资源样本至少 20 个，运行开始和结束均有采样；
- 非模型 API P95 不超过 500 ms、evidence P95 不超过 2 s、queue P95 不超过 2 s、RAG total P95 不超过 30 s；
- query queue 不超过 20，queue-full 必须观察到明确拒绝；模型/数据库故障后必须恢复成功；
- OOM、数据损坏、权限/版本越界均为 0，RAM/VRAM 增长不超过事先冻结的上限。

每行 sample 记录 UTC `timestamp`、`scenario`、`operation`、`ok`，时延操作提供 `latency_ms`，失败提供稳定 `error_code`，资源采样提供 `rss_bytes`，可选 `vram_bytes` 和 `query_queue_depth`。manifest 对环境与样本文件固定 SHA-256，并保存事先批准的稳态错误率和内存增长上限。

```powershell
./.venv/Scripts/python.exe scripts/capacity_report.py <target-run-dir> --formal --report artifacts/capacity-<run-id>.json
```

本机合成测试只验证统计、边界和 FAIL 判定，不能作为 T-075 或 FA-18 的容量证据。

## T-075 正式结果

2026-09-17 在 Ubuntu 22.04.4、RTX 5090 32 GB、PostgreSQL 18.6、pgvector 0.8.6 和
vLLM 0.10.2 的目标环境完成正式门禁。最终 run 持续 1,807.485 秒，直接 SQL 证明为
50,000 active chunks、20 active users，负载使用 5 并发并覆盖全部 10 个场景；共记录
1,478 samples，稳态错误率为 0。

| 指标 | 实测 | 门槛 |
|---|---:|---:|
| API P95 | 64.742 ms | ≤ 500 ms |
| evidence P95 | 22.316 ms | ≤ 2,000 ms |
| queue P95 | 86.028 ms | ≤ 2,000 ms |
| RAG total P95 | 28,662.322 ms | ≤ 30,000 ms |
| 最大 query queue depth | 20 | ≤ 20 |
| RSS 增长 | 49,307,648 bytes（约 47.0 MiB） | ≤ 512 MiB |
| VRAM 增长 | 0 | ≤ 1 GiB |

报告无失败项，OOM、数据损坏和 scope leak 均为 0。证据归档 SHA-256 为
`db363bd8998957858e44db2378e7deb1bef3f73e95e1007d1ca683aa1d066ad2`，本地下载后已再次验哈；
`launcher-status.json` 记录远端退出码 0、证据已下载且已请求自动关机。该结论是受控目标环境容量测试，
不是生产 SLA、线上流量或长期可用性声明。

## T-075 驱动 readiness

先在不计费环境生成由当前 parser 实际复核的精确 50,000-chunk 输入：

```powershell
./.venv/Scripts/python.exe scripts/capacity_driver.py prepare-corpus --output artifacts/t075-readiness-<run-id>
```

目标实例开机后，必须先执行成本保护预检；任一项失败都停止正式跑批：

```powershell
./.venv/Scripts/python.exe scripts/capacity_driver.py preflight --output artifacts/t075-preflight-<run-id>
```

预检依次验证 `nvidia-smi`、`docker compose version`，以及与 parse worker 一致的
`docker run --rm --network none --read-only --cap-drop ALL alpine true`。语料 manifest 和
preflight 均固定 `formal_claim=none`；它们不生成 `samples.jsonl`，不能单独提交给正式判定器。

预检通过后再执行目标准备。凭据文件必须位于证据目录之外，并且只包含
`admin_email`、`admin_password`、`user_password`；驱动器不会把密码、Cookie 或 CSRF token 写入状态文件。

```powershell
./.venv/Scripts/python.exe scripts/capacity_driver.py provision `
  --base-url https://<target> `
  --credentials-file <private-credentials.json> `
  --corpus-root artifacts/t075-readiness-<run-id> `
  --output artifacts/t075-provision-<run-id>
```

该步骤复用或创建专用 KB，确保管理员加 19 个测试用户共 20 个注册用户，上传 20 份语料，
等待真实 parse jobs 和自动 index job 成功，并确认知识库已有 active generation。它仍是 readiness；
正式开始前还要直接从 PostgreSQL 固定 active chunks、active users 与镜像 digest 的原始查询证据。

### 开机后的单入口执行

2026-09-17 已完成阶段 A–E 的零成本准备。最终修复版启动包位于
`artifacts/t075-launch-bundle-20260917-10/`，共 34 个成员，不含凭据或私有评测数据；归档 SHA-256 为
`34821bca7a50427d15b334f826076b7e527c82424febefbd45baf09992412225`。本机 readiness 覆盖 10 个场景、
312 条结构样本；它只验证驱动和正式门禁边界，`formal_claim=none`。

目标 VM 开机并确认 SSH 可达后，在仓库根目录运行：

```powershell
pwsh -NoProfile -File scripts/t075_launch_via_learn_ssh.ps1
```

脚本只通过 LearnSSH 别名 `tracedesk-compshare-p1` 操作远端。默认流程是：校验本地包和现有管理员凭据、
唯一发现当前项目/生产 env/HTTPS origin、上传并复核 SHA-256、创建隔离容量盘、把现有四个镜像固定为
本机 registry 的 `tag@sha256`、启动服务、执行正式预检、建 50,000 chunks 和 20 用户、运行至少 30 分钟
的 10 场景负载、下载并验证证据归档，最后安排远端自动关机。5 小时硬超时会终止目标进程；目标脚本会在
正常或异常退出时打包已产生的证据。准备阶段或普通执行错误会保持开机，便于立即修复；只有正式运行成功且
证据下载验哈完成，或达到 5 小时硬超时，才自动关机。`-KeepRunning` 可禁止这两种自动关机。

如果自动发现得到多个生产 env，脚本会在上传和正式跑批前停止并保持开机；此时应显式同时传入
`-ProjectRoot`、`-SourceEnv` 和 `-PublicOrigin`，不能只覆盖其中一项。正式结论以下载后的
`capacity_report.json` 为准，报告可能 PASS 或 FAIL，启动器本身不预设结论。

## 短期 portfolio smoke

为预算受限的一次性 Linux/GPU 部署，另设独立的 `scope=portfolio-smoke` 轨道，详见
[短期部署计划](plan-portfolio-smoke.md)。该轨道使用自己的 `portfolio_manifest.json` 和
`portfolio_report.py`，可以记录真实部署的启动、时延、资源和恢复数据，但不会进入
`target-release` 验收，也不会改变本页的正式容量门禁。dry-run 报告固定为 `fixture`，不得当作运行证据。
P0 的真实入口仅返回 `health-only`；只有后续认证工作流和宿主采样完成后，才可记录运行时 RAG/资源指标。
