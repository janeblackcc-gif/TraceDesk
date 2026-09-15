# RC2 工业化实施基线（T-001）

源码基线：`ccc00c2f5e369bb50a7e459fda0404de1e74b4f3`，版本 `0.2.0rc2`。
2026-09-09 在现有 Windows/Python 虚拟环境重跑 `python -m pytest -q`：
**179 passed, 1 warning in 6.27s**。警告来自 Starlette 对 AnyIO 已弃用别名的引用。
日志和退出码位于 `artifacts/M0/T-001/`；不把第三方警告算作测试失败。

## 历史质量证据

以下均标记为 `legacy_dev`，本轮没有重跑模型或重新人工评分：

| 数据 | 历史严格通过 | 证据性质 |
|---|---|---|
| 论文首轮 | 0/8 | 原人工评分 |
| 同论文 RC2 | 1/8 | AI 开发复核 |
| FastAPI RC2 | 19/24 | AI 开发复核；上一轮 22/24 |
| 历史运维 | 8/12；直接题意 12/12 | 两种口径同时保留 |
| 合成公式 | 4/5 | 合成开发诊断 |

详情见 [RC2报告](../rc2-evaluation.md)。公开模型标签、digest、数据与结果
在 `eval/baselines/*/manifest.json`；私人论文及数据库不进入公开基线。
执行 `python scripts/freeze_baseline.py` 记录 Git 基线文件、公开评测输入、
模型 manifest 和实施规范的 SHA-256，输出 `artifacts/baseline/manifest.json`。
冻结脚本拒绝覆盖既有 manifest；`--verify` 复核 Git 对象与规范，不要求后续源码保持不变。

## 证据约定

任务首次证据放 `artifacts/<milestone>/<task-id>/`，后续运行使用新的 run 子目录。
记录命令、UTC 时间、退出码和原始日志；失败记录保留。完成状态以
[实施状态](status.md) 为准。测试通过、编码完成、工程验收、质量达标、真实试用分别记录。
