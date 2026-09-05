<div align="center">

<p><strong>中文</strong> &nbsp;/&nbsp; <a href="README_en.md">English</a> &nbsp;/&nbsp; <a href="README_ru.md">Русский</a></p>

<h1>Anamnesis</h1>

<p><strong>为 AstrBot 构建的长期记忆：精准召回，并在每次对话中持续演化。</strong></p>

<p><sub>捕获 &nbsp;&nbsp; 检索 &nbsp;&nbsp; 连接 &nbsp;&nbsp; 演化</sub></p>

<p>
  <a href="https://github.com/Whereis-Alice/astrbot_plugin_anamnesis/releases"><img src="https://img.shields.io/github/v/release/Whereis-Alice/astrbot_plugin_anamnesis?style=flat-square&color=5f7f79" alt="最新版本"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-e9f1ef?style=flat-square&labelColor=263a36" alt="Python 3.10 或更高版本">
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D%204.24.2-f3eee4?style=flat-square&labelColor=544c3d" alt="AstrBot 4.24.2 或更高版本">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-f2e8e5?style=flat-square&labelColor=5b403a" alt="AGPL-3.0 许可证"></a>
</p>

<img src="docs/public/images/retrieval-flow.svg" width="100%" alt="Anamnesis 双路检索流程">

</div>

> **关于本项目**：Anamnesis 3.0.0 是上游插件 [`astrbot_plugin_livingmemory`](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) v2.6.1 的独立重命名分支，由 Whereis-Alice 维护，与上游项目及原作者 lxfight 无隶属关系，也不由上游维护。派生与许可说明见 [NOTICE](NOTICE)，上游历史记录见 [CHANGELOG_upstream.md](CHANGELOG_upstream.md)。

## 让记忆形成结构

<table>
<tr>
<td width="33%"><strong>精准召回</strong><br><br>BM25 与向量检索同时覆盖文档路和图路，最终通过融合排序收敛为可靠结果。</td>
<td width="33%"><strong>动态上下文</strong><br><br>事实被拆分为独立记忆原子，分别拥有重要度、TTL、强化机制与时间衰减。</td>
<td width="33%"><strong>全量可见</strong><br><br>通过社区结构与多级细节，在高性能画布中探索完整的记忆关系图谱。</td>
</tr>
</table>

## 一套完整的记忆系统

| 召回 | 智能 | 控制 |
| :--- | :--- | :--- |
| **混合检索**<br>关键词与语义检索覆盖两条数据路径。 | **双通道总结**<br>事实信息与人格上下文保持独立价值。 | **安全操作**<br>自动备份、事务删除与重建失败回滚。 |
| **Agent 原生工具**<br>`anamnesis_recall_memory` 与 `anamnesis_memorize_memory`。 | **时间感知图谱**<br>关系置信度随证据累积或消退动态变化。 | **专注的管理界面**<br>管理记忆、调试召回并检查完整图谱。 |

## 近期能力

| 可恢复记忆 | 可控边界 | 在线维护 |
| :--- | :--- | :--- |
| **原文与归档**<br>重要记忆可保留来源消息，支持核验、重新总结；低价值记忆可归档并恢复，而非直接删除。 | **作用域与访问控制**<br>可按会话、用户或全局共享记忆，并通过强制隔离、白名单和身份别名明确边界。 | **安全索引重建**<br>启动检查和大规模修复在后台运行，使用分批重建、进度状态、失败回滚和影子索引切换。 |

```mermaid
flowchart LR
    A[对话] --> B[总结]
    B --> C[原子化与索引]
    C --> D[混合召回]
    D --> E[强化]
    C --> F[衰减或过期]
    E --> C
```

## 三步开始

1. 从 AstrBot 插件市场安装，或将插件放入 `data/plugins`。
2. 重载 AstrBot，进入 Anamnesis 配置页面。
3. 选择下方两个 Provider；其余配置均提供实用默认值。

| 配置项 | 用途 |
| :--- | :--- |
| `embedding_provider_id` | 嵌入模型；留空则使用 AstrBot 默认配置。 |
| `llm_provider_id` | 总结模型；留空则使用 AstrBot 默认配置。 |

可视化工作区入口为 `插件 -> Anamnesis -> Pages -> dashboard`。插件 Pages 需要 **AstrBot 4.24.2 或更高版本**。

## 深入了解

| 入门 | 配置 | 使用 | 原理 |
| :--- | :--- | :--- | :--- |
| [快速开始](docs/guide/getting-started.md)<br>[功能全览](docs/features.md) | [完整配置](docs/configuration.md) | [命令列表](docs/commands.md)<br>[WebUI 管理](docs/webui.md) | [技术架构](docs/architecture.md) |

## 从 LivingMemory 迁移

数据目录随插件更名而变化，旧插件的记忆不会被自动读取。请按顺序执行：

1. 安装并启用 Anamnesis，完成上面的 Provider 配置。
2. `/anam migrate preview` 列出将要迁移的文件与体积，不写入任何数据。
3. `/anam migrate exec` 执行迁移；旧 `backups/` 目录会被跳过以节省磁盘。
4. `/anam migrate-verify` 逐表比对 documents、memory_atoms、graph_entries、graph_edges、graph_nodes、conversations 的行数是否与旧库一致。
5. 确认无误后，再在 AstrBot 中卸载旧插件。

迁移只读取旧数据目录，不会修改或删除旧插件的任何数据；卸载旧插件之前随时可以安全回退。

## 项目

[完整文档](docs/) · [版本发布](https://github.com/Whereis-Alice/astrbot_plugin_anamnesis/releases) · [更新记录](CHANGELOG.md) · [上游历史](CHANGELOG_upstream.md) · [问题反馈](https://github.com/Whereis-Alice/astrbot_plugin_anamnesis/issues)

Anamnesis 使用 [AGPL-3.0 许可证](LICENSE)发布。
