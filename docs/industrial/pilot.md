# 真实用户试用证据

T-081/FA-21 必须来自真实用户和获得 pilot 授权的真实资料。模拟用户、自动化任务和仅授权 `evaluation` 的资料都不能关闭该门禁。

试用开始前先冻结：任务成功率下限、人工核验时长上限、每位用户最少任务数和高严重度错误上限（固定为 0）。`pilot_manifest.json` 保存冻结时间、阈值文件 SHA-256、资料授权引用和责任人签字；`participants.private.jsonl` 使用匿名 participant ID 与本地同意记录引用；`tasks.private.jsonl` 只保存任务 ID、结果、核验秒数和严重错误类别，不保存问题、回答或文档正文。

```powershell
./.venv/Scripts/python.exe scripts/pilot_report.py <private-pilot-run> --report <new-private-report.json>
```

校验器会检查：阈值早于试用开始、真实用户标记、任务时间窗口、用户映射、文件哈希、每人任务数、成功率、人工核验时长、高严重度错误和责任人签字。报告只输出聚合数值，可作为 acceptance 的 `kind=pilot` 证据；没有真实执行时不得制造 fixture 报告来宣称完成。

当前 10 份资料的授权范围是 `evaluation`。如要用于真实用户试用，资料所有者需要新增明确的 `pilot` 授权记录；在此之前 T-081 维持阻塞。
