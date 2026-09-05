# 命令速查

Anamnesis 的命令统一使用 `/anam` 前缀（别名 `/amem`）。

| 命令 | 说明 |
| --- | --- |
| `/anam status` | 查看记忆库统计；索引维护进行中或异常时同时显示状态、进度和消息 |
| `/anam search <query> [k]` | 搜索长期记忆，`k` 默认为 5 |
| `/anam forget <id>` | 删除指定记忆 |
| `/anam rebuild-index` | 重建文档索引 |
| `/anam rebuild-graph` | 重建并压缩图谱索引，迁移到记忆级图向量 |
| `/anam webui` | 查看 WebUI 入口信息 |
| `/anam summarize [message_count]` | 立即总结当前会话；指定条数时重新总结最近 N 条消息 |
| `/anam reset` | 重置当前会话记忆上下文 |
| `/anam cleanup [preview\|exec]` | 清理历史消息中的旧记忆注入片段 |
| `/anam migrate [preview\|exec]` | 从旧插件 `astrbot_plugin_livingmemory` 的数据目录只读迁移全部数据；默认 `preview` 只报告清单，`exec` 才真正执行 |
| `/anam migrate-verify` | 迁移后对账，逐表比对行数是否与旧库一致 |
| `/anam vacuum` | 裁剪写操作日志并执行 SQLite VACUUM，回收磁盘空间 |
| `/anam identity` | 查看昵称锚定、身份守卫与 Bot 归属体检报告 |
| `/anam fix-identity [preview\|exec\|rollback] [platform]` | 修复会话库中被错误记到群友名下的 Bot 消息；默认 `preview` 只报告 |
| `/anam help` | 显示帮助 |

## 数据迁移与磁盘回收

从旧插件 LivingMemory 迁移的完整步骤见[快速开始](/guide/getting-started)。

| 命令 | 行为 |
| --- | --- |
| `/anam migrate preview` | 报告将要迁移的文件与体积，不写入任何数据 |
| `/anam migrate exec` | 实际执行迁移，跳过旧 `backups/` 目录以节省磁盘 |
| `/anam migrate-verify` | 逐表比对 documents / memory_atoms / graph_entries / graph_edges / graph_nodes / conversations 的行数 |
| `/anam vacuum` | 裁剪写操作日志并执行 SQLite VACUUM，回收数据库文件占用的磁盘 |

迁移只读取旧数据目录，不会修改或删除旧插件的数据；`/anam migrate-verify` 通过后再卸载旧插件即可。

## 身份体检与归属修复

昵称既不唯一也不稳定，所以人物节点按账号 ID 归档，昵称只是别名。`/anam identity` 把这套机制的运行情况一次性列出来。

| 命令 | 行为 |
| --- | --- |
| `/anam identity` | 只读体检：昵称锚定配置、身份守卫拦截统计、别名库规模，以及会话库里每个平台的 Bot 归属分布 |
| `/anam fix-identity preview` | 报告有多少条 `assistant` 消息被记到了错误账号名下，不写入任何数据 |
| `/anam fix-identity exec` | 实际改写这些消息的发送者，并清理别名库里被误标的 `is_bot` 标记 |
| `/anam fix-identity rollback` | 按行级撤销日志还原上一次 `exec` 的改写 |
| `/anam fix-identity exec aiocqhttp` | 只处理指定平台，其他平台原样保留 |

`preview` / `exec` / `rollback` 都接受常见同义词（`dry-run`、`apply`、`undo` 等）。

修复逻辑刻意保守：真正的 Bot 账号优先取自当前平台适配器；取不到时才回退到统计众数，且只有在某个账号占据该平台 `assistant` 消息的**绝对多数**时才认定。比例不过半的平台会被标记为 `ambiguous` 并跳过，不会靠掷硬币「修复」。

::: warning 只改会话库
`fix-identity` 只改写会话消息表，不动已抽取的记忆和图谱节点——重跑抽取的代价过高，历史误归属会随记忆衰减自然淘汰。回滚也不恢复 `is_bot` 标记，那只是展示用元数据，会从后续流量重新学习。
:::

## 索引维护状态

启动一致性检查和自动修复在后台运行。使用 `/anam status` 观察非空闲状态：

| 状态 | 含义 | 建议操作 |
| --- | --- | --- |
| `checking` | 正在检查文档、向量、BM25 和图谱一致性 | 无需操作，插件仍可提供服务 |
| `rebuilding` | 正在分批重建并显示当前进度 | 保持 Provider 可用，不要重复启动重建 |
| `partial` | 重建完成，但存在允许范围内的失败或最终检查仍发现差异 | 检查 Provider 限流和日志，必要时重新执行 `/anam rebuild-index` |
| `failed` | 后台维护失败；影子代际未切换时会保留原线上索引 | 根据状态消息和日志修复环境后重新执行重建 |
| `cancelled` | 插件停止或重载时维护任务被取消 | 插件下次启动会重新检查；也可在稳定后手动重建 |

`idle` 和 `ready` 不会额外显示维护段落。

## 常用排查

| 现象 | 建议 |
| --- | --- |
| 搜不到刚聊过的内容 | 先执行 `/anam summarize`，确认对话已经写入长期记忆 |
| 总结为空或遗漏细节 | 使用 `/anam summarize 20` 重新总结最近 20 条消息，或在详情中对已保留原文重新总结 |
| 记忆明显串到其他人格 | 检查 `filtering_settings.use_persona_filtering` 是否开启 |
| 别人做的事被记成我做的 | 群友改名或撞名会让「只按昵称匹配」失效；先用 `/anam identity` 确认昵称锚定已开启，再用 `/anam fix-identity preview` 查看 Bot 归属 |
| 群聊上下文不完整 | 检查 `session_manager.enable_full_group_capture` 是否开启 |
| 索引疑似异常 | 执行 `/anam rebuild-index`，图谱异常则执行 `/anam rebuild-graph` |
| 更换 Embedding 模型后旧记忆召回异常 | 等待后台 Provider 指纹检查触发完整向量重建，并用 `/anam status` 查看进度 |
| 报错显示 `faiss-cpu 1.14.2` 或核心依赖冲突 | AstrBot 4.27.1 与插件要求 `faiss-cpu>=1.14.3`；更新或修复 Desktop 内置环境和依赖锁，不要让插件覆盖核心依赖 |
| FAISS 报 `Illegal instruction` | 插件会先尝试 generic 指令集模式；仍失败时需要安装与当前 CPU 和 Python 匹配的 FAISS wheel，或更换运行环境 |
| FAISS 报 `SuperKMeans` 未定义 | Python 封装与二进制扩展不匹配；在 AstrBot 实际使用的同一 Python 环境中干净重装兼容版本 |
