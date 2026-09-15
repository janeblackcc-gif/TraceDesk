# T-075 容量证据负载驱动器 — 实施计划（待审批，未动工）

状态：**计划草案，尚未执行。** 本文件只描述将要做什么，不代表已实施。
预算受限的一次性部署不直接执行本文件；请先使用独立的
[Portfolio Smoke 短期部署计划](plan-portfolio-smoke.md)。本文件的正式门禁和阈值保持不变。
编写依据：`app/operations/capacity.py`、`app/jobs/repository.py`、`app/parsing/worker.py`、
`app/services/index_service.py`、`app/ingest.py`、`app/retrieval.py`、`app/db/session.py`、
`docs/industrial/specification/design-baseline-1.0/{01,06,07,08}`，
以及**四轮只读代码审核 + 一轮主机调研**（均已完成）。

本计划已根据审核修正 **4 处事实错误**（语料配方、`QUEUE_FULL` 触发条件、`force_rebuild`、
索引瓶颈归属），修正处均在文中标注。

---

## 0. 一句话结论

T-075 的正式容量证据**目前无法产出**，缺的不是主机，而是**产出证据的负载驱动器**——
它在仓库里从不存在。本计划从零实现它，并解决三个挡在路上的硬阻塞。

---

## 1. 现状核实（全部经代码或实测验证）

### 1.1 驱动器确实不存在

| 事实 | 证据 |
|---|---|
| `scripts/capacity_report.py` 只是校验器 | `docs/industrial/capacity.md:3`「不制造负载」 |
| `loadtests/` 从未建立 | `06_逐项开发任务清单.md:485`「拟新增」 |
| `samples.jsonl` / `capacity_manifest.json` 只存在于测试夹具 | `tests/industrial/test_capacity.py:50,59` |

⚠️ **诚实性红线**：`tests/industrial/test_capacity.py:8-59` 的合成夹具能“通过”正式门禁，
但它是伪造负载。**绝不可作为 T-075 证据。** 本计划实现的驱动器产出真实样本。

### 1.2 三个硬阻塞

**阻塞 A — 运行中的不是团队版应用。**
`app/main.py:65-70` 以 `DATABASE_URL` 是否存在决定挂载哪套应用。实测 `127.0.0.1:8765`
返回 `single_user:true`，且 `/livez`、`/readyz` 均为 404。**`/api/v1/*` 在当前服务上不存在。**

**阻塞 B — AutoDL 不能跑 Docker，因此不能承载本任务。**
官方文档：容器实例内不支持使用 Docker，需租裸金属，整机包月起租。
而 `app/parsing/worker.py:57` 的 `DockerParser.command()` 硬编码 `docker run`，
compose 的 `parse-worker` 也挂载 `/var/run/docker.sock`。
→ `parse_with_query`、`reindex_with_query`、`worker_crash` 三个场景全部依赖 Docker。
（上轮会话的 vLLM smoke 能成立，仅因为它原生跑 vLLM，没有触碰 Docker。）

**阻塞 C — `QUEUE_FULL` 的真实触发条件（此条曾被我误判，已更正）。**
`app/jobs/repository.py:66` 是 `if global_count >= 20 or user_count >= 2`——
**或**关系，不是与。因此**单个用户在第 3 次活跃提交时就会真实拿到 429 `QUEUE_FULL`**
（`tests/industrial/test_jobs.py:105-111` 已固化此行为）。

⚠️ 但**报告时必须如实描述**：若把它写成"在 ≥10 用户压力下观察到"，
那就是**夸大压力**。真相是单用户配额拒绝，与全局上限 20 无关。

**仍需要多用户的理由与 `QUEUE_FULL` 无关**：`registered_users` 门禁要求 ≥20 个注册用户
（`capacity.py:210`），且 5 并发查询需要 ≥3 个用户才能避开单用户 2 个的上限。

### 1.3 50k chunk 是“刀刃”约束

| 约束 | 值 | 位置 |
|---|---|---|
| 索引上限 | `sum(chunks) > 50000` → 429 | `index_service.py:57-58` |
| 检索上限 | `len(rows) > 50000` → 429 | `rag/sparse.py:31-32` |
| 正式门禁 | `active_chunks < 50000` → FAIL | `operations/capacity.py:208` |
| 单文档上限 | `MAX_CHUNKS = 2500` | `ingest.py:11` |

**三者叠加 ⇒ 活跃 chunk 必须恰好等于 50000。** 50001 会同时被索引和检索拒绝，
49999 则门禁失败。这不是可调参数，是精确目标。

### 1.4 分块算术（决定语料规模）— 已用真实算法验证

`app/ingest.py:59-94`：按行、软上限 **850 字符**、标题感知、最多回退 2 行。
`.md`/`.txt` 走**单页**路径（`ingest.py:30`），`MAX_PAGES=100` 只约束 PDF。

⚠️ **`MAX_TEXT = 1,000,000` 字符/文档**（`ingest.py:10,53-54`）—— 这条决定了配方。
原先设想的"1.7 MB 文档"**不可行**，会直接抛 `解析文本超过 100 万字符`。

**已验证的精确配方**（子代理用真实 `chunks()` 函数实跑）：

```
每文档 = ('## S\n' + 'z'*390 + '\n\n') × 2500
       → 992,499 字符，恰好 2500 块（正好卡在 MAX_CHUNKS 上限）

20 文档 × 2500 块 = 精确 50,000 块
总上传 19,849,980 字节（18.9 MiB），chunk 正文约 19.25 MB
```

块大小由**行宽**决定，实测对照（约 100 万字符/文档）：

