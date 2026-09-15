# ADR-0006：采用 vLLM 作为生产模型服务

状态：已采用；真实 GPU 协议 smoke 已通过，正式全链路、容量和生产验收仍未完成。
关联：T-041–T-052、T-075、M8；supersedes ADR-0001 中关于生产模型运行时的选择。

正式部署使用 vLLM 的 OpenAI 兼容 HTTP 服务，不再把 Ollama 作为生产运行时。模型服务分为
generation 和 embedding 两个受控端点，均只绑定 loopback 或后续明确配置的受控内网地址；公网只
暴露 TraceDesk TLS proxy。应用不允许 provider 失败时静默切换到另一模型或云端。

当前应用通过 `BoundedVLLM` 适配 `/v1/models`、`/v1/embeddings` 和 `/v1/chat/completions`，复用
既有的取消、总截止时间、结构化 JSON、引用校验和错误码契约。对 Qwen3 + vLLM 0.10.2，适配器
使用 OpenAI `json_object` 约束语法，并在应用侧继续用严格 Pydantic schema、引用校验和一次修正
保护字段契约；实测完整 `json_schema` 会出现空白循环并以 `finish_reason=length` 截断。嵌入模型和生成模型都必须使用固定
revision，并在配置中提供 64 位 SHA-256 身份；切换 provider 或嵌入权重必须新建 model profile、
完整重建向量索引，禁止混用旧向量。

现有 v1/legacy 请求中的 `profile=ollama` 暂保留为一个兼容周期；它表示本地生成模式，不再选择
运行时。实际 provider 只由 `TRACEDESK_MODEL_PROVIDER` 决定，正式配置固定为 `vllm`。后续如需把
请求字段重命名为 `profile=vllm`，应单独升级 API 版本并迁移客户端，不在本次适配中静默改变协议。

vLLM 的 `/v1/models` 只能确认 served model ID，不能单独证明权重文件 digest。目标环境必须从固定
revision 或审核后的权重 manifest 生成配置 digest，并在启动命令、环境记录和 smoke artifact 中同时
保存 served model ID、revision、digest 与 vLLM 镜像 digest；在这组证据建立前，模型身份只算协议级
校验，不算生产权重验真。

第一候选为 `Qwen/Qwen3-Embedding-0.6B` 与 `Qwen/Qwen3-4B-Instruct-2507`，运行时最低按模型卡要求
使用 vLLM 0.8.5。模型上下文长度、显存、
并发和结构化输出必须在临时 Linux/GPU 主机上实测；本机 Windows/RTX 4060 Ti 仅用于适配测试，
不构成生产容量证据。历史 Ollama 记录保留为基线，但不计入 vLLM 放行证据。

官方接口依据：[vLLM Online Serving](https://docs.vllm.ai/en/latest/serving/online_serving.html)、
[Structured Outputs](https://docs.vllm.ai/en/latest/features/structured_outputs.html)、
[Docker](https://docs.vllm.ai/en/latest/deployment/docker.html)、
[Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)、
[Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)。
