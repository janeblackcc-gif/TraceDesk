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
| T-060/T-061 | v1 历史证据保留；v2 替代数据集已封存 | v1 的 10 份授权文档、40 题和双标记录保留，旧 12 条 holdout 因问题文本提前暴露而降级为 dev；全新 v2 holdout 140 条已完成双标、溯源和正式数据集封存，模型运行次数为 0 |
| T-062 | v1 历史选择保留；v2 dev 五臂选择完成 | v2 的 60 条 dev/15 份文档/650 chunks 已物理隔离；BM25 证据组 Recall@4=86/92，dense=59/92，最佳 hybrid=69/92；仅 BM25 eligible 并选定，holdout 载入/运行均为 0 |
| T-063 | prompt-v2 已选为非正式 dev generation 候选 | 双评 0 实质分歧；点估计全部达标但固定 dev 小分母使报告保持 `insufficient-confidence`。质量负责人明确接受该限制，选择证据不宣称 PASS，也不授权 holdout |
| T-064 | v2 阈值已冻结；首次 holdout metadata readiness 通过 | 入选代码已提交并推送，冻结阈值绑定 T-062/T-063 选择证据；readiness 为 `ready-for-first-holdout`、0 blocker，140 条 holdout 仍为 0 次运行 |
| T-070/T-071 | 本机验证通过 | 白名单 JSON 日志、OTel span、Prometheus、审计分页权限、存储/任务 readiness、保留策略 |
| T-072 | 本机及 GitHub CI 全门禁通过 | 依赖锁和 pip-audit、strict mypy、ruff、OpenAPI 快照、CI 检查；最新 `main` run 的 5 个 job 全部成功 |
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
- GitHub `main` 已推送至 `8c6939516d8c4a345294a6802ae9fa4304ae1b49`；Actions run `34942595463` 的 5 个 job 全部成功，PostgreSQL industrial suite 为 339 passed、0 skipped。
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
- 2026-09-15 真实评测数据准备：`eval/real/private/tracedesk-pilot-v1/formal_validation_report.json` 当时以 `--formal` 通过，覆盖 10 份授权文档、40 题、28 dev/12 holdout、两名人工复核者和 0 个未决分歧；封存 dataset hash 为 `4f04a973e35c17c82b812c80a5ad8ba5964810645bf5645b0c5c4a09cb1e2986`。2026-09-16 开发代理在格式核对中提前看到了 12 条 holdout 问题文本，原始证据仍完整保留，但该 holdout 从此禁止正式使用并在 v2 降级为 dev；未读取其答案、标签、证据或来源正文，模型 holdout 运行次数仍为 0。
- T-062 首轮 CPU 稀疏消融见 `eval/real/private/tracedesk-pilot-v1/dev-only/experiments/t062-sparse-20260915-03/status.json`：运行边界只含 28 道 dev 题和 7 份 dev 文档，holdout 载入/运行均为 0。当前 43 个 gold evidence groups 均能落入现有 chunk；唯一 Top4 失败在第 5 个稀疏锚点恢复，因此先做 frozen vLLM dense/hybrid 对照，不改 chunking。该次仅为单轮 CPU 开发实验，不是质量或性能门禁。
- 非 GPU 质量收尾：T-062 sparse-only 候选选择报告按规则返回 `blocked`，没有越权冻结最终检索配置；T-063 已生成 28 行 dev-only v2 人工评分模板；T-064 已生成未冻结阈值草案并完成 metadata 预检，未执行 holdout。预检显示 holdout 总数 12（9 answerable、3 unanswerable），在双侧 Wilson 95% 策略下，即使点估计完美，strict task pass、high-severity fact completeness、no-answer recall 和 false refusal 仍为置信度不足；不得把它解释为模型失败或质量 PASS。
- 2026-09-15 非 GPU 工具链复验：相关定向测试 7 passed；全量 pytest `262 passed, 74 skipped`；新增质量/试用代码 Ruff 与 strict mypy 通过。T-081 的真实试用聚合器已就绪，但没有模拟用户或 fixture 证据被用于关闭门禁。
- `eval/real/private/tracedesk-pilot-v1/dev-only/experiments/t062-vllm-20260915-01/`：冻结 vLLM embedding 在物理隔离 dev view 上完成 206 corpus vectors 和全部 dev query vectors。5 个 arm 中 BM25 的证据组 Recall@4 为 41/43；dense 为 25/43，三个 hybrid arm 分别为 28/43、29/43、29/43。预定义选择器仅将 BM25 判为 eligible，并选定 `bm25-single-no-adjacency`；选择证据 SHA-256 为 `b7538ee08af239a6fd35fab6005f1eb946892e5fb0de2d4cb53c6aad814988bc`。该次 `config.scope` 保留了驱动器旧值 `t062-dev-only-sparse-ablation`，但同包 status、5-arm 配置和 model evidence 均为完整模型对照；原证据不改写，后续驱动器已统一为 `t062-dev-only-retrieval-ablation`。该结果是 dev 配置选择证据，不是 holdout 或容量结论。
- `eval/real/private/tracedesk-pilot-v1/dev-only/experiments/t063-vllm-20260915-01/`：固定上述 BM25 配置，使用 `Qwen/Qwen3-4B-Instruct-2507` 在 dev 上顺序生成 28 条输出，21 answered、7 abstained、0 error，P50/P95 为 2143.835/3710.719 ms。两名复核者完成独立评分；7 项 `required_facts_satisfied` 字段误填经操作者确认为非实质分歧并按 2/2 更正，7 道不可回答题的不适用 `false_refusal` 字段按 schema 清空，原始 A/B 文件保持不变。最终评分 SHA-256 为 `2dd2e4a9fe678adf09cf16dfd0dfd06af0166878bade833a8a1de567722ffd01`。
- `eval/real/private/tracedesk-pilot-v1/quality-reports/t063-dev-20260915-01.json`：T-063 dev 聚合状态为 `insufficient-confidence`、`formal=false`。点估计为 strict task pass 28/28、high-severity facts 2/2、claim support 54/54、no-answer recall 7/7、false refusal 0/21，scope/version leakage 和 severe error 均为 0；仅 strict task pass 的 Wilson 95% 下界通过 0.85，其余指标因样本量不足不能判 PASS。报告 SHA-256 为 `64484d6a218fe41e3aaace1795d6ac3a92f87fabb63d75986e0787cd7551f3c5`；阈值仍为 `draft-not-frozen`，holdout run count 仍为 0。
- `docs/industrial/plan-quality-sample-size-v2.md`：根据现有 Wilson 95% 实现冻结扩展方案。v2 共 200 条，其中 dev 60、全新 holdout 140；holdout 为 80 answerable、60 unanswerable，并要求至少 120 个 high/blocker required facts、目标至少 160 个实际 claims。28 份新增候选全部通过解析、哈希和重复预检；revision 02 选取 5 份 dev、18 份未暴露 holdout，保留 5 份 reserve。分组连续执行两次结果一致，selection SHA-256 为 `451ca44795b495f8c41561ab514db8c137299453ac6ffe5108de43023847c763`，materialization SHA-256 为 `e58674ac72de86a22a6f8384aa002d5c26645a2c7b60a0b0779bf872f3c08c32`。
- v2 私有编题工作包已生成 20 个新 dev 题位、140 个全新 holdout 题位、23 行逐文档来源补录项，以及独立复核模板；原工作包 manifest SHA-256 为 `fdd4dee00bdd1d2a86dc87b87d0c1d78f123f5494c5ae658ed3f12b0463f871a`。后续题目和双标已经完成，但原 manifest 作为历史输入保持不改。
- v1 的 40 条 case 已按原始封存哈希机械迁移为 v2 dev，12 条原 holdout 重编号为 `PRD-DEV-029` 至 `PRD-DEV-040`；迁移后为 30 answerable/10 unanswerable、0 holdout，10 份文档哈希全部一致，migration manifest SHA-256 为 `ab4d3f8ca49dd8b139cb07c7c0b725be1041dd64a5e60c77fd13c2137c17103b`。
- 5 份新 dev 文档已完成 20 条 case（10 answerable/10 unanswerable）、52 段最终证据、两份人工复核和 4 项裁决；与迁移数据合并后的 60 条 dev 以非正式校验通过，dataset hash 为 `50986d2947ab035aa54fb518a5891c1676a7eb334d103f0b878ddc02da964bb4`。其精确上游来源已在最终 v2 溯源校验中逐字节核验。
- 2026-09-16 非 GPU 数据准备复验：7 个私有准备脚本通过 `py_compile`；语料分组、编题工作包、dev 起草、v1 迁移、dev 合并和 A/B 复核包均重复执行得到相同哈希；release check 通过 684 个文件，`git diff --check` 通过，抽查的原文、holdout 题位和 dev 数据均被 Git 忽略。
- 2026-09-16 新增 dev 的两份人工复核各完成 20 行，匿名 reviewer ID 不同且无 pending；`PRD-DEV-049/050/054/057` 的 4 项分歧经操作者确认后补齐缺失证据上下文并全部裁决。新增 20 条最终标签含 2 名 reviewer、4 个 resolved dispute、0 个 unresolved dispute 和 52 段证据；与 v1 迁移数据合并后的 60 条 dev 全部双标，非正式校验通过，dataset hash 为 `50986d2947ab035aa54fb518a5891c1676a7eb334d103f0b878ddc02da964bb4`。
- 2026-09-16 重做后的 holdout 含 140 条、18 份文档、80/60 answerability、六类 answerable 题型、五类 unanswerable 边界、40 个 high/blocker case、120 个高严重度事实和 200 段逐字证据；本地哈希、引文存在性和冻结配额通过。两名不同 reviewer 各完成 140 行和 7 个决定字段，0 分歧、0 一致拒绝；复核校验报告 SHA-256 为 `e527058a709ae77c2c23d42796c741d8dfd52fecf8127579841f93439c6da961`。
- 同轮来源真实性复核最初发现非官方占位仓库，补齐后又发现 23 个 `retrieved_at` 把北京时间误标为 UTC；两轮失败证据均原样保留。最终派生版本使用真实在线重新获取时间，来源校验 23/23 可访问且全部与本地文件逐字节一致，报告 SHA-256 为 `ea7086d94f984ddd07c05c178f14a6932340aeb6767297fddd676b37f1c3ff84`。
- holdout 复核派生校验确认两名 reviewer、0 分歧、0 一致拒绝，并将当前 authored 数据的 1,220 个语义值与原 review packet 逐项绑定，0 mismatch；报告 SHA-256 为 `a2f558267199ad60a94514de7fff46fbf2e1353d08906f7f3f56b99ede78e625`。最终 authoring 合并校验无 blocker，SHA-256 为 `55e26ea435a3abd589c61b2ea362c5c54909f8d0115f6e6cac225f6ad9876123`。
- `eval/real/private/tracedesk-prd-real-eval-v2/formal-reviewed-sealed-20260916-01/` 已组装 33 份语料、200 条 case、60 dev/140 holdout、120 answerable/80 unanswerable；所有 case 至少两名 reviewer，0 未决分歧。`check_eval_dataset.py --formal` 通过，dataset hash 为 `b04bde9ab160d78f2197c5fc8aeb1ddc60469185b455ede1fd852c5d20eb7709`，正式校验报告 SHA-256 为 `523f885212fcce20517586de7ec410266ca5ae3efbe38ddc2e4fcbb3f57bbe42`。该 formal 只证明数据集准备与封存，不是模型质量 PASS；阈值未冻结且 holdout 运行次数仍为 0。
- v2 dev-only 视图已物化 60 条 dev、15 份文档且 `contains_holdout=false`。CPU sparse run `t062-sparse-20260916-02` 覆盖 650 chunks 和 92 个证据组：`bm25-single-no-adjacency` Hit@4 为 39/40、evidence-group Recall@4 为 86/92、context complete 为 37/40；选择器以 `DENSE_HYBRID_DEV_ARMS_NOT_COMPLETED` 阻塞，报告 SHA-256 为 `110e9cf09e950400ee048cf8ba1929b7c3b822f4b7005f5c28c0913f205ab2cb`，没有越权选择最终配置。
- 已生成绑定 v2 dataset hash 的默认阈值草案，SHA-256 为 `bf4eec63de42fa2ed971eb1ecd19733cde06994bc43301299f8845c3a0f50290`。pre-GPU readiness 修正了不可在运行前观察的 claim 分母死锁：claim support 改为首轮运行后置信度检查；当前只剩 `T062_RETRIEVAL_SELECTION_NOT_COMPLETE`、`T063_GENERATION_SELECTION_NOT_COMPLETE`、`QUALITY_THRESHOLDS_NOT_FROZEN` 三个 blocker，报告 SHA-256 为 `79e2b2465d7115c38948d17f1ace99f552d203f42747f377597793e97eb25ce4`，holdout scoring access 为 false。
- 2026-09-15 GPU 质量阶段代码复验：T-062/T-063 定向测试 2 passed；全量 pytest `265 passed, 74 skipped`；Ruff、strict mypy（跳过第三方导入实现）、py_compile、release check（683 文件）和 `git diff --check` 通过。
- `eval/real/private/tracedesk-prd-real-eval-v2/formal-reviewed-sealed-20260916-01/dev-only-v2-20260916-01/experiments/t062-vllm-20260916-01/`：RTX 5090/vLLM 0.10.2 对同一 dev view 完成五臂检索。BM25 的 evidence-group Recall@4 为 86/92、context complete 37/40、P95 77.528 ms；dense 为 59/92，最佳 hybrid 为 69/92。预定义选择器只将 `bm25-single-no-adjacency` 判为 eligible，选择文件 SHA-256 为 `c56d504c8902ac6b894142dd6b8833b7a11c739eeb54a55afe1f903f315ff922`；这是 dev 配置选择，不是 holdout 或容量结论。
- T-063 首个完整诊断运行 `t063-generation-v2-20260916-02` 保留 60 行和 4 个 error，状态 SHA-256 为 `b94679999ea846d4afc13d51c342229d66c831eedfddde85e72cae81cc2b2f2f`。根因分别为 runner 未复用真实服务的零证据门控、generation systemd 的 8192 上限低于应用 16384 上下文契约，以及 vLLM `json_object` 文字约束漏写 requirements 最多 6 项；失败证据未覆盖。
- 修复后 `t063-generation-v2-20260916-03` 在同一 60 条 dev 上完成，38 answered、22 abstained、0 error，P50/P95 为 2685.002/6821.807 ms；status SHA-256 为 `f172e16dbaed32170baceffc366bbbdfb5474c1f70c42d78c5585968cec54dd7`，私有输出 SHA-256 为 `bab919cb9ea195395a471e6b5770c07808cc902858b08b269c98c9be76cec876`。本地与远端均复核 60 个唯一 dev case、模板 60 行和 holdout 载入/运行 0；两个 vLLM 服务随后停止，显存回落至 1 MiB。
- v2 T-063 的两名复核者已独立完成全部 60 条 dev 评分（Scorer A SHA-256 为 `a7452ad4e4abed0ddba2513e0d5684bf4a0ce8cc0335561aab3be62c173df447`，Scorer B SHA-256 为 `fe7a1adbcad25268edb570ef8b4a841b2290804b218036b653f0195a9e7d3211`），60 行 schema 校验全量通过；双方完成前互不查看。等待后续执行分歧比较与裁决。
- post-dev-generation metadata readiness 仍为 `blocked`，但 blocker 已收敛为 `T063_GENERATION_SELECTION_NOT_COMPLETE` 和 `QUALITY_THRESHOLDS_NOT_FROZEN`；`holdout_accessed_for_scoring=false`、运行次数 0，报告 SHA-256 为 `bff9571c698ff1dc22aa968b2fe1e760a4637b2b37f4c46d281fda3e66c2e195`。
- 本轮收尾复验：全量 pytest `267 passed, 74 skipped, 2 warnings`；T-062/T-063 与 vLLM 定向测试 `12 passed`；Ruff、Python 3.13 strict mypy、release check（685 文件）和 `git diff --check` 通过。
- v2 首个 T-063 候选的两份独立评分各 60 行，reviewer ID 不同，冻结字段/输出哈希一致，0 缺失字段、0 实质分歧；最终评分 SHA-256 为 `c04832dd73971c727ce88584a1771ad6544627e55f5464d1fcd526b1e975ed93`。dev 聚合报告 `t063-dev-v2-20260916-01.json` 状态为 `failed`、`formal=false`，SHA-256 为 `dbb8869b3cc2937b8ec7161d1e4e29d25443c90645a5efabf24d7b49f03837a4`：strict task pass 58/60、claim support 120/120、无 scope/version leakage 或 severe error；high/blocker facts 22/27 低于 95% 门槛，no-answer recall 20/20 与 false refusal 2/40 的点估计分别为 100%/5%，但置信区间仍不足。
- 失败只集中于 `PRD-DEV-011` 和 `PRD-DEV-053`：BM25 已完整命中 2/2 与 3/3 gold evidence groups，模型却把两题各扩成 6 个 requirements，并为未提问的额外机制、机械对象×属性组合或无关资料创建缺口，触发 all-or-nothing false refusal。未改检索、标签或阈值；prompt 候选已增加最少必要 requirements、禁止机械组合和忽略无关 evidence 的约束，相关测试 64 passed，根因报告保存在私有 quality-reports 目录。
- prompt-v2 在 `t063-prompt-v2-targeted-20260916-01` 先重放 `PRD-DEV-011/053`：两题均 answered、各 4 个 requirements、0 missing facts，状态 SHA-256 为 `b894d4e850fa7ef88266daff8dba1432124f9f4d3717e34793c2b4264db57a5d`，输出 SHA-256 为 `52ecdbe7032f36dd60f0b781d4f5172f5f494423fb8569870d5c0a8b741bbb7c`；holdout 载入/运行均为 0。
- 全量新候选 `t063-generation-v2-prompt-v2-20260916-01` 随后完成同一 60 条 dev，40 answered、20 abstained、0 error，P50/P95 为 2595.369/5071.686 ms；状态 SHA-256 为 `32a98e2d71d0edc36d131c3d0c557ba26d58f4c27d55bb5c77119ad0d2262f6d`，输出 SHA-256 为 `3440f3962b97d9d8a898b9a17e33bef8854badd4e16aa1822a1b3550067ca87f`。这只是待人工评分的 dev 生成结果，不是质量 PASS；holdout 仍未载入或运行。
- 新旧逐题哈希比较显示 58 条输出变化；仅 `PRD-DEV-043/047` 完全相同，因此各复核者自己的旧评分按同一 case 和输出哈希规则迁移。两名复核者已按照说明独立完成剩余 58 条 `pending-review` 评分，两份评分文件均全量达到 60 条 `reviewed`（Scorer A SHA-256 为 `a8ad8285d52f671654426aff1bd345fbd1fbcf4bffb69197ab9fc418c28d10ea`，Scorer B SHA-256 为 `bdde1ebf1574cdce75c95a4d4453220702974670d5c6fc6933bd0fe0fab7735b`）；双方完成前互不查看对方文件，未修改旧候选或冻结输入。迁移清单 SHA-256 为 `1093085bd9d956bcd3ff574f3336969d21a12663f6e010752ed7b30d86308442`。远端 generation/embedding 服务均已停止，GPU 显存回落至 1 MiB。
- prompt-v2 两份评分经机械核验均为 60 行、0 pending、0 holdout，复核者 ID 不同，冻结元数据、时间戳和输出哈希与模板及模型输出一致；11 个实质判定字段和 notes 均为 0 分歧，无需裁决。最终合并评分 SHA-256 为 `83f03a9e0849db73fa4cc4b27f40ad4c8a68dcee8d921c253c92056adbb5def4`，reconciliation SHA-256 为 `a0a93d3b9a8d92a16603864ea0676ee1faef665e88d07abb66cbd1a654df6929`。
- prompt-v2 dev 聚合报告 `t063-dev-v2-prompt-v2-20260916-01.json` 状态为 `insufficient-confidence`、`formal=false`，SHA-256 为 `8bdaf161d8ba4344afcec021613c3f6a4939a3626de1be85fc75f8f09e6669ad`。点估计全部达标：strict task pass 60/60、high/blocker facts 27/27、claim support 120/120、no-answer recall 20/20、false refusal 0/40，scope/version leakage 和 severe error 均为 0；其中 high/blocker facts 的 Wilson 95% 下界为 0.875445、no-answer recall 下界为 0.838875，受 dev 固定分母限制不能记为 PASS。聚合时尚未选择候选或冻结阈值，holdout 载入/运行均为 0。
- 质量负责人随后明确接受 prompt-v2 作为非正式 dev 选择，同时保留 `insufficient-confidence` 和 `formal=false`。`candidate-selection.json` 状态为 `selected-dev-generation`，SHA-256 为 `40388a14bb6af0718a7e07d2e11b24d4d1d9b60e9e293ad40dc1e647b02905e4`；它绑定检索选择、生成 config/status/output、最终评分、聚合报告、模型 digest 和三个代码文件哈希，并明确不是质量 PASS、不能授权 holdout。
- post-generation-selection metadata readiness 已清除 `T063_GENERATION_SELECTION_NOT_COMPLETE`，报告 SHA-256 为 `82471fb903d1778846573c92f673ecc3869b54390479393c1029e13eca8b5ad1`，工具 blocker 只剩 `QUALITY_THRESHOLDS_NOT_FROZEN`，且 `holdout_accessed_for_scoring=false`、运行次数 0。流程层面入选的 `app/providers.py`、`app/models/vllm.py` 和 `scripts/run_generation_eval.py` 仍是未提交状态，选择证据因此记录 `SELECTED_CODE_NOT_COMMITTED`；在精确提交并复核哈希前不冻结阈值。
- 入选质量管线已提交并推送至 `9606f70f29cec406496f55c221a38cac7ed00eab`，远端 `origin/main` 与本地一致；三个入选代码文件的 SHA-256 与生成 config 完全一致。派生 commit-binding generation 选择证据 SHA-256 为 `1d79d7920fe86e08ae71228c6cbd56cb513810badd604efc46d9729c929dcbde`，保留源选择的 `insufficient-confidence`，不改写为 PASS。
- `quality_thresholds.json` 已按原草案阈值冻结，SHA-256 为 `1fae09b79e9cdea5b08365a4a87f8ecc0d9a7e30f4dcf4c72901bbd2e8705f59`，绑定 T-062 retrieval 和已提交的 T-063 generation 选择哈希。冻结后的 metadata readiness 状态为 `ready-for-first-holdout`、0 blocker，报告 SHA-256 为 `b513f8914495be5f72a52ca2fbf46d1bf9324d6938775b108d29f3623d5bdf5b`；`holdout_accessed_for_scoring=false`、运行次数 0，尚未消耗唯一一次正式 holdout。

