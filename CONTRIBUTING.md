# 开发与验证

使用Python 3.13创建虚拟环境，在项目根目录安装并运行：

```powershell
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe scripts/start.py --check
./.venv/Scripts/python.exe scripts/release_check.py
```

单元测试使用临时数据库和模拟模型，不要求下载权重。真实模型评测是独立步骤，见[评测说明](docs/evaluation.md)。修改模型协议、提示或检索参数时，需要保留前后题集与模型标识，记录实际结果和仍失败的情况。

Git忽略配置、数据库、模型与原始实验输出。提交前运行release_check.py检查实际文件集合及本地文档链接；该检查不代替代码审查或独立安全审计。修改公开文件后需要重新构建发布包。

GitHub Actions测试Windows和Ubuntu上的Python 3.11/3.13，使用只读仓库权限，模型权重不在CI中下载。[checkout](https://github.com/actions/checkout)和[setup-python](https://github.com/actions/setup-python)固定到已核查的v7提交。

## 浏览器验收

可选验收依赖为Playwright 1.62.0和已安装的Microsoft Edge。先在独立的数据目录中启动空知识库实例，再从另一个终端运行。脚本使用真实HTTP和页面操作，会载入演示资料并创建、替换和删除自己的虚构文档；拒绝非空知识库，输出目录必须不存在。

```powershell
./.venv/Scripts/python.exe -m pip install playwright==1.62.0
$env:TRACEDESK_DATA_DIR = './data/browser-check-01'
./.venv/Scripts/python.exe scripts/start.py --port 8766 --require-models
```

另一个终端执行：

```powershell
./.venv/Scripts/python.exe scripts/browser_smoke.py --url http://127.0.0.1:8766 --output evidence/browser-check-01
```

单步超时120秒，阶段进度、API响应、截图及最终report.json写入指定目录。模型速度影响总时长，通常需要数分钟。只有退出码0且报告status=passed才能算本轮通过；移动视口不等于真实移动设备测试。当前候选包的实际执行状态见[评测说明](docs/evaluation.md)。

release_check.py基于Git文件列表检查待发布内容，需要在Git工作区运行；从ZIP解压后可直接安装、运行应用和测试。ZIP中的RELEASE_MANIFEST.json记录打包前逐文件哈希。

提交问题时提供最小复现、运行版本和脱敏日志；不要上传真实业务文档、访问令牌或本地数据库。本项目起始实现包含AI辅助生成代码，后续改动应保留实际测试和来源记录。
