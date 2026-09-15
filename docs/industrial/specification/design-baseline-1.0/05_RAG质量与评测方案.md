# TraceDesk RAG 质量与评测方案

## 1. 当前证据如何解释

当前证据不是“RAG 已好”：
- 179 项 pytest：工程与契约回归；
- 38 项浏览器检查：真实 HTTP/Edge 的已记录主流程；
- 当前本机模型组合：真实运行过，说明链路可用；
- RC2 FastAPI 人工集 19/24；论文 8 题开发复核 1/8；都不是独立 holdout；
- 逐字 quote 可定位只证明引用字符串来自候选，不证明 claim 语义被支持。

因此质量工作必须按链路分层，不允许“加一个更强模型”掩盖解析/召回/上下文问题。

## 2. 失败定位框架

每道失败只允许先标一个主因、可加次因：

1. **PARSING**：答案信息未正确抽取；扫描、表格、公式、页序、编码问题。
2. **CHUNKING**：信息被切断、限定条件与结论分离、块过大/过小。
3. **RETRIEVAL**：gold chunk 存在但 Top-K 未召回。
4. **RERANK/SELECTION**：Top-K 有 gold，但进入 generation context 的块不够或顺序差。
5. **CONTEXT**：必要多块证据未同时进入；邻接扩展带来噪声。
6. **GENERATION**：证据充分但模型遗漏、算错、扩写或误拒答。
7. **CITATION_SELECTION**：正文正确但选错/混入无关引用。
8. **DISPLAY/PRODUCT**：后端正确但 UI/导出/范围提示使用户误解。
9. **SCOPE/AUTH**：版本或权限范围错误。
10. **GOLD/ANNOTATION**：金标不完整或题意有争议。

修复必须对应主因。一次实验只改变一个主要因素。

## 3. 改动优先级

### 3.1 先修解析/切块
对真实技术文档抽样 30 份，记录：文本 PDF、扫描页、表格、代码块、标题、错误码、配置表。若 ≥10% 关键任务因扫描/版面解析失败，才启动 OCR/Docling 类实验；否则保持 pypdf/文本路径，避免无收益增加依赖。

切块至少比较：
- RC2 heading-aware 850 chars 基线；
- heading + token budget（例如 350–500 tokens）；
- 结构块（代码/列表/表格不跨断）；
- parent-child 只作为候选实验。

评价不是 chunk 数，而是 gold evidence coverage、上下文噪声、索引成本。

### 3.2 再做召回
保留 BM25/当前 lexical 作为基线。默认目标检索：
- sparse：当前 tokenizer/bigram + PostgreSQL FTS 之一，先做同集比较；
- dense：当前 Qwen embedding 起点；
- hybrid：RRF；
- query translation/拆分是独立开关。

只有当 gold 已在候选池但排序差时才试 reranker。Cross-encoder 会增加延迟/模型内存，必须证明严格任务通过率或 context precision 提升超过成本。

### 3.3 上下文
分别记录：
- Hit@4：gold 是否进入前 4；
- Generation Context Coverage：所有 required evidence groups 是否进入实际生成上下文；
- Context Precision：进入上下文的块中有多少是支持/必要证据。
相邻块扩展不能算“召回命中”，只算 context expansion。

### 3.4 生成
当前 4B 是基线。只有在“证据充分但生成失败”子集占比高时比较更强模型。比较必须固定 retrieval/context。候选包括：
- 当前 4B；
- 更强本地模型（受硬件预算约束）；
- 若政策允许，云模型仅作为显式对照。
不能同时换 embedding、reranker、chunking、generation 后宣称某一组件提升。

### 3.5 支持性检查
逐字 quote 校验继续保留，作为结构安全网。额外 claim-support checker 只有在人工数据显示“引用真实但语义不支持”是主要错误时加入。评审模型先在人工双标子集校准：报告 precision/recall/一致性；未校准前不得作为唯一真值或自动放行器。

## 4. 真实数据与标注格式

### 4.1 数据来源
首批至少来自 1–2 个真实实验室/小研发项目，资料必须获得授权。目标 30–100 份持续更新的技术文档；若实际只有更少，不凑数，报告真实规模。

### 4.2 问题
最低建议 120 道：
- 60 可答：配置值/步骤/错误码/前置条件/多证据；
- 30 不可答但主题相近；
- 15 版本冲突/过期资料；
- 15 追问/上下文。
其中至少 40 道由两名人工交叉标注。样本量不是统计充分性的保证；选择 120 是为了让每个主要任务层有可观察分母，并比历史 8/24 题更不易被单题支配。正式阈值需同时给 Wilson/Bootstrap CI 或至少分子分母。

