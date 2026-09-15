# 容量与混合负载证据

正式容量结论只能来自目标 Linux/GPU 环境。`scripts/capacity_report.py` 不制造负载；它校验由实际负载驱动器和宿主采样器留下的 `capacity_manifest.json`、`environment.json` 与 `samples.jsonl`，并生成不含凭据和业务正文的汇总。

正式模式要求：

- 运行至少 30 分钟，50,000 active chunk、20 个注册用户、5 个并发 query；
- 覆盖稳态查询、解析+查询、重建索引+查询、模型重启、worker 崩溃、数据库重启、队列满、磁盘压力、陈旧写回竞争和稳态资源观察；
- 每类稳态时延至少 50 个成功样本，资源样本至少 20 个，运行开始和结束均有采样；
- 非模型 API P95 不超过 500 ms、evidence P95 不超过 2 s、queue P95 不超过 2 s、RAG total P95 不超过 30 s；
- query queue 不超过 20，queue-full 必须观察到明确拒绝；模型/数据库故障后必须恢复成功；
- OOM、数据损坏、权限/版本越界均为 0，RAM/VRAM 增长不超过事先冻结的上限。

每行 sample 记录 UTC `timestamp`、`scenario`、`operation`、`ok`，时延操作提供 `latency_ms`，失败提供稳定 `error_code`，资源采样提供 `rss_bytes`，可选 `vram_bytes` 和 `query_queue_depth`。manifest 对环境与样本文件固定 SHA-256，并保存事先批准的稳态错误率和内存增长上限。

```powershell
./.venv/Scripts/python.exe scripts/capacity_report.py <target-run-dir> --formal --report artifacts/capacity-<run-id>.json
```

本机合成测试只验证统计、边界和 FAIL 判定，不能作为 T-075 或 FA-18 的容量证据。

## 短期 portfolio smoke

为预算受限的一次性 Linux/GPU 部署，另设独立的 `scope=portfolio-smoke` 轨道，详见
[短期部署计划](plan-portfolio-smoke.md)。该轨道使用自己的 `portfolio_manifest.json` 和
`portfolio_report.py`，可以记录真实部署的启动、时延、资源和恢复数据，但不会进入
`target-release` 验收，也不会改变本页的正式容量门禁。dry-run 报告固定为 `fixture`，不得当作运行证据。
P0 的真实入口仅返回 `health-only`；只有后续认证工作流和宿主采样完成后，才可记录运行时 RAG/资源指标。
