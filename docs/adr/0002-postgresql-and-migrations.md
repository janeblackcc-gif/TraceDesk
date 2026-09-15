# ADR-0002：PostgreSQL、显式迁移与旧数据保护

状态：采用。关联：T-010 至 T-014，T-040 至 T-043；PC-05/06/07/08/14/15/19。

使用 PostgreSQL 18 + pgvector 0.8.6、SQLAlchemy 2.0.52、Alembic 1.19.2、
Psycopg 3。数据库初始化只能通过 Alembic；Web 启动和 readiness 校验 schema head。
生产不调用 `create_all`。SQLite 只用于 RC2 兼容测试与只读迁移源。
先 exact vector(1024) 与现有中文 sparse 基线，ANN 须有容量与召回证据。

对 03 号数据字典作以下必要补充，避免后续代码猜测：

- users 增加 `is_system_admin`，workspaces/knowledge_bases 增加 `permission_epoch`。
- revision 增加 `source_version_label`、`source_file_available`、唯一 `migration_key`。
- 保留旧 collection/version 为独立 KB snapshot scope，禁止自动跨版本合并。
- desired/active revision 和 generation 采用含父级 ID 的复合外键，禁止跨资源指针。
- parse/chunk/embedding 保留父级一致性约束；legacy alias 独立保留 tombstone。
- generation membership 同时固定 KB、document、revision 和 parse run；embedding 不能混用另一次解析。
- UUID 使用 Python 3.11/3.13 通用的 UUIDv4；时间统一 timestamptz UTC。
- 旧 quarantined 文档保持隔离，不在迁移时变为可检索文档。

schema 变更采用追加 revision。初始空库扩展可降级；领域数据建成后的破坏性降级
要求备份恢复，不伪造无损 downgrade。任何真实数据迁移需操作者提供已有管理员身份。

核验日期：2026-09-09。来源：[SQLAlchemy](https://pypi.org/project/SQLAlchemy/2.0.52/)、
[Alembic](https://pypi.org/project/alembic/1.19.2/)、
[pgvector](https://github.com/pgvector/pgvector/tree/v0.8.6)。依赖安装与迁移验证记录在 M1 artifacts。
