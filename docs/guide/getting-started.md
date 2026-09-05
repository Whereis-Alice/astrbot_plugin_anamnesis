# 快速开始

Anamnesis 是一个 AstrBot 长期记忆插件。它会在普通对话之外维护一套长期记忆库，让机器人能记住稳定偏好、长期项目、人物关系、群聊背景和历史约定。

## 安装

1. 将插件目录放到 AstrBot 的 `data/plugins/` 目录下。
2. 重启或重载 AstrBot。
3. AstrBot 会根据 `requirements.txt` 自动安装 Python 依赖。
4. 打开 AstrBot 插件配置页面，找到 `Anamnesis`。

## 从 LivingMemory 迁移

全新安装可以跳过本节。

Anamnesis 的数据目录是 `data/plugin_data/astrbot_plugin_anamnesis`，与旧插件 `astrbot_plugin_livingmemory` 分开存放，因此旧记忆不会被自动读取。迁移命令按下面的顺序执行：

| 步骤 | 命令 | 说明 |
| --- | --- | --- |
| 1 | — | 安装并启用 Anamnesis，旧插件先保持原样不要卸载 |
| 2 | `/anam migrate preview` | 列出将要迁移的文件与体积，只读报告，不写入任何数据 |
| 3 | `/anam migrate exec` | 真正执行迁移；旧 `backups/` 目录会被跳过以节省磁盘 |
| 4 | `/anam migrate-verify` | 逐表比对 documents、memory_atoms、graph_entries、graph_edges、graph_nodes、conversations 的行数是否与旧库一致 |
| 5 | — | 对账通过后，再在 AstrBot 中卸载旧插件 |

迁移全程只读取旧插件的数据目录，不会修改或删除旧插件的任何文件。因此在卸载旧插件之前，随时可以停用 Anamnesis 回退到旧插件。

迁移后如果想回收磁盘，可以执行 `/anam vacuum`，它会裁剪写操作日志并对数据库执行 SQLite VACUUM。

旧插件的命令组是 `/lmem`，Agent 工具名是 `recall_long_term_memory` 与 `memorize_long_term_memory`，记忆注入标记是 `<RAG-Faiss-Memory>`。这些标识符在 Anamnesis 中分别改为 `/anam`、`anamnesis_recall_memory` / `anamnesis_memorize_memory` 和 `<Anamnesis-Memory>`，按旧名称编写的人格提示词需要同步更新。

## 必需配置

| 配置项 | 作用 | 建议 |
| --- | --- | --- |
| `provider_settings.embedding_provider_id` | 生成记忆向量，用于语义检索 | 留空可使用 AstrBot 默认 Embedding |
| `provider_settings.llm_provider_id` | 总结对话、评估记忆 | 留空可使用默认 LLM，建议选择推理能力稳定的模型 |
| `bot_language` | 命令与状态回复语言 | `zh`、`en`、`ru` |

## 推荐配置

| 场景 | 建议 |
| --- | --- |
| 私聊助手 | 开启人格隔离与会话隔离，避免不同身份之间串记忆 |
| 群聊长期陪伴 | 开启 `enable_full_group_capture`，让插件捕获未直接 @Bot 的群聊上下文 |
| Agent / Tool Loop | 保持主动记忆工具开启，让模型在需要时自行回忆或写入 |
| Gemini Provider | 选择 `fake_tool_call` 时会自动降级到 `extra_user_content` |
| DeepSeek V4 thinking | 现在可以直接使用普通 `fake_tool_call`，旧的 `fake_tool_call_deepseek_v4` 仅作兼容别名 |

## 打开管理页面

AstrBot 版本建议为 `4.24.2` 或更高。进入：

`插件 -> Anamnesis -> Pages -> dashboard`

在这里可以查看记忆列表、调试召回、管理备份，并通过图谱视图观察实体关系。

## 验证是否工作

发送几轮对话后，可以使用：

```text
/anam status
/anam summarize
/anam search 你的关键词
```

如果能看到记忆数量和搜索结果，说明基础链路已经跑通。
