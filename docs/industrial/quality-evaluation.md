# 真实质量评测执行协议

本协议覆盖 T-062 至 T-064。所有问题、参考答案、证据原文、模型回答和逐题评分只保存在 `eval/real/private/`；公共代码和汇总不得包含这些正文。

## 顺序和防泄漏边界

1. T-062 只读取物理隔离的 dev view。先完成 BM25，再用同一冻结 vLLM embedding 完成 dense/hybrid 单变量对照；`select_retrieval_candidate.py` 在所有规定 arm 齐全前固定返回 `blocked`。
2. T-063 固定 T-062 选中的检索配置，只在 dev 上生成回答。两名复核者独立填写评分，分歧先裁决，再由 `quality_report.py --split dev` 聚合。不得根据 holdout 修改检索、prompt、模型或阈值。
3. T-064 在质量负责人审核 dev 结果后创建全新的 `quality_thresholds.json`，填写 T-062/T-063 选择证据的 SHA-256，并将状态从 `draft-not-frozen` 改为 `frozen`。冻结时间必须早于首次 holdout 运行。
4. `quality_readiness.py` 仅检查 metadata，不执行或评分 holdout。只有全部 blocker 清零后才允许一次首轮 holdout；首轮结果无论 PASS、FAIL 或置信度不足都必须保留。

## T-062 选择规则

候选首先比较 evidence-group Recall@4，其次比较上下文完整率、上下文精度、P95 和实现复杂度。相对 BM25 的召回提升不足 3 个百分点且没有修复 high/blocker 漏检时，不引入更复杂方案。单轮检索计时只用于同机开发比较，不是容量结论。

## T-063 双人评分字段

`prepare_generation_review.py` 生成不含问题和答案正文的 dev 模板。模型原始回答另存私有文件，模板只记录其 SHA-256 和带时区的生成时间；后者用于证明 holdout 输出晚于阈值冻结。每题必须完成：

- `strict_task_pass`：任务要求、答案范围、格式和拒答行为是否整体正确；
- `required_facts_total/satisfied`：answerable 题的必需事实覆盖；
- `factual_claims_total/supported`：可核验事实主张及其证据支持；
- `correct_refusal` 或 `false_refusal`：按 answerability 二选一；
- `citation_relevant`、scope/version leakage、严重错误和失败类别；
- 两个不同 reviewer ID；有分歧时填写 `resolved` 和 adjudicator ID。

完成行使用 `record_status=completed-review`。未评分的 `null` 模板不能通过正式 schema，也不能生成 PASS。

当前 v2 prompt-v2 工作包位于 `dev-only-v2-20260916-01/experiments/t063-generation-v2-prompt-v2-20260916-01/`。A/B 个人文件保留单个 reviewer ID 和 `record_status=reviewed`；完成对比后生成的最终合并文件填写两个 reviewer ID、`dispute_status`，并改为 `completed-review`。本轮两份评分的实质字段和 notes 均一致，无需人工裁决。

## T-064 默认阈值草案

- scope leakage、version leakage、严重错误：均为 0；
- strict task pass：至少 85%；
- high/blocker 必需事实完整度：至少 95%；
- claim support：至少 95%；
- no-answer recall：至少 90%；
- false refusal：不超过 10%。

汇总同时报告分子、分母和双侧 Wilson 95% 区间。点估计达标但区间越过阈值时只能记为 `insufficient-confidence`，不能记为 PASS。旧 v1 的 12 条 holdout 因问题文本提前暴露已经降级为 dev；替代的 v2 虽完成 140 条唯一一次运行，但事后确认占位内容和不可解析金标使基准无效，同样不得再支持正式质量结论。v3 的样本量和分母须按 [v3 独立质量基准准备协议](plan-quality-sample-size-v3.md) 重新预注册。claim 分母只能在首次模型输出后观察，因此 readiness 将其记录为运行后置信度检查而不是运行前 blocker；若实际 claim 分母不足，只能保留首轮结果并报告 `insufficient-confidence`，不得补题或重跑。

## 数据集 CPU 门禁

在物化 dev view 或租用 GPU 前，先用新的报告路径运行 `check_eval_dataset.py --formal`。正式数据集必须显式记录 `authoring_status=final` 和 `semantic_review_status=final`；默认的 `draft/pending` 仅允许非正式准备。校验还会阻断占位问题和标签、标准化重复问题、holdout 模板组占比超过 20%、跨 split 分组泄漏，以及不能落入当前 parser/chunker 实际 chunk 的金标引文。机械校验通过不替代两名 reviewer 的真实语义复核。

## 命令

```powershell
./.venv/Scripts/python.exe scripts/check_eval_dataset.py <private-dataset> --formal --report <new-dataset-report.json>
./.venv/Scripts/python.exe scripts/select_retrieval_candidate.py <t062-run> --output <t062-run>/candidate-selection.json
./.venv/Scripts/python.exe scripts/run_generation_eval.py --dev-view <dev-view> --retrieval-run <t062-run> --output <dev-view>/experiments/<t063-run>
./.venv/Scripts/python.exe scripts/prepare_generation_review.py --dev-view <dev-view> --output <dev-view>/reviews/t063-generation-review.template.jsonl
./.venv/Scripts/python.exe scripts/quality_readiness.py --dataset <private-dataset> --thresholds <private-dataset>/quality_thresholds.draft.json --write-draft
./.venv/Scripts/python.exe scripts/quality_readiness.py --dataset <private-dataset> --thresholds <thresholds> --retrieval-selection <selection> --output <private-dataset>/quality-readiness/<run>.json
./.venv/Scripts/python.exe scripts/quality_report.py --dataset <private-dataset> --reviews <completed-private-jsonl> --thresholds <thresholds> --split dev --report <private-report.json>
```

以上工具不调用模型、不制造评分、不自动冻结阈值，也不修改 holdout seal。
