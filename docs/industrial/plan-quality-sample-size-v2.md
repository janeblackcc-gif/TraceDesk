# 扩展真实质量数据集与样本量方案

状态：数据集准备门禁已完成。60 条 dev 已完成双人复核、4 项裁决；140 条 holdout 已完成题目、答案、事实、200 段逐字证据及两份独立人工复核，0 分歧、0 一致拒绝。23 份新增文档均已核验官方仓库固定 commit，在线获取内容与本地文件逐字节一致；完整 200-case 数据集已通过 `check_eval_dataset.py --formal` 并封存。检索、生成、代码 commit 和正式质量阈值均已冻结，metadata readiness 为 `ready-for-first-holdout`；holdout 推理运行次数仍为 0。

适用范围：T-060 至 T-064。新数据集暂定 ID 为 `tracedesk-prd-real-eval-v2`。当前 v1 的 dev 结果只用于开发决策。2026-09-16 在核对 schema 和编号时，开发代理提前看到了 v1 的 12 条 holdout 问题文本；未读取其答案、标签、证据、来源正文或模型输出，也从未执行 holdout。按本方案的隔离规则，这 12 条已失去正式 holdout 资格，在 v2 中全部降级为 dev。

## 决策

采用学生成本可执行的平衡方案：共 200 条 case，其中 dev 60 条、holdout 140 条。

| split | answerable | unanswerable | 合计 | 用途 |
|---|---:|---:|---:|---|
| dev | 40 | 20 | 60 | 检索、prompt、模型和失败分类，仅用于开发选择 |
| holdout | 80 | 60 | 140 | 配置和阈值冻结后仅运行一次 |
| 合计 | 120 | 80 | 200 | 所有 case 均须双标并处理分歧 |

当前 v1 共 40 条，其中原 dev 为 21/7、原 holdout 为 9/3。隔离事件后，40 条全部作为 dev，因此 v2 需要新增：

- dev：10 条 answerable、10 条 unanswerable，共 20 条；
- holdout：80 条 answerable、60 条 unanswerable，共 140 条；
- 合计仍新增 160 条，其中 answerable 90 条、unanswerable 70 条。

现有 28 条 dev 继续留在 dev。原 12 条 holdout 仅允许按原始哈希机械迁移并重编号为 `PRD-DEV-029` 至 `PRD-DEV-040`；其原始 artifact 保留不改，但不得再支持任何正式 holdout 声明。隔离事件记录和替代分组分别见私有目录中的 `holdout-isolation-incident-20260916-01.json` 与 `document-selection-20260916-02.json`。

## 样本量依据

继续使用 `app.operations.quality_eval.wilson_interval` 的双侧 Wilson 95% 区间和现有阈值。以下是固定错误数下取得统计 PASS 所需的最小分母：

| 指标阈值 | 0 个错误 | 1 个错误 | 2 个错误 |
|---|---:|---:|---:|
| 最低 85% | 22 | 34 | 45 |
| 最低 95% | 73 | 110 | 142 |
| 最低 90% | 35 | 53 | 69 |
| 最高 10% | 35 | 53 | 69 |

v2 holdout 的预设分母和容错为：

| 指标 | 阈值 | 预设分母 | Wilson 95% 下允许的最大错误数 |
|---|---:|---:|---:|
| strict task pass | ≥ 85% | 140 cases | 12 |
| high/blocker 必需事实完整度 | ≥ 95% | 至少 120 facts | 1 |
| claim support | ≥ 95% | 目标至少 160 claims；正式最低 142 | 2 |
| no-answer recall | ≥ 90% | 60 unanswerable cases | 1 |
| false refusal | ≤ 10% | 80 answerable cases | 2 |

scope leakage、version leakage 和 severe error 仍要求为 0，不因扩充样本而放宽。

claim 数取决于冻结模型的真实输出，不能在运行前完全保证。80 条 answerable case 均设计为至少两个可独立核验的事实点，使预期 claim 分母不低于 160；如果首次 holdout 的实际 claim 分母低于 142，只能保留为 `insufficient-confidence`，不得查看结果后补题或重跑同一 holdout。

## 语料与 case 分配

从 28 份候选中选取 23 份有授权、可审计的新增文档，另保留 5 份 reserve，并按文档级 `source_group` 整体分配 split：

