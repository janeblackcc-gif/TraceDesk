# 工业化实施状态

依据：`specification/design-baseline-1.0/`（原始规范已原样保留）。
前序会话 `01a07b92-ea8c-7c42-a448-0b65d6cc9890` 停在文档审查入口。
2026-09-09 本会话开始实施；没有修改历史失败记录，没有提交或发布。

| 任务 | 当前状态 | 证据/下一步 |
|---|---|---|
| T-001 | 本机验证通过 | RC2 179 passed；133 个 Git 输入及规范哈希已冻结/复核 |
| T-002 | 本机验证通过 | 3 份 ADR；39 个 T、20 个 PC、21 个 FA 无孤立项 |
| T-010/T-011 | 编码及本机验证通过 | PostgreSQL 18.6 + pgvector 0.8.6；Alembic 0001–0006；约束、重复迁移和失败保护 |
| T-012 | 编码及本机验证通过 | 内容寻址、原子去重、损坏保护、受控 GC；Windows 扩展路径并发问题已修复，300 轮发布通过 |
| T-013 | 编码及本机验证通过 | 正常/损坏/孤儿/向量/WAL/文件名冲突预检；真实源库有非空 WAL，预检阻断 |
| T-014 | 构造库验证通过；真实迁移未执行 | 重复迁移、杀进程续跑、文本哈希、旧 ID、隔离资料保留；真实管理员及维护窗口待提供 |
| T-020/T-021 | 编码、本机及目标认证链验证通过 | 一次性初始化、Argon2id、登录限流/审计、会话/CSRF、禁用与密码变更撤销；目标公网 HTTPS/Edge 会话 Cookie、CSRF 和登出撤销已验证，自签名 TLS 不冒充受信任证书 |
| T-022 | 已实现接口验证通过 | 用户/KB/上传/来源/任务/query/trace/export/审计已鉴权；eval 接口待接入 |
| T-023 | 本机在途查询验证通过 | 权限变化时拒绝提交，并清除问答、来源与 trace |
| T-024 | 编码及本机验证通过 | SKIP LOCKED 领取、owner+attempt 校验、事务副作用、请求幂等 |
| T-025/T-026 | 隔离库、目标关停及单次崩溃恢复通过；正式故障容量待外部 | PID 1 优雅关停已复验；目标无卡 VM 上对持有真实查询租约的 query-worker 单次 SIGKILL，首次 attempt 以 `LEASE_EXPIRED` 回队，第二次 attempt 成功；多任务/长时容量仍待 T-075 |
| T-031 | 上传至索引激活链路已验证 | 原文件/修订/任务/来源；自动建索引、generation 原子激活、陈旧 parse 拒写 |
| T-034 | 隔离解析实现及目标 Linux smoke 通过 | 无网络容器、只读文件、内存/CPU/临时空间/输出限制、超时/取消/孤儿回收；优云智算系统 VM 已验证 parse-worker 经宿主 Docker socket 启动受限 parser；目标故障恢复仍纳入 T-075 |
| T-032 | 编码及本机验证通过 | 删除 tombstone、在途任务取消、7 天恢复、引用重用与可重试回收站 GC |
| T-040 | 合成逐题兼容验证通过 | 40 题 × BM25/hybrid，80 组排名差异 0、版本越界 0；见 ADR 0004 |
| T-041–T-044 | 编码及本机验证通过 | 1024 维/digest/profile、分批嵌入、manifest/lease 检查、原子激活和回滚、模型截止时间/取消 |
| T-041–T-044/vLLM | 适配层、目标协议、合成全链路及单进程恢复通过 | RTX 5090 32GB 系统 VM 上 generation/embedding 4/4 协议通过，并完成 PostgreSQL + secure parser + parse/index/query；两个模型端点各单次 SIGKILL 后应用返回 `partial/MODEL_CONNECTION_FAILED`，systemd 自动恢复后重新 `answered`；正式质量和容量仍待门禁 |
| T-050–T-052 | 编码及本机验证通过 | 异步问答、状态/trace/导出、旧来源鉴权适配、权限与资料 epoch 复核、partial 语义 |
| T-053 | 本机团队及目标认证浏览器 smoke 通过 | `browser-team-20260911-05` 完成 10 项本机隔离验收；`compshare-p1-browser-20260915-02` 在目标公网入口完成登录、Cookie/CSRF、证据查询、引用、导出和登出共 8 项 |
| T-060/T-061 | 授权语料、真实题集双标和 holdout 封存完成 | 10 份授权文档、40 题（28 dev/12 holdout）、两名人工复核者、9 项联合裁决且无未决分歧；正式 v2 数据校验通过 |
| T-062 | dev-only 稀疏阶段完成；dense/hybrid 待 GPU | 28 道 dev、7 份文档、206 chunks；BM25 Hit@4=20/21、证据组 Recall@4=41/43，6 锚点上下文完整率=21/21；多查询和邻接未增召回，暂不触发 chunking v2 |
| T-063 | 非 GPU 协议、私有模板和聚合器完成；模型运行未执行 | 28 道 dev 的双人评分模板已生成；待 T-062 完成后固定检索配置、生成 dev 回答并完成人工复核 |
| T-064 | holdout 已封存；非 GPU 预检完成；质量运行未执行 | 阈值仍是未冻结草案，holdout 运行次数为 0；当前 12 题在 Wilson 95% 置信策略下不足以形成正式质量 PASS |
| T-070/T-071 | 本机验证通过 | 白名单 JSON 日志、OTel span、Prometheus、审计分页权限、存储/任务 readiness、保留策略 |
| T-072 | 本机全门禁通过 | 依赖锁和 pip-audit、strict mypy、ruff、OpenAPI 快照、CI 检查；目标 CI 尚未执行 |
| T-073 | 隔离构造库及目标 VM 合成恢复通过 | 目标无卡 VM 完成 PG custom dump + objects、独立校验、全新数据库/目录恢复、33 表指纹和应用读取；授权真实资料规模下的 RPO/RTO 仍待演练 |
| T-074 | 隔离库迁移/失败回滚及目标双镜像健康回滚通过 | 0005→0006 成功/DDL 失败回滚已演练；目标无卡 VM 以两个本地不可变 image ID 完成旧→新→旧→新切换且每阶段 HTTPS READY；正式 registry `tag@sha256` 发布切换仍待真实发布窗口 |
| T-075 | 判定器已实现；正式负载驱动和目标容量待完成 | `capacity_report.py` 可执行正式证据判定；30 分钟、50k chunk、20 用户、5 并发和 10 场景的真实驱动/证据尚未提供 |
| P-PORTFOLIO | P0、P1 短期简历证据轨道完成 | Ubuntu 22.04/RTX 5090 系统 VM 上完成 Compose、PostgreSQL、secure parser、vLLM、合成 parse/index/query、公网 health-only、外部 Edge 认证链、1/2/5 混合端点短探针、generation/embedding 单进程故障降级与自动恢复，以及数据库重启、单次 query-worker 租约恢复、备份/全新目标恢复和双镜像健康回滚 |
| T-080 | 本机 readiness 已汇总；正式放行阻塞 | `acceptance-local-20260911-04`：14 PASS、7 SKIPPED-BLOCKED；target-release 不可伪造 |
| T-081 | 证据 schema、聚合器和执行协议完成；真实试用未执行 | 仍需 pilot 授权、真实用户任务、事先阈值和责任人签字；现有资料仅授权 evaluation，不能直接用于试用 |

