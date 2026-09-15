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

## T-064 默认阈值草案

- scope leakage、version leakage、严重错误：均为 0；
- strict task pass：至少 85%；
- high/blocker 必需事实完整度：至少 95%；
- claim support：至少 95%；
- no-answer recall：至少 90%；
- false refusal：不超过 10%。

汇总同时报告分子、分母和双侧 Wilson 95% 区间。点估计达标但区间越过阈值时只能记为 `insufficient-confidence`，不能记为 PASS。当前 12 题 holdout 是有效封存集，但不足以让 strict task pass、no-answer recall 和 false refusal 在该置信策略下取得正式 PASS；如坚持正式置信放行，需要在不查看当前 holdout 结果的前提下建立更大的新数据集版本并重新封存。

## 命令

```powershell
./.venv/Scripts/python.exe scripts/select_retrieval_candidate.py <t062-run> --output <t062-run>/candidate-selection.json
./.venv/Scripts/python.exe scripts/prepare_generation_review.py --dev-view <dev-view> --output <dev-view>/reviews/t063-generation-review.template.jsonl
./.venv/Scripts/python.exe scripts/quality_readiness.py --dataset <private-dataset> --thresholds <private-dataset>/quality_thresholds.draft.json --write-draft
./.venv/Scripts/python.exe scripts/quality_readiness.py --dataset <private-dataset> --thresholds <thresholds> --retrieval-selection <selection> --output <private-dataset>/quality-readiness/<run>.json
./.venv/Scripts/python.exe scripts/quality_report.py --dataset <private-dataset> --reviews <completed-private-jsonl> --thresholds <thresholds> --split dev --report <private-report.json>
```

以上工具不调用模型、不制造评分、不自动冻结阈值，也不修改 holdout seal。