- 新增 dev 文档 5 份，每份 4 条 case，均按 2 answerable + 2 unanswerable，合计 10/10；开发代理在分组前抽样查看过的 5 份候选全部固定在这里；
- 新增 holdout 文档 18 份：8 份按 5 answerable + 3 unanswerable，6 份按 4 + 4，4 份按 4 + 3，合计 80/60；这些文档的正文在 revision 02 分组时均未被开发代理读取；
- 单文档不超过 8 条，避免少数来源支配 Wilson 分母；同一文档、版本族和改写模板不得跨 dev/holdout；
- 每份文档记录 owner、授权用途、保留期、精确上游 URL、不可变版本或 commit、SHA-256 和规范化工具版本。项目级许可证不能代替逐文档溯源。原文继续放在 `eval/real/private/`，不进入公共 Git。

新增 holdout 的 80 条 answerable 中，至少 40 条必须是真实 high/blocker 风险任务，每条至少 3 个独立 required facts，使新增语料本身提供不少于 120 个高严重度事实分母。工作包中的 40 个位置仅是候选配额；严重度必须依据业务后果判定，不能仅为凑分母抬高。若真实风险任务不足，就从 reserve 补充语料而不是改标签。

60 条新增 holdout unanswerable 按以下类别覆盖，类别和数量已在编题前固定：

- 资料完全缺失：16；
- 错版本、错范围或跨文档诱导：12；
- 相似实体、相邻数值或对象错配：12；
- 只有部分事实、无法完成全部要求：11；
- 文档内指令、越权要求或外部知识诱导：9。

answerable 任务至少覆盖直接查找、多事实综合、步骤/顺序、比较决策、数值/公式、边界与例外六类；任何单一 `template_group` 不超过 holdout 的 20%。

## 防泄漏与双标协议

1. 先冻结本方案、case 配额、source/template group 分配和 author/reviewer 名单，再开始编题。
2. 编题者只能访问被分配的原始资料；全新 holdout 不作为开发示例，不进入 prompt、开发检索索引或开发 review packet。holdout 编题必须在与开发代理隔离的上下文中完成。
3. 两名复核者独立检查 answerability、required facts、evidence groups、forbidden claims、severity 和 task type。评分前不得看到另一人的决定。
4. 分歧生成单独 adjudication 文件；最终标签必须携带两个 reviewer ID，实质分歧必须记录 adjudicator ID。
5. `check_eval_dataset.py --formal` 必须验证 200 条、60/140 split、source/template group 无交叉、所有证据可解析、两名复核者和 0 个未决分歧。
6. 封存后只物化 dev view；T-062/T-063 只能读取这份物理 dev view。holdout 文档、问题和标签不得上传到开发 GPU 主机。

## 一次性正式执行顺序

1. 在 v2 dev 上重新完成 T-062 检索选择和 T-063 生成双评，记录失败分类；
2. 固定 retrieval、prompt、generation model、model revision/digest、解析/切块版本和代码 commit；
3. 质量负责人审核 dev 汇总后，创建全新的 v2 `quality_thresholds.json`，写入选择证据 SHA-256 并冻结；
4. 运行 readiness，只检查 metadata、分母计划、seal、阈值时间和哈希，不生成 holdout 回答；
5. 在全新输出目录执行唯一一次 holdout 生成；原始输出立即只读封存；
6. 两名复核者独立评分，裁决后运行 `quality_report.py --split holdout`；无论 PASS、FAIL 或 `insufficient-confidence` 都永久保留首轮结果。

如果 holdout 被提前打开、用于调参或重复运行，整个 holdout 降级为 dev，必须用新的 source/template groups 重建 v3。

## 成本与停止条件

- 入选新增语料：23 份，另有 5 份 reserve；新增 case：160 条；人工判定总量：320 份独立评分，加分歧裁决；
- 预计人工投入：编题和证据标注约 20–35 小时，两名复核者各约 12–18 小时，裁决约 2–4 小时；
- GPU 只用于冻结后的 dev/holdout 生成，按当前 4B/RTX 5090 实测，纯生成预计为分钟级，主要成本是人工而不是算力；
- 如果无法完成 23 份入选资料的逐文档溯源、两名独立复核者或上述分母，不运行正式 holdout。现有 T-063 dev 点估计可作为简历工程证据，但继续标记 `insufficient-confidence`，不得写成正式质量 PASS。

## 2026-09-16 数据重做核验

