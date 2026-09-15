# ADR-0003：租约任务、内容寻址文件与服务端会话

状态：采用。关联：T-012，T-020 至 T-034；PC-01/02/03/04/09/10/11/12/14/18。

任务使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 短事务领取。执行期不持锁；
lease 默认 30 秒、heartbeat 10 秒。最终写回检查 owner/attempt、cancel、deleted、
desired revision 和 corpus manifest。重试预算和队列上限持久化，禁止无限重试。

原文件位于 Web root 外，以 SHA-256 命名，先写临时文件并 fsync，再原子发布，
绝不覆盖既有 digest。用户文件名仅作元数据。数据库提交失败产生的对象由延迟 GC 识别；
存储模块只报告候选，真实清理遵循部署保留策略。

默认本地账号、Argon2id、服务端 session，token 仅存哈希；Cookie Secure/HttpOnly/SameSite，
写请求检查 Origin 和 CSRF。无 membership 默认拒绝，来源/trace/导出/job/eval 同样鉴权。
在途请求于模型前和返回前复核 auth/permission/data epoch。高风险写操作与审计同事务，
审计失败则操作失败。普通日志只允许元数据，禁止密码、连接串、问题或原文。

解析采用宿主 worker 调用独立 Docker 容器；不把 Docker socket 挂入解析容器。
容器只获当前只读输入，无数据库/模型凭据或网络；有 CPU、内存、PID、临时空间和输出上限。
宿主监督、子进程墙钟计时、重启后过期容器回收共同处理失去监督的情况。
部署时需审查宿主 Docker 权限边界；本机 Docker Desktop 验证不代替 Linux 运维验收。

密码参数依据 [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
于 2026-09-09 核验：Argon2id，19 MiB、2 次迭代、并行度 1。登录/bootstrap 的无会话阶段
使用 Origin 和数据库限流；登录后写请求再要求 CSRF。全写接口幂等、完整审计覆盖在 T-050/T-071 继续收口。
