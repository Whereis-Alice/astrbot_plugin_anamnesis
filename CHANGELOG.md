# Changelog

所有重要的更改都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并且遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

Anamnesis 由上游插件 `astrbot_plugin_livingmemory` v2.6.1 派生而来。3.0.0 之前的历史记录原样保留在 [CHANGELOG_upstream.md](CHANGELOG_upstream.md)，其中的命令名与标识符均为旧版，不适用于本插件。

## 3.2.0

### 新增

- **用户个人档案（Beta，默认关闭）**：独立于 Top-K 长期记忆检索，按平台与发送者隔离，只从本人原话抽取明确事实；稳定事实始终可在检索超时或关闭时临时注入，临时状态有有效期。支持事实纠错覆盖、`/anam profile` 查看和 `/anam profile-clear [key]` 删除。对应上游需求 [#276](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/issues/276)。
- **Agent 删除记忆工具（默认关闭）**：`anamnesis_forget_memory` 仅接受准确 ID，校验白名单、记忆作用域与人格；`/anam forget` 与工具删除后会清除来源于该记忆的档案条目。

### 优化

- 对比上游近期变更后，保留已有的异步 FAISS 持久化、表达式索引、多行 JSON 修复与提示词缓存；补强适合本分支的 Mixin 宿主接口契约。

## 3.1.1

### 修复

- **SQLite `database is locked`**：记忆存储连接统一启用 WAL、忙等待和有界指数退避重试；访问时间更新改用独立连接，后台更新失败不会回滚或提交正在进行的多步记忆写入。
- **自动召回超时不可配置**：新增 `recall_engine.search_timeout_seconds`，默认 5 秒，可在 AstrBot 配置页调整为 `0–600` 秒；设为 `0` 表示不限时，超时只跳过当前轮记忆注入。

### 优化

- 新增 `storage_maintenance.sqlite_busy_timeout_seconds`、`sqlite_lock_retries` 和 `sqlite_lock_retry_delay_seconds` 高级调优项，默认值适合多数单机部署。
- 补齐配置校验、三语 WebUI 文案、配置文档与 SQLite 锁竞争回归测试。

## 3.1.0

本次更新只做一件事：**让「谁做了什么」记得住、也记得对**。此前人物完全依赖群昵称识别，改名即失联，撞名即串档，Bot 自称还会被当成群友写进记忆。没有破坏性变更，旧库直接可用。

### 新增

- **人物身份锚定**。抽取出的 `key_facts` 里的人名会被改写成 `昵称#账号尾号`（格式由 `identity_settings.anchor_format` 控制，默认 `{name}#{tail}`，尾号长度 `anchor_tail_length` 默认 4 位）。同一窗口内撞名、Bot、昵称不稳定、单字名、以及已经带 `#` 的名字一律跳过，只在真正可能混淆时才加后缀。锚定只作用于 `key_facts`，`summary` 保持自然语言，可读性不受影响。
- **昵称改名史（`person_aliases` 表）**。每个账号用过的全部昵称都会被记录，检索时自动把旧昵称展开成同义词（`alias_query_expansion`，默认开启），用三个月前的旧名字也能搜到人。单账号上限 `alias_max_per_identity`（默认 40），首次启动会从既有记忆回填一次（`alias_backfill_on_start`）。
- **身份卫士**。写入前拦掉三类脏数据：缺 `sender_id` 的匿名条目、把 `session_id` 当成人名的回音、以及 Bot 自称的各种变体（按多数票归一到同一个名字）。同一账号在 `name_stability_window_hours`（默认 24 小时）内出现超过 `max_distinct_names_per_identity`（默认 12）个不同昵称时标记为不稳定，不再参与锚定。
- **`/anam identity`**（管理员，只读）：身份体检报告，一次性给出锚定、身份卫士、别名库三部分的运行统计与生效配置，不写任何数据。
- **`/anam fix-identity [preview|exec|rollback] [platform]`**（管理员）：诊断并修复历史上被错记到群友名下的 Bot 发言。`preview` 只报告（默认），`exec` 执行修复并写行级撤销日志，`rollback` 完整还原上一次修复。判定策略保守：优先采信当前在线适配器上报的 Bot 账号，其次才用统计众数，且众数占比需超过 50%；合成的 `bot:` id 不采信；平台归属本身存在歧义时整个平台跳过。修复只改会话日志里的发言人归属，不动记忆正文与图谱。
- **`identity_settings` 配置段（12 项）**，详见 [docs/configuration.md](docs/configuration.md)。其中只有 `alias_cache_max`（默认 2000）与 `identity_guard_max_tracked`（默认 512）会决定常驻内存，1 GB 小机建议分别调到 500 与 128。

### 修复

- **人物节点只记得最后一次用过的昵称**。`_upsert_node` 原来是 `metadata = excluded.metadata`，每写一次就把整份 metadata 覆盖掉，同一个人的历史昵称全部丢失——结果是用旧名字既搜不到人，也看不出这个人曾经叫过什么。现在 `node_type == "person"` 改走 `_merge_person_metadata()`：新昵称优先、去重、按 `alias_max_per_identity` 截断，历史昵称完整保留。
- **一条坏数据能把群友永久误标成 Bot**。`is_bot` 在查询侧被 `MAX()` 聚合，任何一次错误标记都会永久粘住，之后这个账号的发言都会被当成 Bot 输出过滤掉。现在写入侧 `is_bot` 改为粘性 OR（某次缺标记不会把已知 Bot 降级成人），已经产生的错误标记由 `/anam fix-identity exec` 统一清理，别名文本本身保留不动。
- **群聊提示词没有约束昵称的抄写方式**，模型会自行改写或简化人名（去掉后缀、合并近似名），同一个人在不同记忆里名字不一致。现在提示词明确要求逐字照抄前缀原文；同一账号出现多个昵称时取最后一条；不同账号撞名时补账号后 4 位。

### 优化

- **图谱抽取能认出改过名的人**。抽取前会把别名库里的全量改名史折进参与者身份（浅拷贝，不污染已持久化的 metadata），旧名字与新名字因此会被归到同一个人身上。
- **检索接入别名扩展**。BM25 与图谱关键词两条通路都会展开旧昵称，单次查询最多 12 个同义词，避免关键词爆炸拖慢检索。
- **新增结构全部有内存上限**。别名库是内存快照 + TTL（`alias_cache_max` / `alias_cache_ttl_seconds`），身份卫士的跟踪表按 LRU 淘汰（`identity_guard_max_tracked`），长期运行不会无边界增长。
- 新增 43 个测试，覆盖别名库、身份卫士、锚定、人物节点昵称累积与归属修复（含撤销），全量测试 1032 项。

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