| 行宽 | 块数/文档 | 存储膨胀 |
|---|---|---|
| 40 字符 | 1,284 | 1.10× |
| 80 字符 | 1,543 | 1.25× |
| 200 字符 | 2,487 | **2.00×** |
| 390 字符 + 标题 | **2,500（上限）** | 0.995× |

→ **取 390 字符段落是"真实感/成本"最优解**；行宽 200 附近膨胀 2 倍，应避免。

### 1.5 性能实测（决定预算与可行性）— 两个瓶颈，都不在 GPU

**瓶颈一：检索期 Python 分词。** 本机实测 `app/retrieval.py:42`：

```
50,000 块 → 每次分词 1.52–1.84 秒（纯 CPU，不含 DB 与打分）
```

`search()` 被 `search_context()` 按查询变体调用（`retrieval.py:133-136`，
`retrieval_queries()` 最多 4 个变体）。`profile='ollama'` 下 ⇒ **每个问题约 6.1 秒纯 Python**。

**瓶颈二（更严重，此前遗漏）：索引期每批重扫全语料。**

`app/jobs/index_handler.py:87` 在**每个批次的事务内**调用
`manifest_hash(corpus(session, lease.kb_id))`，而 `corpus()` 会加载
**全部 50,000 行 ORM `Chunk`（含 `text` 与 `search_tsv`）**（`index_service.py:52-53`）并整篇 JSON 序列化 + SHA-256。

- 嵌入批大小 = **8**（`index_handler.py:62`）⇒ **6,250 个批次**
- ⇒ **6,250 次全语料重扫**，PG→应用传输量约 **119 GB**
- 实测纯 Python 部分 32.5 ms/批 ⇒ 203 秒；**SQL/ORM 部分估计 0.3–1.5 s/批 ⇒ 31–156 分钟**

**⇒ 索引总耗时约 40 分钟（乐观）到 2.7 小时（悲观），GPU 占空比仅 2–5%。**
成本大头是 O(N²/8) 的清单重校验，不是嵌入。解析只需 **20 次容器运行（约 90 秒）**。

⚠️ **待实测**：`corpus()` 的真实 SQL 成本必须先用 `EXPLAIN (ANALYZE)` 在真库上量出来，
再决定 GPU 窗口预算。这是全局最大的不确定项。

**结论：稳态 `rag`/`evidence` 时延与索引耗时都由 Python/DB 主导，不是 GPU。**
规格的 `evidence P95 ≤ 2s` 与 `queue P95 ≤ 2s` 在当前实现下**很可能超标**——
这正是 T-075 应该如实报告的发现，而不是掩盖。

### 1.6 其他已确认的硬约束

| 项 | 值 | 影响 |
|---|---|---|
| 全局查询准入 | 20 | `repository.py:66` |
| 单用户查询准入 | 2 | `repository.py:66` |
| **同时 running** | **每类型 1（parse 为 2）** | `repository.py:100-102` |
| lease / 心跳 / 重试 | 30s / 10s / 3 次 | `repository.py:85`、`heartbeat.py:14`、`models.py:284` |
| DB 池 | pool 5 + overflow 5，`pool_timeout=5` | `session.py:48-50` |
| 会话 | 绝对 8h，空闲 **30min** | `auth_service.py:102,116` |
| 登录限流 | **整机共享 30 次/分**（uvicorn 无 `--proxy-headers`） | `auth_service.py:48-62`、`start.py:85` |
| Origin | 逐字节匹配，缺失也失败 | `application.py:78` |
| Cookie | `__Host-tracedesk-session`，`Secure` | `auth.py:143-148` |
| CSRF | `sha256(b'csrf:' + cookie)` | `auth_service.py:26-27` |
| 密码 | 12–128 字符，≥4 个不同字符 | `passwords.py:10` |

**三个隐蔽陷阱（会造成静默错误结果）**：
1. `worker_crash` 只能杀**一次** —— `max_attempts=3`，第三次杀会让任务永久 `failed`。
2. `profile='ollama'` 这个**字符串**才是调用 vLLM 的开关（`query_handler.py:73`）；
   默认的 `profile='evidence'` **完全不碰模型**。`model_restart` 场景必须显式指定。
3. `Secure` cookie 在纯 HTTP 下不会被 Python cookie jar 重放 → 必须手动设置 `Cookie:` 头。

---

## 2. 目标主机（已调研，有证据）

### 结论：优云智算 compshare.cn，**系统镜像（虚机）** Ubuntu 22.04 + **RTX 4090 24G**

- **¥2.15/小时**（14 核 / 64 GB 内存 / **100 GB 免费 SSD**），按秒计费，关机不计 GPU 费
- 官方文档明载该镜像可装 Docker（`docs/operation/bestpractices/installdocker`，适用范围为「系统镜像（虚机）」）
- 系统镜像底层是**虚机**而非容器 → `docker run --network none --read-only --cap-drop ALL`
  **无需 `--privileged` 即可工作**（该命令只移除能力，不增加）
- 官方 Dify 指南实证该档有 systemd 与 `docker compose` v2
- 高校邮箱认证享 **95 折**
- 6 小时约 ¥13（含 95 折约 ¥12.4）；**因索引耗时不确定，建议备 ¥20**

### RTX 3090 与 RTX 4090 的选择顺序

优云智算显卡表注明该平台的 3090 档「**需要使用 cuda11.x**」，而本项目当前 vLLM 组合使用
CUDA 12.x；这说明该供应商的镜像/驱动可能受限，**不是 RTX 3090 硬件本身不能运行现代 vLLM**。
已有 AutoDL RTX 3090（driver `570.124.04`、CUDA `12.8`）运行 vLLM `0.10.2` 的 4/4 协议
smoke 作为直接反例。优云智算 3090 是否可用仍必须由实际 `nvidia-smi`、Docker 沙箱和协议
预检决定。

