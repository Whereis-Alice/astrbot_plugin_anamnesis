# Changelog

所有重要的更改都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并且遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

Anamnesis 由上游插件 `astrbot_plugin_livingmemory` v2.6.1 派生而来。3.0.0 之前的历史记录原样保留在 [CHANGELOG_upstream.md](CHANGELOG_upstream.md)，其中的命令名与标识符均为旧版，不适用于本插件。

## 3.0.0

### 破坏性变更

- 插件更名为 **Anamnesis**：插件 id、目录名与数据目录统一为 `astrbot_plugin_anamnesis`。
- 命令组由 `/lmem` 改为 `/anam`（别名 `/amem`），所有子命令前缀随之变化。
- 数据目录随插件 id 变更，旧插件的数据不会被自动读取；从 LivingMemory 升级需要执行 `/anam migrate` 迁移，详见 [docs/guide/getting-started.md](docs/guide/getting-started.md)。
- Agent 工具更名：`recall_long_term_memory` → `anamnesis_recall_memory`，`memorize_long_term_memory` → `anamnesis_memorize_memory`；按旧工具名编写的人格提示词需要同步更新。
- 记忆注入标记由 `<RAG-Faiss-Memory>` / `</RAG-Faiss-Memory>` 改为 `<Anamnesis-Memory>` / `</Anamnesis-Memory>`；`/anam cleanup` 只识别新标记，旧插件残留在历史消息中的标记需要旧插件自行清理。
- 最低 AstrBot 版本要求为 4.24.2。

> 数据库内部标识符 `livingmemory_memories_fts`、`livingmemory_graph_entries_fts`、`livingmemory_legacy_documents_fts_backup` 以及会话作用域前缀 `livingmemory:` **故意保留原名**。它们是已入库数据的一部分，改名会让迁移过来的旧库无法识别；这些名字只存在于 SQLite 内部，不影响任何对外接口。

### 新增

- `/anam migrate [preview|exec]`：从旧插件 `astrbot_plugin_livingmemory` 的数据目录只读迁移全部数据。默认 `preview` 只报告将要迁移的文件与体积，`exec` 才真正执行；跳过旧 `backups/` 目录以节省磁盘。迁移全程只读旧目录，校验用 blake2b 摘要逐文件比对，并对每个 SQLite 库执行 `quick_check`，任一步失败自动回滚。
- `/anam migrate-verify`：迁移后对账，逐表比对 documents / memory_atoms / graph_entries / graph_edges / graph_nodes / conversations 的行数是否与旧库一致。
- 首次启动自动导入旧插件数据：`migration_settings.auto_import_legacy`（默认开启）、`migration_settings.legacy_plugin_name`（默认 `astrbot_plugin_livingmemory`）。检测到新数据目录为空且旧目录存在时自动执行一次迁移，之后不再触发。
- `/anam vacuum`：裁剪写操作日志、清理图谱残留行并执行 SQLite VACUUM，回收磁盘空间，返回回收前后的体积对比。
- 写操作日志保留策略：`storage_maintenance.auto_prune_write_ops`（默认开启）、`write_ops_keep_days`（7 天）、`write_ops_keep_max`（2000 条）、`write_ops_failed_keep_days`（失败记录保留 30 天）。此前该表只增不减。
- 备份体积上限：`backup_settings.max_keep`（保留 2 份）、`backup_settings.max_total_size_mb`（总计 1024 MB）、`backup_settings.skip_if_larger_than_mb`（主库超过 512 MB 时跳过全量备份）。此前每日全量备份没有任何上限。
- 图谱残留清理开关 `storage_maintenance.graph_prune_orphans`（默认关闭）与每日 VACUUM 开关 `storage_maintenance.daily_vacuum`（默认关闭）。
- 关系条目配额 `graph_memory.max_edge_entries_per_memory`（默认 0 = 不限，保持原行为）。
- 记忆整理限流：`memory_consolidation.max_candidates`（500）、`max_group_size`（10）、`min_run_interval_hours`（6 小时）、`merge_input_char_budget`（12000 字符）。
- `/anam status` 增加图谱体积报告，直接显示节点 / 边 / 条目的行数（复用已有统计查询，不增加额外开销）。
- 插件 logo（`logo.png`，512×512）。生成脚本 `assets/logo/generate_logo.py` 一并入库，只依赖 numpy 与 Pillow，可复现。

