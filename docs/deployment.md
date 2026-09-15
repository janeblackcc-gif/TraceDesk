# 安装与部署

本页同时覆盖Windows单人兼容路径与Linux团队试点。单人网页固定监听127.0.0.1，端口可配置；正式团队部署由TLS proxy对外提供HTTPS，模型端点仍只绑定受控loopback。推荐Python 3.13；代码入口要求Python 3.11及以上。历史真实模型实测使用Windows、RTX 4060 Ti 8GB、Ollama 0.33.3；vLLM的显存、并发和可用上下文必须在目标Linux/GPU主机实测。

## Windows / PowerShell 7

在项目根目录执行：

```powershell
py -3.13 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe scripts/start.py --check
./.venv/Scripts/python.exe scripts/start.py
```

打开 http://127.0.0.1:8765。未安装模型时可以载入演示资料，使用证据模式检查检索与引用。

## 历史 Windows / Ollama 本地RAG

安装并启动[Ollama](https://ollama.com/download)。Qwen3.5结构化输出要求Ollama至少0.31.2，本项目实际测试版本为0.33.3；旧版本的thinking关闭与JSON约束兼容问题见[官方修复说明](https://github.com/ollama/ollama/releases/tag/v0.31.2)。

```powershell
ollama pull qwen3-embedding:0.6b-q8_0
ollama pull qwen3.5:4b-q4_K_M
./.venv/Scripts/python.exe scripts/start.py --check --require-models
pwsh -NoProfile -File ./start_local_rag.ps1
```

模型下载需要网络、磁盘空间和等待时间，应用不会代替用户下载模型。默认Ollama地址为http://127.0.0.1:11434。需要手动启动服务时，在另一个终端运行ollama serve。导入资料后，在知识库页面建立本地向量索引，再使用本地RAG模式。

## vLLM 临时 Linux smoke

正式团队模式使用两个受控的vLLM OpenAI兼容服务：generation为 `http://127.0.0.1:8000`，embedding为
`http://127.0.0.1:8001`。模型服务由目标主机操作者按固定镜像、固定模型revision和显式served model name启动；本仓库不会自动下载权重，也不会在provider失败时回退到Ollama或云端。

当前临时 GPU smoke 的兼容组合为 vLLM `0.10.2`、Torch `2.8.0+cu128`、Transformers `4.57.6`、
Tokenizers `0.22.1` 和 `huggingface-hub 0.36.0`。vLLM 0.10.2 的依赖元数据允许更高版本 Transformers，
但未固定时会拉取 5.x 并在 Qwen tokenizer 初始化阶段失败；重新建 vLLM 虚拟环境时应显式固定这三项：

```bash
python -m pip install "transformers==4.57.6" "tokenizers==0.22.1" "huggingface-hub==0.36.0"
```

Ubuntu 22.04 系统镜像还需安装 `python3.10-dev` 和 `build-essential`，供 Triton 首次编译 helper；缺少时会以
`Python.h: No such file or directory` 失败。systemd 模板见 `deploy/tracedesk-vllm-generation.service`、
`deploy/tracedesk-vllm-embedding.service` 和 `deploy/vllm-systemd.env.example`。模板显式把 HOME、Triton 和
TorchInductor 缓存指向 `/srv/tracedesk/vllm-cache`，以兼容 `ProtectHome=true`。

在主机上先复制 `docs/industrial/vllm-smoke.env.example` 为私有的 `.env.vllm-smoke`，填入两个模型revision对应的64位digest，再运行协议探针：

```bash
./.venv/bin/python scripts/vllm_protocol_smoke.py --env-file .env.vllm-smoke --output artifacts/vllm-protocol-<run-id>
```

探针通过后，再按[工业化运维配置](industrial/operations.md)提供隔离PostgreSQL和解析镜像，运行
`scripts/industrial_model_smoke.py`。协议探针只证明接口、1024维嵌入、结构化输出、身份和边界处理；它不证明语义质量、权重文件真伪或容量。

## 配置

项目根目录可以放置.env，内容格式参考[.env.example](../.env.example)。现有环境变量优先于.env，随后才使用默认值；不会搜索父目录中的.env，也不会把文件内容写入全局环境。配置按字面值读取，不展开${...}。

| 配置项 | 默认值 | 含义 |
|---|---|---|
| TRACEDESK_DATA_DIR | ./data | 相对路径从项目根目录解析 |
| TRACEDESK_PORT | 8765 | 网页端口，范围1至65535 |
| TRACEDESK_MODEL_PROVIDER | ollama | `ollama`兼容路径或正式`vllm` |
| TRACEDESK_VLLM_CHAT_URL / EMBED_URL | 127.0.0.1:8000 / 8001 | vLLM generation/embedding端点 |
| TRACEDESK_OLLAMA_URL | http://127.0.0.1:11434 | 历史兼容路径的本机HTTP服务 |
| TRACEDESK_EMBED_MODEL / CHAT_MODEL | 按provider默认 | 模型标签，改变digest后需要重建 |
| TRACEDESK_EMBED_MODEL_DIGEST / CHAT_MODEL_DIGEST | vLLM必填 | 固定revision或审核manifest的64位SHA-256 |

也可对单次启动覆盖网页端口：

```powershell
./.venv/Scripts/python.exe scripts/start.py --port 8766
```

start_local_rag.ps1默认连接已经运行的模型服务，并在当前终端运行网页服务，Ctrl+C停止网页服务。保留-RuntimeRoot可选参数，用于历史ollama-0.33.3/ollama.exe与models子目录的独立运行时；该模式使用11435，并把模型服务日志写入data/local_runtime。vLLM由Linux主机单独管理；-Check只执行检查，不启动服务。

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

RC2生成上下文为16384，最多输出3072 tokens。跨语言查询可能先翻译检索词，结构不合法时最多修正一次，单次HTTP超时120秒。长问题可能明显慢于短文档查询；具体成本与未解决的论文问答问题见[RC2报告](rc2-evaluation.md)。

## 数据与更新

数据库包含导入的原文、向量及有限追踪，保存在配置的数据目录中，默认不进入Git。备份或升级前先停止网页服务，再备份整个数据目录，包含可能存在的SQLite WAL文件。恢复备份后核对知识库与模型digest；新模型需要重新建立相应索引。

RC2兼容路径只面向单人本机访问，不提供账号、租户隔离或公网请求限额；工业化团队路径提供账号与知识库权限，但目标部署、容量和真实试用仍需完成验收。公开访问必须由TLS proxy、上传边界和算力限额共同保护。

## 配置实现来源

.env解析使用[python-dotenv的dotenv_values](https://pypi.org/project/python-dotenv/)，应用自行确定文件位置、配置优先级和允许值。配置检查不会上传本地资料。