## 最新验证与证据

- 最近完整通过：`artifacts/integration-20260911-03/`，**285 passed，0 skipped**；包含依赖锁、pip-audit、mypy、ruff、OpenAPI、部署模板、审计/指标/健康检查、备份恢复、升级和评测输入 schema。
- 同轮 `pip check`、追踪矩阵、Alembic metadata diff 和 release check 通过；release 报告 317 个文件，保留 2 个第三方弃用警告。
- `artifacts/browser-team-20260911-05/`：当前 secure parser 镜像上的真实 HTTPS + Edge + PostgreSQL 通过 10 项检查，包含新版替换的旧版保持与原子激活。
- `artifacts/acceptance-local-20260911-04/summary.json`：本机 readiness 汇总为 `blocked`（14 项 PASS、7 项外部门禁 SKIPPED-BLOCKED）；这不是 target-release 证书。
- `artifacts/integration-20260909-17/` 的保留策略首轮 SQL 类型失败仍原样保留；当前修复已由最新全量复验覆盖。
- 全部历史失败日志保留；不使用后续成功覆盖失败记录。
- 解析镜像构建：`artifacts/M3/T-034/build-02.log`；原始失败日志与较早 run 保留。
- 当前真实 SQLite 预检：`artifacts/M1/T-013/current-database-preflight-02/`。
  非空 WAL 约 1.9 MB，错误码 `SOURCE_REQUIRES_OFFLINE_CHECKPOINT`；未执行 checkpoint、迁移或切流。
