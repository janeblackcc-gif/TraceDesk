# 冻结的v0.1运维开发集

## 本次交付

本目录保留v0.1时期的资料和问题，供不同检索与生成方案在相同输入上对照。它不是当前版本的操作手册；当前配置与启动行为见[部署文档](../../docs/deployment.md)，例如0.2已支持自动读取项目.env。

本目录包含3份根据当时代码和本机实测配置整理的运维资料，以及12道开发用问题（10可答、2不可答）。资料由AI辅助整理，描述v0.1时期的真实项目行为；问题同样由AI拟定，不属于真实用户题集，也不是独立测试集。

制备时的文件来源与哈希记录在provenance.json。冻结后保留这些记录；后续资料更新应建立新的数据集版本。

## 导入冻结题集

1. 在 http://127.0.0.1:8765 打开知识库页面。
2. 知识库名称填写 TraceDesk 本机运维，版本填写 2026-09-08。
3. 依次选择 documents 目录下的3个 Markdown 文件，点击导入并建立文本索引。每次提交前核对知识库名称和版本，防止表单回填旧范围；只导入这3份资料，不导入本说明、问题文件或 provenance.json。
4. 确认3份文档均显示可检索。检查页面当前操作范围为 TraceDesk 本机运维 / 2026-09-08；若不一致，先到问答工作台切换知识库和版本，再返回知识库页面。
5. 点击建立本地向量索引，等待完成提示。

保留原来的 Atlas 演示项目，两者位于不同知识库。

## 本轮验收

- 新知识库中恰好有这3份资料，状态均为 ready。
- 索引成功，向量维度为1024，模型标签为 qwen3-embedding:0.6b-q8_0。
- 文档数、分块数和索引结果有记录。preflight.json 是离线检查结果，不能代替实际索引成功记录。

完成后记录索引提示中的总分块数即可，不需要逐题截图。

离线检查可以从项目根目录运行 ./.venv/Scripts/python.exe scripts/check_ops_dataset.py。它检查冻结文件的哈希、解析、分块和答案原文，不访问Ollama，也不更新provenance或在线知识库。

## 批量评测

从项目根目录使用 PowerShell 7 运行：

```powershell
./.venv/Scripts/python.exe scripts/run_baseline.py --dataset datasets/tracedesk_ops
```

脚本从项目配置指定的数据库只读复制本题集对应的3份资料、17个分块及当前模型向量到独立评测库，使用同一配置中的Ollama地址。先启动本机服务，并保持本轮模型标签和digest不变；无需重新嵌入全部文档。可通过--source-db、--ollama-url指定其他本机实例，--output指定新的输出目录，已有目录会被拒绝覆盖。

scripts/run_baseline.py 是执行入口；app/batch_evaluation.py 负责数据哈希、范围、向量快照和评分。三种方法复用 Service.ask，使用同一题集、生成模型和提示。每种方法预热一次，然后按题号轮换执行 BM25、dense、hybrid，每题使用独立会话。本轮不改动检索参数。

默认运行预算为900秒，在请求之间检查；单次HTTP请求超时120秒。预计本机约3至5分钟。输出默认位于 evidence/baselines/tracedesk_ops_时间戳，过程保存在 events.jsonl 和 status.json，中断或失败保留已完成的逐题记录。退出码0表示执行完成，2表示有逐题执行错误，1表示准备或整轮检查失败；完成不代表所有答案正确。

- results.jsonl：完整问题、返回片段、结论、引用、自动指标和原始Ollama调用。
- results.csv、summary.json、report.md：可筛选结果、汇总指标和口径说明。
- semantic_review.json：待单独复核的语义标注；自动 answered 状态和逐字引用通过率不等于准确率。
- manifest.json、snapshot、evaluation.db、warmup.json：模型与代码哈希、输入快照、评测库和不计分的预热记录。

前后结果与口径见[评测说明](../../docs/evaluation.md)。app/evaluation.py仍负责原有Atlas演示库回归。

之后逐步加入独立收集的问题和冻结的测试集，再依据失败类型选择改进。这里的12道题只负责先建立开发流程。