### 4.3 JSONL
```json
{
  "id":"LAB-001",
  "split":"dev",
  "task_type":"config_value",
  "kb_id":"...",
  "scope_version":"...",
  "question":"...",
  "answerability":"answerable",
  "required_facts":[
    {"id":"f1","text":"默认端口为8088","severity":"high"}
  ],
  "evidence_groups":[
    {"fact_id":"f1","alternatives":[{"document_revision_id":"...","passage":"..."}]}
  ],
  "forbidden_claims":["自动重试一次"],
  "notes":"..."
}
```
金标 passage 必须来自冻结 corpus；参考答案不是运行提示的一部分。

## 5. dev / holdout 与污染控制

- 按**文档来源/任务模板分组切分**，不能把同一段改写题分别放 dev/test。
- 建议 70% dev、30% holdout；holdout 在冻结后只允许正式运行，不能用于调阈值/提示。
- 历史 40/12/31/49/8/24 等开发题全部标 `legacy_dev`，不得改名成独立 test。
- 每次模型/提示/检索修改记录 commit、corpus hash、dataset hash、model digest、config hash。
- holdout 题目和 gold 不进入模型上下文、开发日志或 prompt。
- 正式失败不得删除；修复后可增加新 run，但原 run 保留。
- 发生数据泄漏/开发者提前查看 holdout gold 时，该 holdout 立即降级为 dev，并重新建立新的封存集。

## 6. 双标与争议

每个双标题由 reviewer A/B 独立判断：
- answerable；
- required facts；
- acceptable evidence；
- 严格答案 correctness/completeness；
- 每条 claim 是否 supported；
- severity。

先独立，再计算一致率（Cohen’s kappa 可用于分类项；required facts 用 exact/F1 + 争议数）。不一致进入 adjudication，由第三人或共同会议记录理由。最终 gold 保存原始 A/B 和 adjudicated 版本，不能只保留“最终答案”抹掉争议。

## 7. 指标定义

### 检索
- `Hit@4`：至少一个 gold evidence group 的任一可接受 chunk 出现在 Top4 的题比例。它不是 Recall@4。
- `Evidence Group Recall@K`：所有 required evidence groups 中被 TopK 覆盖的比例。
- `MRR@K / nDCG@K`：仅对有定义相关度的可答题。
- `Cross-scope leakage`：返回任何无权/错误版本 chunk 的请求比例，目标 0。

### 上下文
- `Context complete rate`：实际送给生成模型的上下文覆盖全部 required facts 的题比例。
- `Context precision`：上下文块中人工判定为必要/支持的比例。

### 生成
- `Strict task pass`：所有 high/required facts 正确且无高严重度 forbidden claim。
- `Completeness`：required facts 覆盖数/总数。
- `Claim support rate`：有事实性 claim 中被其引用充分支持的比例。
- `False refusal`：可答题返回 no_evidence/abstain。
- `No-answer recall`：不可答题正确拒答比例。
- `Citation exactness`：quote 是否逐字属于指定 chunk；这是结构指标，不等于语义支持。
- `Citation relevance`：引用是否真正支持该 claim，由人工/经校准 checker 判断。

所有指标必须显示分母；空分母为 null。

## 8. 消融矩阵

按阶段执行，不全排列爆炸：

A. RC2 sparse baseline  
B. + dense（当前 Qwen embedding）  
C. + query translation  
D. + multi-query  
E. + adjacency  
F. + reranker（仅若排序失败占主因）  
G. + chunking v2（在独立解析实验中）  
H. generation 4B vs stronger（固定最佳 retrieval）  
I. + support checker（仅若语义混引是主因）

每一阶段记录质量、P50/P95、GPU/CPU/内存、索引时间、上下文 token。若质量收益小于预先定义的最小有意义差异（建议 strict pass +3 个百分点或修复明确高严重度失败）且成本明显上升，则不保留。

## 9. 质量门禁

M6 前半先用 dev 选方案；随后冻结正式门槛。默认试点建议：
- scope/permission leakage = 0；
- strict task pass ≥85%；
- high-severity required fact completeness ≥95%；
- claim support ≥95%；
- no-answer recall ≥90%；
- false refusal ≤10%。

这些不是已达成结果，也不是行业统一标准。若 95% CI 太宽，报告“不足以确认达标”，不要只看点估计。

## 10. 失败回放

每次失败保存：
`question_id → corpus/index generation → retrieved ids/scores → generation context ids → model profile/digest → structured plan → claims/citations → human labels → failure category`。

不保存隐藏思维链。回放工具必须能在同 corpus/model 可用时复现检索；模型生成的非确定性要记录 seed/options，不能保证字节级一致。

## 11. OCR / reranker / 更强模型的触发规则

- OCR：关键任务中解析失败占比达到冻结阈值，且人工确认 OCR 能恢复答案证据。
- reranker：gold 已进入候选 Top20，但 Top4/context selection 经常失败。
- 更强 generation：context complete 但 generation failure 占主要错误。
- support checker：claim-support 错误高于引用结构错误，且 checker 在双标集达到批准 precision/recall。
- 更强 embedding：retrieval miss 是主要错误，且换 embedding 在同 corpus 上显著提升 evidence-group recall。

这套触发规则防止“所有组件全部叠加”的不可解释工程。