- GitHub CI 配置已加入 PostgreSQL 与解析镜像测试、固定 action/image 版本和结果 artifact；**尚未推送或执行远程 CI**。
- `artifacts/model-smoke-20260911-02/`：当前安全 parser 与配置中的 Ollama `127.0.0.1:11435` 完成一次合成中文资料 parse/index/query；这不是质量证据。
- vLLM 适配阶段本机协议回归：定向测试通过；Compose 模板门禁通过。临时 Linux/RTX 3090 运行 `artifacts/vllm-gpu-smoke-20260912-03/protocol-smoke/`，模型、1024 维嵌入、查询规划、结构化生成 4/4 通过；`start.py --check --require-models` 通过。另有 5 路 chat + 5 路 embedding 的短并发探针通过，但明确不作为正式容量证据。
- 临时 GPU smoke 使用 vLLM `0.10.2`、Torch `2.8.0+cu128`、Transformers `4.57.6`、Tokenizers `0.22.1` 和 `huggingface-hub 0.36.0`；未固定 Transformers 时的 5.17.0 tokenizer 失败和完整 JSON Schema 空白循环均保留在早期 artifact，当前适配器已记录并规避这些已复现问题。
- 本轮收尾复验：全量 `pytest` 为 231 passed、73 skipped；strict mypy 覆盖 12 个文件、Ruff、OpenAPI、release check、冻结基线校验均通过。Compose 模板配置哈希为 `227024e04eed17f9e0bec601122879ca406612f9717b76784002e80ef414bb92`。
- 所有历史失败日志和早期浏览器/模型记录保持不变；后续成功结果不覆盖失败记录。
- 上述本机模型、浏览器和恢复记录不等于语义 holdout、目标容量、生产部署或真实用户试用。
- P0 portfolio smoke dry-run 已验证独立 `scope=portfolio-smoke` 证据格式；10 项回归测试通过，全量 pytest 为 241 passed、73 skipped，报告状态为 `fixture`，不构成容量或发布证据。计划与命令见 [plan-portfolio-smoke.md](plan-portfolio-smoke.md)。
- `artifacts/compshare-p1-cpu-20260914/`：优云智算无卡模式完成 Docker 29.1.3、Compose 2.40.3、PostgreSQL/Alembic 0006、HTTPS 和隔离 parser 预检；模型 revision 及逐文件 manifest 已核验。
- `artifacts/compshare-p1-gpu-20260914/status-final.json`：上海二 B 的 Ubuntu 22.04.4/RTX 5090 32GB、驱动 570.153.02 上，vLLM 0.10.2 协议 4/4 和合成 PostgreSQL + parser + parse/index/query 全链路通过；短 1/2/5 混合 chat/embedding 探针全部成功。它明确为 `formal_claim=none`，不是质量、holdout 或正式容量证据。
- `artifacts/portfolio-health-compshare-20260914-02/report.json`：从本机访问临时公网 HTTPS，12 个健康请求全部成功，状态为 `health-only`；证书为临时自签名，`tls_verification=disabled`。
- 2026-09-15 P1 收尾复验：全量 pytest `242 passed, 73 skipped`；Ruff、目标 CLI mypy、portfolio report 重新校验、Compose/nginx 模板门禁、release check（334 文件）和 `git diff --check` 均通过。
- 2026-09-15 关停诊断与目标复验：首次修复仅增加信号展开，但 PostgreSQL notification 路径后仍缺少 shutdown event 的主循环检查；该失败原样保留。补充事件兜底和 1 秒空闲等待后，在优云智算无卡 VM 上以新镜像执行标准 Compose stop，parse/index/query/gc 四类 worker 在约 1 秒内全部退出 0、`OOMKilled=false`，并停止 PostgreSQL；证据见 `artifacts/compshare-p1-worker-shutdown-20260915-02/`。当前只证明空闲 worker 优雅关停，不覆盖在途任务；全量 pytest 更新为 `244 passed, 73 skipped`。
- `artifacts/compshare-p1-fault-recovery-20260915/`：无卡目标 VM 上，数据库故障链为 `200 READY → 503 DATABASE_UNAVAILABLE → 200 READY`，四类 worker 各自动重启一次；真实 query-worker 持有合成 evidence/BM25 查询租约时被单次 SIGKILL，退出 137 且 `OOMKilled=false`，操作者启动后首次 attempt 以 `LEASE_EXPIRED` 回队，第二次 attempt 成功并持久化响应。首次代理尚未就绪的失败日志原样保留；这不是正式 T-075 容量证据。
- 同一目标无卡 VM 使用当前备份工具完成合成部署的 PostgreSQL custom dump 与 1 个 immutable object 备份、独立哈希校验，并恢复到全新数据库和全新对象目录；schema `0006`、33 张表的行数/内容指纹一致，恢复后的应用探针可读既有查询状态。备份与恢复各约 2 秒，仅反映微型合成数据，不能当作授权资料规模下的正式 RPO/RTO。
- 同一目标无卡 VM 以本地不可变 image ID 执行 Web 旧版基线、新版部署、旧版回滚及恢复期望新版；四阶段均为 healthy 且 HTTPS `/readyz` 返回 `200 READY`，最终配置保持新版并正常停止。该证据没有 registry `tag@sha256`，不冒充正式发布制品切换。
- `artifacts/compshare-p1-browser-20260915-02/status.json`：从本机 Microsoft Edge 访问目标公网 HTTPS，登录页、Secure/HttpOnly/SameSite=Strict 的 `__Host-` 会话 Cookie、CSRF、授权知识库、异步 evidence 查询、精确引用高亮、授权导出及登出后 `/api/v1/me` 返回 401 共 8 项通过；仅使用合成标记，导出正文未持久化，临时账号已禁用且活动会话为 0。证书为临时自签名，`tls_verification=disabled`。新增 driver 定向测试为 11 passed、1 skipped（Windows 符号链接权限），全量 pytest 为 255 passed、74 skipped；Ruff、strict mypy、py_compile、release check（336 文件）和 `git diff --check` 通过。
- `artifacts/compshare-p1-model-recovery-20260915-02/status.json`：RTX 5090 上 generation 和 embedding 端点分别执行一次主进程 SIGKILL；故障窗口中另一端点保持可用，真实 query-worker 的 `profile='ollama'` 查询均以一个来源安全降级为 `partial/MODEL_CONNECTION_FAILED`。systemd 将 generation PID `1579→4178`、embedding PID `1880→4613`，两者 restart count 均 `0→1`；恢复后端点功能探针和最终应用查询 `answered`。该证据为 7 项合成短测，不代表长期可用性或正式容量。
- 2026-09-15 真实评测数据准备：`eval/real/private/tracedesk-pilot-v1/formal_validation_report.json` 以 `--formal` 通过，覆盖 10 份授权文档、40 题、28 dev/12 holdout、两名人工复核者和 0 个未决分歧；封存 dataset hash 为 `4f04a973e35c17c82b812c80a5ad8ba5964810645bf5645b0c5c4a09cb1e2986`。原始 A/B 决定和 9 项联合裁决保留在 Git 忽略目录；目前没有执行 holdout，也不构成质量 PASS。
- T-062 首轮 CPU 稀疏消融见 `eval/real/private/tracedesk-pilot-v1/dev-only/experiments/t062-sparse-20260915-03/status.json`：运行边界只含 28 道 dev 题和 7 份 dev 文档，holdout 载入/运行均为 0。当前 43 个 gold evidence groups 均能落入现有 chunk；唯一 Top4 失败在第 5 个稀疏锚点恢复，因此先做 frozen vLLM dense/hybrid 对照，不改 chunking。该次仅为单轮 CPU 开发实验，不是质量或性能门禁。
- 非 GPU 质量收尾：T-062 sparse-only 候选选择报告按规则返回 `blocked`，没有越权冻结最终检索配置；T-063 已生成 28 行 dev-only v2 人工评分模板；T-064 已生成未冻结阈值草案并完成 metadata 预检，未执行 holdout。预检显示 holdout 总数 12（9 answerable、3 unanswerable），在双侧 Wilson 95% 策略下，即使点估计完美，strict task pass、high-severity fact completeness、no-answer recall 和 false refusal 仍为置信度不足；不得把它解释为模型失败或质量 PASS。
- 2026-09-15 非 GPU 工具链复验：相关定向测试 7 passed；全量 pytest `262 passed, 74 skipped`；新增质量/试用代码 Ruff 与 strict mypy 通过。T-081 的真实试用聚合器已就绪，但没有模拟用户或 fixture 证据被用于关闭门禁。

