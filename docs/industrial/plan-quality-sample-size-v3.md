# v3 独立质量基准准备协议

状态：CPU 数据门禁已实现，等待新的、已授权且未暴露的候选资料。v3 的样本量、文档数、source/template group 配额尚未预注册；在这些值冻结前不得开始正式 holdout 编题，更不得租用 GPU 或创建正式运行 claim。

## 目标与边界

v3 用于重新建立独立的模型质量估计。v2 已消耗且基准无效，只能作为开发缺陷回归材料，不得修补后重跑、复制题目或模板，也不得把原 `formal=true/failed` 报告改写为有效的模型质量结论。

v3 必须同时满足：

- holdout 的原始资料、source groups 和 template groups 均未用于 v1/v2、prompt 示例、开发调参或工程代理上下文；
- 每份资料有逐文档授权、owner、用途、有效期、不可变版本和 SHA-256；
- author、两名独立 reviewer 和 adjudicator 的职责及可见范围事先冻结；
- 数据通过当前 parser/chunker 的 CPU 门禁后才可封存；
- 封存后只向开发流程物化 dev view，holdout 不进入开发索引或 GPU；
- 检索、prompt、模型、代码、阈值和运行环境冻结后，只允许一次正式 holdout 运行。

## 预注册输入

数据负责人在获取或阅读 holdout 正文前，先冻结以下内容并由质量负责人签字：

1. 目标指标、Wilson 95% 置信区间、每项最低分母和可接受错误数；
2. dev/holdout 的 case 数、answerable/unanswerable 数及 high/blocker required facts 数；
3. 候选文档数、reserve 数、单一 source group 上限和逐文档 case 上限；
4. task type 与 unanswerable 类别配额；
5. template group 列表及改写规则，任何单一 holdout template group 不得超过 20%；
6. author、reviewer A、reviewer B、adjudicator 和质量负责人；
7. 暴露登记规则，以及发生任何提前暴露时将相应资料整体降为 dev 的处置方式。

不得默认复用 v2 的 200 case、60/140 split 或 18 份 holdout 文档设计。样本量应由本轮指标分母和可获得的新资料共同决定；不足时停止，而不是降低独立性要求。

## 新资料准入清单

每份候选资料必须逐项确认：

- [ ] 来源合法、授权明确包含 `evaluation`，且有效期覆盖预计评测窗口；
- [ ] 文件来自固定版本或 commit，记录获取时间、上游引用、字节数和 SHA-256；
- [ ] 文件为 UTF-8 Markdown/TXT 或文本型 PDF，未加密、无需 OCR，能通过当前 `parse()`；
- [ ] source group 与版本族已定义，未跨 dev/holdout；
- [ ] 未用于 v1/v2、公开示例、开发 prompt、人工训练材料或既有调参；
- [ ] 分配给 holdout 后，正文不会提供给开发代理、模型选择人员或 GPU 操作者；
- [ ] 内容足以产生真实业务问题、边界和例外，不靠文件名或模板占位凑数；
- [ ] 若资料资格、暴露状态或授权链任一项不确定，则进入 reserve 或拒绝，不进入 holdout。

## 编题、复核与裁决

1. author 仅依据被分配资料编写真实语义问题、required facts、forbidden claims 和逐字 evidence passages；禁止 `Generated ... question for ...`、`TODO`、`TBD`、待填写、示例答案等占位内容。
2. 每个 answerable required fact 对应一个非空 evidence group；每个 passage 的原文、文档 SHA-256 和 quote SHA-256 必须精确绑定。
3. reviewer A/B 在彼此不可见的情况下独立核对 answerability、task type、severity、事实、证据、禁止声明和模板归属，不得只做结构或哈希确认。
4. 任一语义分歧进入 adjudication；未解决分歧不得标记 final。裁决者不能替代缺失的第二名 reviewer。
5. 全部内容完成后才把 `authoring_status` 设为 `final`，全部独立语义复核和裁决完成后才把 `semantic_review_status` 设为 `final`。
6. 设置 `sealed_holdout=true` 前重新生成 cases、labels 和 corpus manifest 哈希；状态字段、内容哈希和 seal 一起进入最终 `dataset_hash`。

## CPU 门禁

所有步骤在本地 CPU 上完成，不启动模型服务，不连接远程 GPU。正式检查使用新的报告路径：

```powershell
$ErrorActionPreference = 'Stop'
./.venv/Scripts/python.exe scripts/check_eval_dataset.py <dataset-dir> --formal --report <new-report-path>
if ($LASTEXITCODE -ne 0) { throw 'v3 dataset validation failed' }
```

当前 `--formal` 必须阻断：

- 组件哈希、授权、文件类型、路径隔离或 corpus 字节不一致；
- 非 final authoring/semantic review、少于两名 reviewer 或未解决分歧；
- dev/holdout 的 source 或 template group 交叉；
- 已知占位问题或标签文本、标准化后重复问题；
- 单一 holdout template group 超过 20%；
- answerable case 的事实/证据数量不一致，或金标引文无法落入当前 `parse()` + `chunks()` 产生的实际 chunk；
- 少于 40 个 case、缺少任一 split，或 holdout 未封存。

`passed` 只证明数据结构和本协议的可机械检查项通过，不代表语义一定正确，也不代表模型质量 PASS。质量负责人仍须检查预注册配额、资料独立性、双人语义复核记录和暴露登记。

## 封存后的执行顺序

1. 保存 formal CPU 报告、schema 版本、代码 commit、parser/chunker 版本和数据集哈希；保持私有内容不进入公共日志。
2. 只物化 dev view，用它完成检索、prompt 和模型选择；所有失败及选择理由留证。
3. 冻结 retrieval、prompt、generation model/revision、依赖、代码 commit 和质量阈值；核对 dev 选择证据与冻结哈希。
4. readiness 仅核查 metadata、seal、阈值时间顺序、首次运行计数和哈希，不加载 holdout 内容到开发流程。
5. 在已核验的目标环境创建原子 first-run claim，执行唯一一次 holdout 生成并立即只读封存输出。
6. 对模型输出进行两名独立评分和必要裁决，然后聚合正式报告；无论 PASS、FAIL 或 `insufficient-confidence` 均永久保留。

若 seal 后发生数据修订、内容暴露、阈值回填、运行中断后需要重做，当前 holdout 立即失去独立正式资格。不得覆盖原 claim 或输出，须使用新的 source/template groups 重新建下一版。

## GPU 租用前放行记录

只有下列项目全部有证据时，质量负责人才能批准 GPU：

- [ ] 预注册样本量、配额和停止条件已签字；
- [ ] 新资料授权、来源、哈希和未暴露状态全部核验；
- [ ] authoring 与双人语义复核均为 final，0 未解决分歧；
- [ ] `check_eval_dataset.py --formal` 返回 `passed`；
- [ ] dataset/schema/parser/chunker/代码哈希已记录；
- [ ] dev-only 选择完成，holdout 加载和运行计数仍为 0；
- [ ] 模型、检索、prompt、阈值、依赖和目标环境已冻结；
- [ ] 唯一运行窗口、预算、操作者、输出目录和失败处置已确认。

任一项未完成时保持 FA-19 blocked，不租 GPU，不创建正式 claim。