→ 预算受限时先尝试 ¥1.19/小时的 3090；预检失败后再切换约 ¥2.15/小时的 4090。不要在
未预检前把 4090 当作必选项，也不要把供应商的 CUDA 说明泛化成硬件兼容性结论。

### ⚠️ 两个选型陷阱（选错直接前功尽弃，均有官方文档依据）

**陷阱一：镜像类型。** 必须选「系统镜像」，绝不能选「基础镜像」或「社区镜像」：

| 镜像类型 | 底层 | 登录 | 端口 | Docker |
|---|---|---|---|---|
| **系统镜像** | 虚机 VM | `ubuntu` | 22 | ✅ 有 daemon |
| 基础镜像 / 社区镜像 | Docker 容器 | `root` | 23 | ❌ 无 daemon |

官方概念文档原文：「基础镜像：…**底层为Docker环境**」 vs「系统镜像：…**底层为虚机环境（VM）**」。
平台登录文档亦警告：「SSH 登录名与端口取决于实例类型（容器 / 虚机），请勿混用。」

**陷阱二：地域。** 必须选 **华北二A** 或 **上海二B**，**不能选华北一C**：

- 官方地域表：「华北一C（新）… GPU 实例类型：**容器**」
  vs「华北二A · 上海二B … GPU 实例类型：容器/**虚机**」
- **华北一C 根本无法创建虚机**，且无独立公网 IP、不支持数据盘

**另外：不要用抢占式**（4090 ¥1.31/hr 虽便宜，但实例可能被中途回收，
会毁掉一次性 30 分钟正式跑批）。

### 系统镜像无预装环境（需预留时间）

官方：「系统镜像：虚拟机，**无任何预装环境**」。需手动装 NVIDIA 驱动 + CUDA + Docker +
`nvidia-container-toolkit`。**预留 20–30 分钟**。这直接说明为什么必须做下面的三命令预检。

### 上机后必须首先通过的三命令预检

这三条决定整个方案是否成立，**必须在构建任何东西之前跑**：

```bash
nvidia-smi                                                              # 驱动/CUDA ≥ 550 / 12.x
docker compose version                                                  # compose v2 可用
docker run --rm --network none --read-only --cap-drop ALL alpine true   # 复刻 parse worker 沙箱
```

第三条**精确复刻** `app/parsing/worker.py:57` 的沙箱参数。**这三条不过，整个方案不成立，
此时止损成本几乎为零。** 因此驱动器会把它们实现为 `--preflight`，在正式跑批前强制校验。

### 学生优惠（诚实结论：没有免费的 24GB GPU 额度）

调研明确结论：**没有任何可核实的项目能让大陆学生免费获得"带 Docker 和 root 的 24GB GPU 时长"**。
已核实的优惠要么是纯 CPU 券，要么是明确不支持 Docker 的 GPU 折扣：

| 项目 | 实际内容 | 可用性 |
|---|---|---|
| **优云智算 高校邮箱认证** | **95 折**算力优惠（`.edu.cn` 邮箱即可） | ✅ **唯一真正有用** |
| 阿里云 云工开物 | ¥300 券，但**未列明覆盖 GPU**，且不可叠加 | ⚠️ 未核实 |
| 腾讯云 云+校园 | 目录为**纯 CPU**，无 GPU SKU | ❌ |
| 腾讯云 Cloud Studio | 首次绑定送 20 机时，但 GPU Docker 未经证实，且无 24GB 卡 | ⚠️ 未核实 |
| 共绩算力 新手券 | 无门槛 ¥50（≈24 小时 4090 积分） | ❌ **仅可用于云主机/弹性部署档，而该档官方禁用 Docker** |
| AutoDL 学生认证 | 仅会员价，**无免费 GPU 额度**；且容器禁 Docker | ❌ |

→ **不要按免费额度做预算，按 ¥12–20 备。**

### 一条安全发现（与本项目无关，但影响候选评估）

`docs.lanyun.net` 是**被接管的子域名**：页面标题为 `Subdomain takeover by kresec - Mint Starter Kit`，
实际提供一个无关的 Yapily 金融文档模板。实际后果是**蓝耘没有任何可用官方文档**
（`www.lanyun.net/docs` 404，其余子域 TLS 失败），因此其 Docker 判定必须保持 UNVERIFIED ——
**是没有厂商文档可查，而不是"没找到文档"**。该平台 ¥2.3/卡时也不比优云智算 ¥2.15 便宜，无冒险价值。

（说明：子代理曾报告该页面存在"提示注入指令行"。复核后**未发现任何注入文本**，
唯一近似匹配是模板自身的 CSS 类名 `--assistant-sheet-width`。该指控属**夸大**，已更正。
全程未执行或遵循任何抓取页面的内容。）

### 兜底方案的边界（重要）

PG 18.6 + pgvector 0.8.6 在 PGDG 有 **jammy 预编译包**
（`postgresql-18` 18.6-1.pgdg22.04+2、`postgresql-18-pgvector` 0.8.6-1.pgdg22.04+1，
国内镜像同步），原生安装约 5 分钟，无需编译。

**但它只能替代 postgres 容器**：既不能替代 compose 编排，
也**完全不能替代 parse worker 的 `docker run` 沙箱**（那需要改用 bubblewrap/nsjail 重写，属于代码改动）。
→ **结论：买能跑 Docker 的虚机，不要在缺失 daemon 的平台上做设计妥协。**

### 已排除的候选（附证据）

| 平台 | 判定 | 证据 |
|---|---|---|
| AutoDL | ❌ | 官方：容器内不支持 Docker，裸金属包月起租 |
| 共绩算力 云主机 | ❌ | 官方 FAQ：容器化环境，不支持内部再启 Docker |
| 恒源云 | ❌ | 官方：实例以 Docker 容器方式运行，不能安装和使用 Docker |
| 阿里云 PAI-DSW | ❌ | 官方：DSW 本身是容器，不支持内部再装 Docker；DinD 仅限灵骏专有资源组（包年包月） |
| 阿里云 ECS gn7i | ❌ | 约 ¥3200/月，超预算两个数量级 |

**兜底方案**：PG 18.6 + pgvector 0.8.6 在 PGDG 有 jammy 预编译包
（`postgresql-18` 18.6-1.pgdg22.04+2、`postgresql-18-pgvector` 0.8.6-1.pgdg22.04+1），
原生安装约 5 分钟。但它**只能替代 postgres 容器，不能替代 compose 编排与 parse worker 的 Docker 沙箱**，
因此只是部分兜底。

---

## 3. 驱动器架构

单体式（已获批准）：负载驱动 + 故障注入 + 宿主采样器合为一个脚本，**部署在目标主机上运行**。

```
scripts/capacity_driver.py          # 唯一入口
  ├── preflight.py     --preflight：三命令校验（nvidia-smi / compose / 沙箱），不过即拒绝跑批
  ├── corpus.py        造 50000 活跃 chunk + 20 用户（可复现种子）
  ├── client.py        纯 HTTP 客户端（手动 Cookie/CSRF/Origin/Host/幂等键）
  ├── sampler.py       只采应用自身 cgroup 的 CPU/RAM/VRAM，排除驱动器自身
  ├── scenarios/       10 个场景，各自独立可测
  └── manifest.py      写 environment.json + samples.jsonl + capacity_manifest.json + sha256
```

**`--preflight` 是成本保护机制**：在计费窗口内，最先执行且最先失败。
若 Docker 沙箱或驱动不满足要求，驱动器**拒绝进入跑批**，避免把 30 分钟浪费在注定失败的跑批上。

**为什么必须跑在目标主机上**：故障注入需要 `docker compose stop/kill`，
采样需要读 cgroup 与 `nvidia-smi`。

**为什么采样要排除驱动器自身**：驱动器本身是 Python 进程，占 CPU/RAM。
若把它计入，`max_rss_growth_bytes` 会反映驱动器的泄漏而非应用的泄漏，结论失真。
做法：`docker inspect` 取容器 PID，只读应用 cgroup；VRAM 用 `nvidia-smi` 按 PID 过滤只取 vLLM。

### 测量语义裁决（审核提出、计划采纳）

| 操作类 | 定义 |
|---|---|
| `api` | 非模型控制面：`/me`、`/jobs`、`/documents`、`/auth/csrf`、`/knowledge-bases/{id}`、`/readyz` |
| `evidence` | 读回：`GET /queries/{id}`、`/trace`、`/document-revisions/{id}/source` |
| `queue` | `POST /api/v1/queries` 的准入时延（202 或 429），随 `query_queue_depth` 记录 |
| `rag` | **主指标** = 客户端 submit → 终态轮询；**交叉校验** = 应用侧 `timing.total_ms` |
| `resource` | cgroup RSS + `nvidia-smi` VRAM，按稳态场景归集 |
| `control` | 驱动器自身的故障注入动作，与被测系统的响应分开记录 |

---

## 4. 十个场景的注入设计

| # | 场景 | 注入机制 | 诚实性 |
|---|---|---|---|
| 1 | `steady_query` | 5 并发（≥10 用户轮转）、`profile='evidence'`、`method='bm25'` | ✅ 真实负载 |
| 2 | `parse_with_query` | 2 个大 PDF（接近上限）解析期间持续查询 | ✅ 真实解析容器 |
| 3 | `reindex_with_query` | `POST /index-generations` 且**必须带 `{"force_rebuild": true}`**，期间持续查询 | ✅ 真实重索引 |

⚠️ `force_rebuild` 不是可选项：`force_rebuild:false` 且清单未变时，
`schedule` 直接返回 `{'state':'active','job_id':None}` 并**不入队任何任务**（`index_service.py:119-121`）。
不带这个字段，该场景会静默变成空转。
| 4 | `model_restart` | 停/启主机侧 vLLM（`profile='ollama'` + `method='hybrid'`）；预期 202→轮询得 `partial` + `MODEL_CONNECTION_FAILED` | ✅ 真杀真起 |
| 5 | `worker_crash` | `docker compose kill query-worker`，**仅一次**；lease 30s 后回收 | ✅ 真崩溃 |
| 6 | `database_restart` | `docker compose stop postgres` → `start`；**只重启 postgres，不动 web**；客户端超时 ≥15s | ✅ 真 DB 中断 |
| 7 | `queue_full` | 停止 query-worker，≥10 用户并发提交 21+ 个 → 429 `QUEUE_FULL` | ✅ 被测系统真实拒绝 |
| 8 | `disk_pressure` | 64MB loopback ext4 挂在 objects 路径，真实写满 → ENOSPC | ✅ 真磁盘满 |
| 9 | `stale_write_race` | 重放同一 `Idempotency-Key` 改 payload → 409 `IDEMPOTENCY_CONFLICT` | ✅ 真实版本化拒绝 |
| 10 | `steady_observation` | 30–60 分钟稳态资源观察 | ✅ 真实采样 |

### 关于场景 8 与 9 的诚实声明

- **`disk_pressure`**：应用内**没有**磁盘阈值检查，也**没有**磁盘错误码
  （`app/` 全局只有 `metrics.py:68` 一个只读 gauge；15%/8% 阈值仅存在于文档的建议告警中）。
  唯一真实的磁盘失败信号是 `/readyz` 的 **503 `STORAGE_UNAVAILABLE`**，且只在真实写失败时触发。
  本计划**如实记录该信号**，并在报告中明确标注这是应用的观察缺口，不编造 `DISK_*` 码。
  另注：校验器 `capacity.py` **不检查** `disk_pressure` 的失败/恢复对，因此绝不能用“全 ok”蒙混。
- **`stale_write_race`**：真实的 `AUTHORIZATION_CHANGED` 竞态需要精确插入两次 `recheck` 之间的
  无锁窗口，外部驱动器只能撞运气（DB 层 `FOR UPDATE` 与建议锁会主动串行化）。
  经你裁决，采用**确定性**的 `IDEMPOTENCY_CONFLICT` 路径，并在样本中注明其确切含义。

---

## 5. 分阶段实施

| 阶段 | 内容 | 地点 | 成本 | 完成标志 |
|---|---|---|---|---|
| **A** | 起团队版实例 + 驱动器骨架 + manifest 构建器 + 真实性守卫 | 本机 | ¥0 | 生成校验器接受的**结构** |
| **B** | 语料生成器：精确 50000 块 + 20 用户（可复现种子） | 本机 | ¥0 | 真实 chunk 计数 = 50000 |
| **C** | HTTP 负载引擎（手动 Cookie/CSRF/Origin、5 并发、幂等键、时延埋点） | 本机 | ¥0 | 四类样本稳定产出 |
| **D** | 4 个稳态场景（含 2 个大 PDF 解析） | 本机 | ¥0 | 4/10 |
| **E** | 6 个故障场景 + 采样器 | 本机 | ¥0 | 10/10 |
| **F** | 正式 30 分钟跑批（`--formal`） | 优云智算 4090 | ~¥13（备 ¥20） | T-075 PASS/FAIL 报告 |

**阶段 A–E 全部在本机零成本完成。** 产出物为 `scope=local-readiness` 的完整 dry-run，
结构与正式跑批完全一致，只有 scope 与真实硬件身份不同。
这样阶段 F 是**执行**而非**调试**，把烧钱窗口压到最短。

### 阶段 F 的时间预算（GPU 计费窗口内）

⚠️ **索引阶段是最大不确定项**（每批重扫全语料，见 §1.5）：乐观 40 分钟，悲观 2.7 小时。

| 活动 | 预计 |
|---|---|
| 驱动 + CUDA + Docker + nvidia-container-toolkit 安装 | 20–30 min |
| 三命令预检 | 2 min |
| `docker compose up` + 拉取镜像 | 10–15 min |
| **建 50000 chunk 索引** | **40 min – 2.7 h（不确定）** |
| **正式 30 分钟跑批** | **30–35 min** |
| 证据收集与销毁实例 | 10 min |
| **合计** | **约 2–4 小时** |

→ 按 4 小时计约 **¥8.6**；**建议按 6–8 小时备（¥13–17）**，留足调试与重跑余量。

### 最大的成本杠杆：索引阶段不必占用 GPU 窗口

审核发现索引期 **GPU 占空比仅 2–5%**——瓶颈是 O(N²/8) 的清单重扫与 DB 写入，不是嵌入。
而正式跑批中：

- 稳态 5 个场景用 `profile='evidence'`，**完全不调用模型**
- 只有 `model_restart` 需要 vLLM 在线

⇒ **可把建索引放在本机（或任何便宜 CPU/GPU 环境）完成，只把正式跑批放进计费窗口。**
这样 GPU 窗口可压缩到 **1.5–2 小时（约 ¥3–5）**。

⚠️ 前提约束：索引的 `embedding_digest` 由建索引时的 provider 决定。
若本机与目标机用不同嵌入模型，digest 不匹配会使跑批取不到索引。
→ 实施时必须先确认两端 digest 策略；**若无法对齐，就退回"全程在 GPU 主机上建索引"**
（此时按上表 2–4 小时预算）。这一点在阶段 D 结束前必须实测确认。

### 阶段 A 的具体启动方式（本机）

用现有 `tracedesk-industrial-pg-20260909`（PG 18.6 + pgvector 0.8.6，端口 62696）
与已构建镜像 `tracedesk-app:industrial-20260911-02`、`tracedesk-parser:industrial-secure-20260911-02`，
以独立数据目录、独立端口、新 bootstrap token 起一个团队版实例。
需要显式提供 `TRACEDESK_EMBED_MODEL_DIGEST` / `TRACEDESK_CHAT_MODEL_DIGEST`（64 位小写 hex，
已有可用值见 `artifacts/vllm-gpu-smoke-20260912-03/vllm-smoke.env`）。

---

## 6. 将要新增／修改的文件

**全部为新增**，改动面刻意压到最小：

| 文件 | 动作 |
|---|---|
| `scripts/capacity_driver.py` + `loadtests/*.py` | 新建 |
| `scripts/vllm_service.sh`（或 `.py`） | 新建 —— 主机侧 vLLM 启动/停止包装（仓库内确实没有，无此则场景 4 无法脚本化） |
| `tests/industrial/test_capacity_driver.py` | 新建（驱动器自身回归） |
| `deploy/compose.capacity.yaml` | 新建（可选：loopback 盘与采样挂载） |
| `docs/industrial/capacity.md` | **追加**「驱动器使用说明」一节 |
| `docs/industrial/status.md` | **追加** T-075 进展一行 |

**不会改动**：`app/` 业务代码、`.release-work/`、`evidence/`、`artifacts/` 内任何冻结证据、
任何历史失败记录。

---

## 7. 反作弊与合规约定

1. 每个故障样本必须来自**真实状态变更**（真停容器、真 `kill`、真打满配额、真 ENOSPC），不伪造。
2. 驱动器自身的控制动作标记为 `control`，与被测系统响应分离。
3. 20 个用户仅用于容量测量；**绝不用于关闭 FA-21 真实试用门禁**
   （`acceptance-supplement.md:9` 明确禁止用模拟用户关闭该项）。
4. 不用后续成功覆盖历史失败记录；失败 run 保留现场，修复后新建 run id。
5. 报告如实标注 `scope` 与剩余阻塞项；不人工判绿。

### 已识别的造假漏洞（驱动器必须绕开，审核逐条列出）

| # | 漏洞 | 为什么危险 |
|---|---|---|
| 1 | `tests/industrial/test_capacity.py:53-59` **硬编码 `'active_chunks': 50000`** | `evaluate_capacity` **只读 JSON、从不连 Postgres** ⇒ 在**空库**上也能返回 `passed, active_chunks: 50000`。**最大造假漏洞，绝不可引用。** |
| 2 | `samples.jsonl` / `environment.json` 全为自述文件 | SHA-256 只证明**一致性**，不证明**真实性**。所有时延/RSS/VRAM 数字都是驱动器写什么就是什么。 |
| 3 | `IndexGeneration.chunk_count` 是**声明值** | 排程时由语料求和写入（`index_service.py:135`），`activate` 只拿它和该代的 `ChunkEmbedding` 计数比对（`:184-186`）。**不是"当前可见活跃 chunk"数** ⇒ **不许报告此字段，必须跑 §1.3 的真 SQL。** |
| 4 | `SELECT count(*) FROM chunks` **会高估** | 删除有 7 天窗口（`deletion_service.py:17`），且被取代代的 embeddings 会留存（`index_service.py:187-189`）。 |
| 5 | **向量内容无法被校验器验证** | 直接向 `chunk_embeddings` 插入任意 1024 维向量即可满足 `activate` 的计数检查（`:184`）并通过全部门禁，而稠密检索毫无意义。**嵌入必须来自真实 provider。** 测试替身（`test_indexing.py:19-23`、`industrial_browser_smoke.py:42-45`）只能用于单元测试。 |
| 6 | `sparse.candidates` 有 `.limit(50001)` | 用 `len(candidates(...))` 无法证明 >50,000。 |

**强制措施**：驱动器必须把 §1.3 的真 SQL 原始输出与用户计数 SQL 一并写入证据目录，
**让审阅者能自行重算，而不是选择相信**。

### 已识别的正确性陷阱

**`data_epoch` 递增会使在途查询失效。** `capture()` 记录 `data_epoch`（`authz/policy.py:78-79`），
`recheck()` 抛 `AUTHORIZATION_CHANGED`（`policy.py:82-88`），在 `query_handler.py:51,93,112,156` 四处调用。
`parse_with_query` 每解析一次就递增（`parse_handler.py:92`），每次激活也递增（`index_service.py:194`）。

→ **这些 409 会真实发生，并计入 `max_error_rate ≤ .05` 的稳态错误率**（`capacity.py:20,220-223`）。
预期在激活边界会看到少量合法 409，必须**要么重试、要么在报告中解释**，不能当作驱动器 bug 掩盖。

**`registered_users` 在代码中无定义**：`capacity.py:87` 只要求 ≥1。
→ 驱动器必须**同时报告** `SELECT count(*) FROM users` 与 `WHERE status='active'` 两个数，并注明用了哪个。

---

## 8. 已知风险与未决项

| 风险 | 说明 | 应对 |
|---|---|---|
| **`evidence`/`rag` P95 可能超标** | 实测单次分词 **1.52–1.84 s** @50k；`profile='ollama'` 最多 4 个查询变体 ⇒ **约 6.1 s 纯 Python/问题** | **如实报告**，这是 T-075 应交付的发现 |
| **`queue` P95 ≤ 2s 可能超标** | 查询执行被串行化为**全部署仅 1 个 running**（`repository.py:100-102`），5 并发提交下排队必然拉长 | 如实报告；用 `timing.queue_ms` 把排队与服务时间分开 |
| **索引耗时远超预期** | 每批重扫全语料 ⇒ O(N²/8)，6,250 次重扫、约 119 GB 传输；**GPU 占空比仅 2–5%** | 先 `EXPLAIN (ANALYZE)` 实测 `corpus()` 成本，再定 GPU 窗口 |
| **`corpus()` 每批 SQL 成本未实测** | 估计 0.3–1.5 s/批 ⇒ 31–156 分钟，是**全局最大不确定项** | **上 GPU 前必须先在本地量出来** |
| **激活/解析引发合法 409** | `data_epoch` 递增使在途查询 `AUTHORIZATION_CHANGED`，计入 `max_error_rate ≤ .05` | 重试或在报告中解释，不当 bug 掩盖 |
| DB 池 5+5 会成为瓶颈 | >10 并发 DB 请求 → 503 `DATABASE_UNAVAILABLE` | 如实记录，不掩盖 |
| `statement_timeout=10000` | 每会话 10s，50k 行 ORM 查询可能踩线 | 建索引阶段单独观察，必要时说明 |
| 30min 空闲超时 = 跑批时长 | 中途重登录会撞 30 次/分限流 | 跑批前一次性登录 20 用户，期间保持活跃 |
| **三命令预检不过** | 若 Docker/沙箱不可用，整个方案不成立 | `--preflight` 强制前置；不过则立即止损（成本≈0） |
| **3090 档的供应商驱动限制** | 该平台镜像可能只提供 CUDA 11.x；硬件本身已有 CUDA 12/vLLM 成功实测 | 先做 `nvidia-smi`、Docker 沙箱和协议预检，失败再切 4090 |
| **误选华北一C 或基础镜像** | 华北一C 只能建容器；基础镜像无 daemon | 地域选**华北二A/上海二B**，镜像选**系统镜像** |
| 系统镜像需装驱动/CUDA | 无预装环境 | 预留 20–30 分钟，已计入时间预算 |
| 实例销毁前系统盘仍计费 | 关机不收费，但云盘保留期间收费 | 跑批后**销毁实例**，不只关机 |
| 优云智算 22.04 镜像待确认 | 文档称支持 22.04/24.04 | 控制台创建时确认 |
| `nvidia-container-toolkit` 可用性 | vLLM 若原生跑则不需要 | 优先原生跑 vLLM（与上轮 smoke 一致） |

**已核实的负面结论（不再重复调研）**：共绩算力与恒源云**官方明确禁止**实例内 Docker；
阿里云 PAI-DSW 的两份官方文档自相矛盾，可用路径仅限灵骏专有资源组（包年包月）；
AutoDL 裸金属**整机包月起租**。腾讯云 HAI 无法指定 GPU 型号且 Docker 未证实。

**尚未核实**：优云智算 22.04 镜像的具体可选项；3090 档库存与可用驱动版本；
`nvidia-container-toolkit` 在该 VM 上的安装情况。

---

## 附录 A：主机调研完整证据链（含官方原文与出处）

本节吸收自一次独立主机调研，保留全部可核验引用，以便日后无需重新调研。
**所有引文均为官方页面原文**；抓取内容一律按不可信数据处理，未执行任何页面指令。

### A.1 推荐主机：优云智算 compshare.cn

| 项 | 值 |
|---|---|
| 产品档位 | **系统镜像**（虚机 VM）Ubuntu 22.04 |
| GPU | **RTX 4090 24G** @ **¥2.15/小时**（14 核 / 32 GB）或 ¥2.13（14 核 / 64 GB） |
| 更便宜选项 | RTX 3090 24G ¥1.19/hr（16 核 / **64 GB**）— 但见 A.2 的 CUDA 警告 |
| 系统盘 | **100 GB SSD 免费**（华北二A / 上海二B） |
| 私有镜像存储 | 30 GB 免费，超出 ¥0.008/GB/日 |
| 计费 | **秒级计费**，**关机不收费**（云盘与镜像保留期间仍计费） |
| 长期承诺 | **无**，可当日销毁 |
| SSH | `ssh ubuntu@<外网IP>` 端口 22 |

**Docker 可用性的官方证据**（这是选它的唯一理由）：
官方页面「GPU实例如何安装Docker」首行即限定适用范围：

> 「适用于系统镜像（虚机）」
> — https://www.compshare.cn/docs/operation/bestpractices/installdocker

**为什么是虚机而非嵌套容器** —— 官方镜像表与概念文档：

> 「平台镜像-系统镜像 | 虚拟-Ubuntu | ubuntu | 22」
> 「平台镜像-基础镜像 | 容器 | root | 23」
> — https://www.compshare.cn/docs/operation/gpu/community

> 「基础镜像：平台官方提供的框架镜像，如PyTorch、Tensorflow等，**底层为Docker环境**。」
> 「系统镜像：平台官方提供的系统镜像，如Windows、Ubuntu等，**底层为虚机环境（VM）**。」
> — https://www.compshare.cn/docs/operation/introduce/basicconcepts

**systemd 与 `docker compose` v2 已实证**（官方 Dify 指南用 `sudo systemctl restart docker`、
`sudo docker compose pull`）— https://www.compshare.cn/docs/operation/bestpractices/installdify

**地区限制的官方依据**（决定能否创建虚机）：

> 「华北一C（新）… GPU 实例类型：**容器**」
> 「华北二A · 上海二B … GPU 实例类型：容器/**虚机**」
> — https://www.compshare.cn/docs/operation/introduce/region

**计费依据**：

> 「后付费-按量计费 — 按照小时计费的后付费模式，**秒级计费**」
> 「按量计费的实例均已**开通关机不收费**，关机后CPU、GPU和内存会被回收并停止收费，云盘和镜像资源会保留并继续收费」
> — https://www.compshare.cn/docs/operation/charge/billdescribe

**登录名陷阱的官方警告**：

> 「SSH 登录名与端口取决于实例类型（容器 / 虚机），请勿混用。」
> — https://www.compshare.cn/docs/operation/gpu/logininstance

### A.2 RTX 3090 的供应商驱动风险

优云智算显卡表注明 3090「**需要使用cuda11.x**」，因此其特定镜像/驱动可能无法直接使用
本项目的 CUDA 12.x vLLM 组合。该说明不能推导出 3090 硬件普遍不兼容：AutoDL RTX 3090
已在 driver `570.124.04`、CUDA `12.8`、vLLM `0.10.2` 下通过协议 smoke。
→ 新平台必须先做实际预检；只有预检失败时才为确定性切换 4090。

**抢占式价格**（4090 ¥1.31、3090 ¥0.83）**不可用于正式跑批**：实例可能被中途回收。

### A.3 被排除的候选（全部有官方原文，非推测）

**共绩算力 云主机 —— 官方明确禁用：**

> 「云主机采用**容器化运行环境，而非传统虚拟机，因此不支持在主机内部再启动 Docker 服务**。」
> 「平台不直接连接外网，无法直接使用外网的镜像仓库比如 **docker.io** 等」
> — https://suanli.cn/docs/cloud-hosting/faq/vwuvwbms2ipchlkzyftcrgwlnmd/

更明确的机制说明：「**平台在系统层面禁用了云主机内部的 Docker 服务**」。
其「弹性服务部署」档**完全没有 SSH**。
⚠️ 陷阱：其 `/docs/docker/` 页面讲的是把 compose 文件粘贴到**网页控制台**，与实例内 Docker 无关。

**恒源云 —— 官方明确禁用：**

> 「实例是以 **Docker 容器方式运行**…在容器实例中具有如下限制：…**不能安装和使用 Docker 容器**」
> — https://www.gpushare.com/docs/instance/environment/

**AutoDL —— 官方明确禁用，裸金属包月起租：**

> 「容器实例内不支持使用Docker，如需使用Docker请联系客服租用裸金属服务器（**裸金属整机包月起租**）」
> — https://api.autodl.com/docs/env/

**阿里云 PAI-DSW —— 两份官方文档自相矛盾，可用路径为包年包月：**
一份称「DSW本身是一个容器环境，**不支持在内部再安装或使用Docker**」；
另一份称支持二级容器，但**仅限灵骏/通用资源组**（= 专有资源 = 包年包月），
且即便在那也禁用 `--privileged`/`--ipc=host`/`--security-opt`/`--cap-add`，且无法拉取 Docker Hub。

| 平台 | Docker | 证据强度 |
|---|---|---|
| **优云智算（系统镜像）** | ✅ 可用 | 官方安装文档（限定虚机档） |
| 共绩算力 云主机 | ❌ 系统层禁用 | 官方 FAQ 原文 |
| 恒源云 | ❌ 明确禁止 | 官方文档原文 |
| AutoDL | ❌ 容器不支持 | 官方文档原文 |
| 阿里云 PAI-DSW | ⚠️ 仅包年包月灵骏 | 两份官方文档冲突 |
| 腾讯云 Cloud Studio | ❓ 未经证实 | 仅营销文章，非产品文档 |
| 腾讯云 HAI | ❓ 未证实 | 且**无法指定 GPU 型号** |
| 蓝耘 lanyun.net | ❓ 无官方文档可查 | `docs.lanyun.net` 已被接管（见 A.5） |
| 揽睿星舟 lanrui.co | ❓ 未证实 | 文档只讲"推镜像到平台" |

### A.4 原生 PG 兜底方案（已核实包存在）

从**实时 PGDG 仓库索引**核实（非凭记忆）：
`https://apt.postgresql.org/pub/repos/apt/dists/jammy-pgdg/main/binary-amd64/Packages.gz` 包含：

- `postgresql-18` — **`18.6-1.pgdg22.04+2`**
- `postgresql-18-pgvector` — **`0.8.6-1.pgdg22.04+1`**

即所需版本**有 Ubuntu 22.04 预编译 deb，无需源码编译**；国内阿里云/腾讯镜像同步同版本。

⚠️ **但只是部分兜底**：只能替代 postgres 容器，**不能替代 compose 编排**，
也**完全不能替代 parse worker 的 `docker run` 沙箱**（需改用 bubblewrap/nsjail 重写 = 代码改动）。
→ **结论：买能跑 Docker 的虚机，不要在缺 daemon 的平台上做设计妥协。**

### A.5 安全发现（与本项目无关，但影响候选评估）

`docs.lanyun.net` 是**被接管的子域名**：页面标题为 `Subdomain takeover by kresec - Mint Starter Kit`，
实际提供一个无关的 Yapily 金融文档模板。后果：**蓝耘没有任何可用官方文档**
（`www.lanyun.net/docs` 404，`help./support./manual.lanyun.net` TLS 失败），
因此其 Docker 判定只能保持 UNVERIFIED —— **是没有厂商文档可查，不是"没找到"**。
该平台 ¥2.3/卡时也不比优云智算 ¥2.15 便宜，无冒险价值。

（说明：曾有一份子代理报告称该页面存在"提示注入指令行"。复核后**未发现任何注入文本**，
唯一近似匹配是 Mintlify 模板自身的 CSS 类名。该指控属**夸大**，已更正。
全程未执行或遵循任何抓取页面的内容。）

### A.6 上机操作清单

1. 用 **`.edu.cn` 邮箱**注册 https://www.compshare.cn/ ，完成实名 + 高校邮箱认证（95 折）
   — https://console.compshare.cn/uaccount/authentication
2. 创建实例：地域 **华北二A / 上海二B**（**不是华北一C**），镜像 **系统镜像 → Ubuntu 22.04**，
   GPU **RTX 4090 24G**，计费 **按量计费**
3. 装 NVIDIA 驱动 + CUDA 12（https://www.compshare.cn/docs/operation/bestpractices/installlinux ），
   再装 Docker（…/installdocker），再装 `nvidia-container-toolkit`
4. **跑三命令预检**（见 §2）：`nvidia-smi`、`docker compose version`、
   `docker run --rm --network none --read-only --cap-drop ALL alpine true`
5. 预检通过后才 `docker compose up`、拉取 `pgvector/pgvector:0.8.6-pg18-trixie`、启动 vLLM
6. 设**定时关机**；**跑批后销毁实例**（关机不收费，但云盘保留期间计费）

---

## 9. 下一步

计划获批后，先执行**阶段 A**（本机、零成本）：

1. 起团队版实例，验证 `/livez`、`/readyz`、`/api/v1/auth/bootstrap` 可用
2. 写驱动器骨架、`--preflight` 与 manifest 构建器
3. 用**小语料**（数百 chunk）跑通 10 个场景的**结构**验证

确认驱动器真实工作后，再进入放大与 GPU 阶段。**在你明确批准前不创建任何云实例。**