## 接续顺序

1. P0/P1 短期简历证据轨道已完成；当前临时 VM 的模型和 Compose 服务均已停止，可由操作者在控制台关机并按预算决定是否释放实例。受信任域名/TLS 属于长期公开访问阶段。
2. v2 T-062/T-063 配置、代码 commit 和质量阈值均已冻结，首次 holdout metadata readiness 已全绿。下一次有卡窗口只能按冻结配置执行唯一一次 140-case holdout；不得再根据 holdout 调整检索、prompt、模型或阈值，原始输出须立即封存并随后完成双人独立评分。
3. 如仍追求正式工业化容量结论，在目标环境执行至少 30 分钟、50,000 chunks、20 用户、5 并发和 10 场景验收，保存 `capacity_report.py --formal` 证据。
4. 先补充资料的 pilot 授权，再由真实用户完成持续试用并记录任务结果、核验耗时、严重错误和责任人签字；最后生成 target-release manifest。

本文件为当前实施状态入口；原始规范和历史质量失败保持不变，不另建重复任务追踪器。

## 待外部输入

- 首批部署边界、是否已有 OIDC：暂按单组织内网、本地账号。
- 真实数据迁移的管理员身份：由真实操作者提供，测试账号不能成为真实管理员。
- M6：v1 历史证据保留，旧 holdout 因问题文本提前暴露已降级。v2 的 23 份新增文档溯源、60 条 dev、140 条 holdout、双标、派生 finalization、200-case 正式校验和封存均已完成；仍需质量负责人基于 dev 结果事先确认并冻结正式阈值，之后才可执行唯一一次 holdout。
- M8：正式 30 分钟/50k chunk 容量窗口和预算；短期 Linux/GPU 核心 smoke 已完成。
- M9：资料的 pilot 授权、真实用户、试用周期与责任人确认。

独立工程工作继续进行；这些条件仅阻塞对应的数据搬迁、质量放行、部署或试用步骤。