## 接续顺序

1. P0/P1 短期简历证据轨道已完成；当前临时 VM 的模型和 Compose 服务均已停止，可由操作者在控制台关机并按预算决定是否释放实例。受信任域名/TLS 属于长期公开访问阶段。
2. T-060/T-061 及 T-062–T-064 的非 GPU 工具/预检已完成；下一步在 GPU 上只对 dev 执行 frozen vLLM dense/hybrid 对照，再按已准备协议完成 T-063 dev 生成与双人评分。不得冻结阈值或运行 holdout，直到检索/生成配置选定并处理当前 holdout 的置信度样本量缺口。
3. 如仍追求正式工业化容量结论，在目标环境执行至少 30 分钟、50,000 chunks、20 用户、5 并发和 10 场景验收，保存 `capacity_report.py --formal` 证据。
4. 先补充资料的 pilot 授权，再由真实用户完成持续试用并记录任务结果、核验耗时、严重错误和责任人签字；最后生成 target-release manifest。

本文件为当前实施状态入口；原始规范和历史质量失败保持不变，不另建重复任务追踪器。

## 待外部输入

- 首批部署边界、是否已有 OIDC：暂按单组织内网、本地账号。
- 真实数据迁移的管理员身份：由真实操作者提供，测试账号不能成为真实管理员。
- M6：真实资料、40 题双标、holdout 封存、dev 稀疏基线及非 GPU 质量门禁工具已完成；仍需 vLLM dense/hybrid dev 对照、T-063 dev 生成/双人评分、样本量方案和质量负责人对正式阈值的事先确认。
- M8：正式 30 分钟/50k chunk 容量窗口和预算；短期 Linux/GPU 核心 smoke 已完成。
- M9：资料的 pilot 授权、真实用户、试用周期与责任人确认。

独立工程工作继续进行；这些条件仅阻塞对应的数据搬迁、质量放行、部署或试用步骤。
