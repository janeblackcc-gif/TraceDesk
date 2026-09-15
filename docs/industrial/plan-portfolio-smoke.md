# Portfolio Smoke 短期部署计划

状态：**P0 和 P1 短期简历证据轨道已完成：真实 Linux/GPU smoke、认证浏览器、generation/embedding 模型进程故障恢复、数据库重启、单次 worker 崩溃恢复、合成备份/全新目标恢复及本地不可变双镜像健康回滚均已完成。**

本计划服务于一次性工程实测和简历证据收集，不替代正式 T-075 容量验收，也不改变
`docs/industrial/capacity.md` 中的 30 分钟、50,000 chunks、20 用户和 5 并发门禁。

## 1. 目标

在有限预算内完成一次可复核的真实部署，记录：

- Ubuntu Linux、Docker Compose、PostgreSQL/pgvector 和 secure parser 的启动状态；
- vLLM generation/embedding 协议和模型身份；
- 小规模语料的 parse/index/query 链路；
- 1、2、5 并发下的实际请求时延；
- 模型、worker 和数据库重启后的恢复结果；
- 启动耗时、P50/P95、CPU/RAM/VRAM 和运行成本。

所有结果必须标明数据类别（`synthetic` 或 `authorized-subset`）、访问范围和运行模式。

## 2. 明确不做的事

- 不把小规模结果写成 `target-release` 或正式容量 PASS；
- 不降低 T-075 的 50,000 active chunks、20 用户、5 并发和 10 场景要求；
- 不用 `local-readiness` 伪装云端短测；
- 不在 dry-run 中填写看似真实的性能结论；
- 不在尚未完成 TLS、访问控制和备份前上传真实资料；
- 不把公网开放作为 P0 的必要条件。

正式容量校验器只接受 `local-readiness` 和 `target-release`，因此本轨道使用独立的
`scope=portfolio-smoke` manifest 和报告校验器，避免污染正式证据链。

## 3. P0 交付物

| 文件 | 作用 |
|---|---|
| `scripts/portfolio_smoke.py` | 生成 parser 真实计数的合成语料；运行 dry-run；提供 P1 健康探针入口 |
| `scripts/portfolio_report.py` | 校验路径、哈希、时间窗口、样本来源、敏感字段和基本统计 |
| `tests/industrial/test_portfolio_smoke.py` | 回归验证语料计数、篡改检测和 dry-run 语义 |
| `docs/industrial/portfolio-environment.example.json` | P1 真实主机环境元数据模板；含占位符，不能直接作为证据 |
| `portfolio_manifest.json` | 独立的运行元数据和证据文件哈希 |
| `corpus_manifest.json` | 每个文件的大小、SHA-256 和 parser 实测 chunk 数 |
| `samples.jsonl` | 只记录时延、状态码、错误码和资源数值，不记录问题正文、回答或凭据 |

### Dry-run 命令

在项目根目录执行，`--output` 必须指向一个不存在的新目录：

```powershell
./.venv/Scripts/python.exe scripts/portfolio_smoke.py `
  --dry-run `
  --output artifacts/portfolio-smoke-p0-dry-run-<run-id> `
  --documents 4 `
  --chunks-per-document 25 `
  --registered-users 3 `
  --concurrency 5
```

dry-run 成功时报告状态为 `fixture`，不是 `passed`。重新校验时必须显式写出：

```powershell
./.venv/Scripts/python.exe scripts/portfolio_report.py `
  artifacts/portfolio-smoke-p0-dry-run-<run-id> `
  --allow-fixture `
  --report artifacts/portfolio-smoke-p0-dry-run-<run-id>/report-recheck.json
```

不带 `--allow-fixture` 时，校验器会拒绝把 dry-run 当作运行证据。

## 4. P0 验收门

P0 只有在以下条件全部满足后才算完成：

1. 合成 Markdown 经 `app.ingest.parse` 和 `app.ingest.chunks` 实际解析，manifest 中的总 chunk 数与各文档之和一致；
2. `environment.json`、`corpus_manifest.json`、`samples.jsonl` 的 SHA-256 可重算；
3. 任意修改语料、样本或环境后，报告校验器拒绝运行；
4. dry-run 明确输出 `scope=portfolio-smoke`、`run_mode=dry-run`、`formal=false`、`formal_claim=none`；
5. 样本包含 preflight、steady observation 和资源夹具，但 fixture 的延迟/资源数值不进入运行指标汇总；
6. 环境与样本中不能出现 password、token、session、question、quote、prompt 或数据库 URL；
7. 现有正式容量测试和 acceptance manifest 不被修改。

## 5. P1 真实主机接口

P0 提供一个只记录健康状态的真实入口，供上机后先做低成本预检：

```bash
python scripts/portfolio_smoke.py \
  --real-health \
  --base-url https://<temporary-host> \
  --environment-json <real-environment.json> \
  --output artifacts/portfolio-smoke-health-<run-id>
