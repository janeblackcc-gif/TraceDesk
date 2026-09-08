# FastAPI 中文文档新挑战集 v1

本数据集使用 FastAPI 官方仓库固定提交 `50113da16fec53b66b80d75e80a89296de4fa5a5` 的六篇中文资料。题目在首次运行模型之前由 AI 拟定并核对原文，独立于此前 TraceDesk 运维开发题；不是独立人工标注、真实用户问题，也不能证明官方文档未进入模型预训练。

上游原文、URL、获取日期与 SHA256 记录在 `sources.json`，MIT 许可证保存在 `UPSTREAM_LICENSE`。文档仅将扩展名由 `.md` 改为 `.txt`，字节不变，仍通过应用现有 Markdown/TXT 解析器。上游相对链接、图片与 `docs_src` 包含指令没有展开，不得用未导入内容补答案。资料描述的是固定提交中的行为，不保证适用于其他 FastAPI 版本。

## 运行前固定的协议

- 六篇语料：部署概念、Docker 部署、环境变量设置、后台任务、Uvicorn workers、错误处理。24 题中 18 题可答、6 题相关但不可答；每篇三道可答题、一道人为构造的拒答题。
- 题型覆盖精确事实、条件与例外、多段必要信息。正文长度与切块数由预检查记录；不声称是超长上下文或大规模检索压测。
- `questions.test.jsonl` 记录问题、预期答案、必要原文和等价出处；`rubric.json` 记录逐题必要事实及语义复核口径。`provenance.json` 固定文档、问题、评分清单和本协议的哈希。首次运行后不修改本版本的题目、语料、标注或评分标准。
- 使用 RC1 的应用、模型、提示词、切块与阈值。BM25、dense、hybrid 复用同一个 `Service.ask`。先每种方法预热一题，再按题序轮换三种方法顺序；正式每题每方法一次，共 72 条。固定温度 0、seed 42、上下文 8192、输出上限 768、关闭 thinking。
- 在单独的评测数据库导入这六篇文档并创建索引，再由评测程序只读快照。禁止混入原运维或演示资料。记录模型 digest、Ollama 版本、代码哈希、原始请求与输出、逐题事件和完成状态。
- 建索引预计 1 至 3 分钟，72 条推理预计 5 至 12 分钟。整轮预算 1800 秒，单个模型请求最长 120 秒；每题开始和结束写事件，保留中断、错误和部分结果。

## 评分口径

- 检索：Hit@4、Recall@5、MRR@5，并以 `gold_coverage_at_4` 和全部必要证据覆盖数衡量多事实题。每条 gold 是一个必要证据组，`gold_alternatives` 对应索引中任一出处均可命中；不同必要组仍须全部满足。
- Recall@5 的分母是所有已标注相关块，等价重复出处会增加分母；它不等于必要事实覆盖率，也不宣称穷尽语料全部可能措辞。
- 自动行为：实际 Ollama 回答、拒答、误拒答、错误、显式降级、逐字引用。逐字引用正确只证明引文来自检索片段。
- 逐条语义复核：`correct` 要求直接回应且没有错误事实，允许正确但不完整；`complete` 要求覆盖全部必要事实并保留条件；`all_claims_supported` 要求每项结论由它自己的引用支持。
- 严格通过：可答题须实际返回模型答案，并同时正确、完整、逐项引用支持；不可答题须 `no_evidence` 且没有 claims。另报遗漏事实和无支持断言，不能用返回了答案替代准确率。
- 所有分母保留错误、拒答和降级；72 条结果全部复核，复核者为 AI 代理。单次计时只描述本机此轮，不能当作稳定服务承诺。

本轮首先测量当前 RC1 的外部表现。若后续根据结果修复，必须保留原始基线；该题集自此属于回归资料，修复后的结果不能重新称为独立测试。

## 复现

在本机模型配置完成后，使用独立导入脚本创建新数据库和向量索引，再启动评测。下面两个输出目录都必须尚不存在：

```text
python scripts/import_eval_dataset.py --dataset datasets/fastapi_zh_challenge_v1 --output evidence/fastapi_challenge_index
python scripts/run_baseline.py --dataset datasets/fastapi_zh_challenge_v1 --source-db evidence/fastapi_challenge_index/source.db --output evidence/fastapi_challenge_run --max-seconds 1800
```

评测输出中的 `manifest.json`、`status.json`、`results.jsonl` 与语义复核文件共同构成证据，自动汇总不包含人工准确率。