### 修复

- **删除记忆会静默丢失其它记忆的图谱条目**（继承自上游，实测确认）。上游 `_add_edge` 会把语义相同的边跨记忆合并，复用第一条记忆名下的边行；而 `graph_entries.edge_id` 是 `ON DELETE CASCADE` 外键，删除记忆时按 `source_memory_id` 无条件删边，就会连带删掉其它记忆指向同一条边的条目。实测两条记忆各 12 条关系条目，删掉第一条后第二条只剩 9 条。现在四条删边路径（`delete_memory`、`batch_delete_memories`、以及残留清理的两步删边）统一加上「没有任何 `graph_entries.edge_id` 指向它」的前置条件，可以证明不会触发任何级联删除；被保护的边等最后一个引用者消失后由残留清理回收。记忆衰减与遗忘每次都会命中这条路径，长期运行的库受影响尤其明显。
- **JSON 修复逻辑会破坏本来合法的多行 JSON**：`MemoryProcessor._try_fix_json` 里的 `fixed.replace("\n", "\\n")` 会把 JSON 结构自身的换行一起转义，导致原本能解析的模型响应反而解析失败。已按字符串字面量内外分别处理，并拆成 7 个可单测的辅助方法。
- **`memory_processor` 缺少 `ast` 与 `json` 导入**：走到修复分支时会抛 `NameError`。
- **3 处 `except: pass` 静默吞掉异常**：补上 debug 日志，故障不再无痕消失。
- **`graph_edges.target_node_id` 缺少索引**：反向邻居查询需要全表扫描。新增 `idx_graph_edges_target`，旧库下次初始化时自动补建。
- **`package-lock.json` 锁定了 npmmirror 镜像地址**（178 处）：镜像不可达时前端依赖装不上，也无法用官方源校验完整性。已统一改回 `registry.npmjs.org`。

### 优化

- **图子系统体积治理**。宿主机实测：1766 条真实记忆，主库 216 MB 中记忆正文只占 12 MB（5.6%），图子系统占 233 MB / 289 MB 活跃数据（76%），平均每条记忆 48 条关系条目。根因是关系抽取的三重笛卡尔积（`topics × facts` + `participants × facts` + `C(participants, 2)`，单条记忆最坏 140 条）。`max_edge_entries_per_memory` 按置信度降序保留，天然先砍掉信息量最低的共现边；配合 `/anam rebuild-graph` 可以对存量数据生效。
- **删除记忆不再做全表扫描**。原实现每删一条记忆都对 `graph_nodes` 执行 `id NOT IN (SELECT ... UNION ...)` 的全表反连接；现在按索引收集候选节点集再判定孤儿，并以 500 行为单位分批执行。
- **新增图谱残留清理**。图结构写入分成节点、边、条目三个独立事务，中途失败会留下互不引用的残留行，FTS5 影子表也可能残留已删条目。清理按引用关系分五步（无条目的边 / 端点已消失的边 / 悬空关联行 / FTS 影子行 / 孤儿节点），每批先取 rowid 再按 rowid 删除、逐批提交并让出事件循环，峰值内存只有一批 rowid；单次总行数预算 200000，没清完时返回 `truncated=True`，下次维护继续。
- **记忆整理不再可能把内存打满**。候选集、分组大小、单次合并输入字符数都有上限，超大分组会被拆分而不是整组塞给模型；两次整理之间有最小间隔。
- **提示词文件按 mtime 缓存**，不再每次调用都读盘。
- **6 处热路径日志由 info 降为 debug**，减少高频磁盘写入与日志噪音。
- **移除 `networkx` 依赖**：图检索已完全由 SQLite 实现，不再需要这个包。
- 存储维护（写操作日志裁剪、图谱残留清理、FTS optimize、WAL checkpoint、可选 VACUUM）统一收敛到一个入口，返回结构化报告，任一步失败都会转成报告而不是中断整个维护任务。

