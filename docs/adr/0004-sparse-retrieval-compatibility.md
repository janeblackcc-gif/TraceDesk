# ADR 0004：保留 RC2 sparse 检索与同分排序

状态：已实现，本机逐题验证通过（2026-09-09）。

PostgreSQL 负责按权限后的 KB、generation、revision、parse run 取得候选；候选仍使用
RC2 的中文 tokenizer、BM25、TF-IDF、RRF 和上下文邻接算法。`search_tsv` 索引保留供后续
消融，不在本次数据库迁移中替换中文检索语义。dense 使用 pgvector exact cosine，profile
和 1024 维约束单独验证；本 ADR 不据此声称 dense 或 hybrid 的质量提升。

迁移把 public chunk ID 改成 UUID。首次在 Atlas 合成资料上对比 RC2 提交
`ccc00c2f5e369bb50a7e459fda0404de1e74b4f3`，40 题 × 2 方法中出现 6 组 hybrid 排名变化。
原因是 UUID 改变了同分排序。保留 legacy_chunk_id 作为迁移候选的稳定排序键，外部来源仍用 UUID。
新文档使用自身 ID；此规则不混用不同 KB/版本的数据。

修复后 `artifacts/integration-20260909-13/sparse-rank-diff.json` 的 80 组排名差异为 0、
版本越界为 0。原始失败保留在 `integration-20260909-12/`。回归测试固定引用该 RC2 commit，
不会随新的 HEAD 漂移。CI 因此须获取该历史对象。

这些是合成开发资料的检索兼容性证据，不是正式 holdout、人工答案评分或 ANN 容量结论。