- holdout 结构与内容侧校验：140 条、18 份文档、80/60 answerability、40 个 high/blocker case、120 个高严重度 required facts、200 段逐字证据；本地文档哈希、引文哈希、引文存在性和冻结配额均通过。
- holdout 人工复核：两名不同 reviewer 各 140 行，7 个决定字段全部完成，0 分歧、0 一致拒绝。旧的 review manifest 待填写状态和 README 控制字符作为历史输入保留；派生 review finalization 已绑定原复核文件、语义一致性报告和最终 authored 哈希。
- 初次补录的 23 个 `retrieved_at` 把北京时间误标为 UTC，形成未来时间；该输入和失败报告均保留。最终派生版本使用代理真实在线重新获取的 UTC 时间，并证明除来源三字段外的 1,220 个复核语义值未改变。
- 最终在线来源校验为 23/23 官方固定 commit、23/23 可获取、23/23 与本地文档逐字节一致；完整数据集为 33 份语料、200 条 case、60 dev/140 holdout、两名复核者、0 未决分歧。
- `check_eval_dataset.py --formal` 返回 `passed`，dataset hash 为 `b04bde9ab160d78f2197c5fc8aeb1ddc60469185b455ede1fd852c5d20eb7709`。这里的 formal 仅表示数据集构造和封存门禁通过，不代表模型质量 PASS。

## 2026-09-16 dev GPU 执行状态

- T-062 在物理 dev-only 视图上完成 BM25、dense 和三组 hybrid：BM25 evidence-group Recall@4 为 86/92，dense 为 59/92，最佳 hybrid 为 69/92；预定义选择器仅选定 `bm25-single-no-adjacency`。
- 固定该选择后的首个 T-063 候选覆盖 60 条 dev，38 answered、22 abstained、0 error；输出和人工评分模板已下载并逐文件验哈希，holdout 载入和运行均为 0。
- 首个候选的两份独立人工评分均已完成，0 实质分歧；阈值仍为草案，140 条 holdout 继续封存。
- 首个 60 条候选随后完成双评且 0 分歧，但 dev 聚合因 high/blocker facts 仅 22/27 而失败；失败集中在两个检索证据完整命中的 false refusal。根因是模型过度拆题而非检索缺失，prompt 修正已进入候选状态，须先做两题 GPU 复验，再全量重新生成和双评。原失败评分与报告保持不改，阈值和 holdout 仍未触碰。
- prompt-v2 已按停止条件先重放两个失败 case；两题均 answered、各 4 个 requirements、0 missing facts。随后全量重新生成 60 条 dev，得到 40 answered、20 abstained、0 error，P50/P95 为 2595.369/5071.686 ms；holdout 载入和运行仍为 0。该状态只证明生成完整，不代表质量通过。
- 新旧逐题比较有 58 条输出哈希变化，必须重新双评；仅 `PRD-DEV-043` 和 `PRD-DEV-047` 的输出哈希完全相同，可由每名复核者沿用自己上一轮的决定。两份新工作文件各保留 58 条 `pending-review` 和 2 条已迁移记录，双方仍须独立完成后再聚合。阈值继续保持草案，尚未形成 `selected-dev-generation`，140 条 holdout 禁止运行。
- 两名复核者随后完成 prompt-v2 的全部待评行；A/B 各 60 行、0 pending、0 holdout，冻结字段与输出绑定一致，实质判定和 notes 均为 0 分歧。最终合并评分已通过严格 schema 校验。
- prompt-v2 dev 报告为 `insufficient-confidence`、`formal=false`：strict 60/60、high/blocker facts 27/27、claim support 120/120、no-answer recall 20/20、false refusal 0/40，泄漏和严重错误均为 0。点估计全部达标，但 27 个高严重度 facts 和 20 个 unanswerable cases 的 Wilson 95% 下界不足；不得将该结果改写为 PASS，也不得为提高置信度重复运行同一输出。阈值和 140 条 holdout 仍未触碰。
- 质量负责人明确接受该限制并将 prompt-v2 选为非正式 dev generation 候选；选择证据完整绑定模型、检索、输出、评分、报告和代码文件哈希，仍声明 `formal=false`、`formal_claim=none`。入选管线随后提交并推送至 `9606f70f29cec406496f55c221a38cac7ed00eab`，派生选择证据绑定该 commit；`quality_thresholds.json` 已冻结并绑定 T-062/T-063 选择哈希。最终 metadata readiness 为 `ready-for-first-holdout`、0 blocker，holdout 仍为 0 次。
