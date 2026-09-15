# 真实评测资料入口

此目录只保留公开流程说明。真实文档、题目、gold、授权记录和评审者映射默认放在受限目录，不提交 Git，也不进入发布包。

一个 v2 数据集目录包含：

```text
dataset_manifest.json
corpus_manifest.json
cases.jsonl
labels.private.jsonl
documents/<authorized files>
```

`corpus_manifest.json` 为每份文档记录 SHA-256、字节数、数据 owner、授权引用、允许用途和授权到期时间。`cases.jsonl` 只保存问题、dev/holdout、任务类型、严重度和 source/template 分组。`labels.private.jsonl` 保存 required facts、证据组、禁止 claim、匿名 reviewer ID 和争议裁决；普通应用 API 不提供该文件。

公开结构见 `eval/schema_v2.json`。正式冻结前运行：

```powershell
./.venv/Scripts/python.exe scripts/check_eval_dataset.py <restricted-dataset-dir> --formal --report artifacts/eval-preflight-<run-id>.json
```

正式模式要求至少 40 题、dev/holdout 都存在、holdout 标记为 sealed、source/template 分组不跨 split、每题至少两名不同复核者，且没有未解决争议。报告只包含数量和哈希，不复制问题、gold 或文档内容。通过预检只表示输入可审计，不表示模型质量通过；阈值需在首次 holdout 运行前另行冻结。