```

它只访问 `/livez`、`/readyz`（或显式指定的路径），不保存响应正文和认证信息。
`environment.json` 中的 `tls_verification` 必须如实记录为 `verified` 或 `disabled`。
认证后的 parse/index/query、故障注入和宿主资源采样属于 P1，不在 P0 dry-run 中虚构。
健康入口成功时报告状态为 `health-only`，这只证明探针请求，不代表应用已经完成认证、索引或 RAG 查询。

P1 实际主机与预检结果：

1. 因系统镜像库存限制，实际采用优云智算上海二 B、Ubuntu-nvidia 22.04 系统 VM、RTX 5090 32GB、16 vCPU、约 96GB RAM，按量 3.15 元/小时；
2. 无卡模式完成 Docker/Compose、不可变应用镜像、模型下载和 manifest；有卡模式只用于 CUDA、vLLM 和真实 smoke；
3. `nvidia-smi`、Docker daemon、parser 沙箱、PostgreSQL/Alembic 0006、HTTPS proxy 均通过；
4. vLLM 0.10.2 的 generation/embedding 协议 4/4 和合成 parse/index/query 全链路通过；
5. 模型端点保持 loopback，公网只暴露临时自签名 HTTPS；正式域名/证书未完成。

已有 AutoDL RTX 3090/vLLM 协议 smoke 只能证明协议组合可行，不能证明新供应商的驱动、Docker
和完整团队栈已经可用。

## 6. 成本与安全边界

- 本次 RTX 5090 按量价格为 3.15 元/小时，安装和模型下载优先在无卡模式完成；实际账单仍以控制台为准；
- 使用按量、非抢占式实例，并设置自动关机；证据归档后销毁实例；
- 模型端口、PostgreSQL 和 Docker socket 不对公网开放；
- 默认只允许本人和试用用户 IP，公网域名、TLS 和大陆备案另行决策；
- 首轮只用合成数据，真实资料必须先完成授权确认、访问控制和备份。

## 7. 简历使用口径

可写成“在 Ubuntu 22.04 + RTX 5090 32GB 临时 GPU VM 上完成 Docker Compose、vLLM、PostgreSQL/pgvector
和隔离解析器的真实部署，完成合成 parse/index/query 全链路、1/2/5 路混合模型端点短探针、外部 Edge 认证访问、generation/embedding 单进程故障的应用降级与自动恢复，以及数据库中断恢复、单次 query-worker 租约重领、全新目标备份恢复和双镜像健康回滚”。不得扩写为受信任 TLS、授权资料规模 RPO/RTO、正式 registry 发布、长期可用性或正式容量结论。

不得写成“正式工业化容量验收通过”“支持 20 并发”或“生产环境稳定运行”，除非另行完成正式
T-075 和 target-release 门禁。

## 8. 后续顺序

1. 在本机运行 P0 dry-run 和全部 portfolio 回归测试；
2. 复核输出目录中没有凭据或业务正文；
3. 再创建临时 Linux/GPU 主机并执行三命令预检；
4. 通过预检后扩展到真实 vLLM、Compose、认证链路和小规模负载；
5. 归档证据、销毁实例并更新工业化状态；
6. 只有确实需要正式容量证明时，才启动原 T-075 阶段 F。

## 9. P0 验证记录

2026-09-14 本机验证结果：

- `tests/industrial/test_portfolio_smoke.py`：10 passed；
- 全量 pytest：241 passed、73 skipped；
- Ruff（新增脚本与测试）：通过；
- `scripts/release_check.py`：通过，330 个源文件；
- 默认 dry-run：成功生成 parser 实测 100 chunks 的合成语料，报告状态 `fixture`；
- `git diff --check`：通过。

P1 运行证据见 `artifacts/compshare-p1-cpu-20260914/`、`artifacts/compshare-p1-gpu-20260914/`、
`artifacts/portfolio-health-compshare-20260914-02/`、`artifacts/compshare-p1-worker-shutdown-20260915-02/`、
`artifacts/compshare-p1-fault-recovery-20260915/`、`artifacts/compshare-p1-browser-20260915-02/` 和
`artifacts/compshare-p1-model-recovery-20260915-02/`。它们只证明本次临时主机的合成工程 smoke，不能替代正式质量、容量和发布验收。浏览器证据使用临时自签名证书并如实记录 `tls_verification=disabled`，未持久化凭据、Cookie、CSRF token、问题、回答或来源正文。模型恢复证据只执行 generation/embedding 各一次进程故障，不代表长期可用性。
