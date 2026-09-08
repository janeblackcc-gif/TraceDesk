# 安装与部署

当前目标为本地单人部署，网页固定监听127.0.0.1，端口可配置。推荐Python 3.13；代码入口要求Python 3.11及以上。真实模型实测使用Windows、RTX 4060 Ti 8GB、Ollama 0.33.3，其他硬件的速度和可用上下文需要自行测量。

## Windows / PowerShell 7

在项目根目录执行：

```powershell
py -3.13 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe scripts/start.py --check
./.venv/Scripts/python.exe scripts/start.py
```

打开 http://127.0.0.1:8765。未安装模型时可以载入演示资料，使用证据模式检查检索与引用。

## 本地RAG

安装并启动[Ollama](https://ollama.com/download)。Qwen3.5结构化输出要求Ollama至少0.31.2，本项目实际测试版本为0.33.3；旧版本的thinking关闭与JSON约束兼容问题见[官方修复说明](https://github.com/ollama/ollama/releases/tag/v0.31.2)。

```powershell
ollama pull qwen3-embedding:0.6b-q8_0
ollama pull qwen3.5:4b-q4_K_M
./.venv/Scripts/python.exe scripts/start.py --check --require-models
pwsh -NoProfile -File ./start_local_rag.ps1
```

模型下载需要网络、磁盘空间和等待时间，应用不会代替用户下载模型。默认Ollama地址为http://127.0.0.1:11434。需要手动启动服务时，在另一个终端运行ollama serve。导入资料后，在知识库页面建立本地向量索引，再使用本地RAG模式。

## 配置

项目根目录可以放置.env，内容格式参考[.env.example](../.env.example)。现有环境变量优先于.env，随后才使用默认值；不会搜索父目录中的.env，也不会把文件内容写入全局环境。配置按字面值读取，不展开${...}。

| 配置项 | 默认值 | 含义 |
|---|---|---|
| TRACEDESK_DATA_DIR | ./data | 相对路径从项目根目录解析 |
| TRACEDESK_PORT | 8765 | 网页端口，范围1至65535 |
| TRACEDESK_OLLAMA_URL | http://127.0.0.1:11434 | 本机HTTP模型服务 |
| TRACEDESK_EMBED_MODEL | qwen3-embedding:0.6b-q8_0 | 索引模型，改变digest后需要重建 |
| TRACEDESK_CHAT_MODEL | qwen3.5:4b-q4_K_M | 生成模型 |

也可对单次启动覆盖网页端口：

```powershell
./.venv/Scripts/python.exe scripts/start.py --port 8766
```

start_local_rag.ps1默认连接已经运行的模型服务，并在当前终端运行网页服务，Ctrl+C停止网页。保留-RuntimeRoot可选参数，用于已有ollama-0.33.3/ollama.exe与models子目录的独立运行时；该模式使用11435，并把模型服务日志写入data/local_runtime。-Check只执行检查，不启动服务。

## Linux

在已安装Python 3.11+的环境中：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python scripts/start.py --check
./.venv/bin/python scripts/start.py
```

依赖和测试配置包含Ubuntu与Windows，远程CI结果以仓库实际运行记录为准。当前不承诺macOS浏览器或其他GPU的实测结果。

## 常见状态

| 情况 | 处理 |
|---|---|
| --check提示端口不可用 | 可能已有实例；打开现有页面，或使用--port指定空闲端口 |
| 模型服务在线但ready=false | 核对服务地址和两个完整模型标签 |
| 索引缺失或digest改变 | 在当前知识库和版本重新建立索引 |
| HTTP 409 | 当前操作忙碌或索引需要建立，查看返回信息 |
| HTTP 503 | 检查模型连接、输出长度和JSON兼容性 |
| 原文摘录降级 | 引用校验未通过；查看警告与原文，不能当作模型回答 |

## 数据与更新

数据库包含导入的原文、向量及有限追踪，保存在配置的数据目录中，默认不进入Git。备份或升级前先停止网页服务，再备份整个数据目录，包含可能存在的SQLite WAL文件。恢复备份后核对知识库与模型digest；新模型需要重新建立相应索引。

本版本只面向单人本机访问，不提供账号、租户隔离或公网请求限额。公网演示需要单独配置访问、上传和算力使用边界。

## 配置实现来源

.env解析使用[python-dotenv的dotenv_values](https://pypi.org/project/python-dotenv/)，应用自行确定文件位置、配置优先级和允许值。配置检查不会上传本地资料。
