# LivingMemory 上游更新评估（2026-10-25）

此次比较以 Anamnesis 3.2.4 为基线，参考 LivingMemory **2.7.1**，固定到提交 [`b8aaaec2c1232ec2c86fce22cec066a0069018e0`](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/b8aaaec2c1232ec2c86fce22cec066a0069018e0)。已适配的内容发布为 Anamnesis **3.3.0**。上游与本分支已存在身份锚定、用户档案、来源保存和写入补偿等差异，逐项适配更便于保留这些能力。

## 本次采纳

| 上游方向 | 本分支的适配 |
| --- | --- |
| [中文字符二元组](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/91d2634) | 在连续中文段补相邻双字词，不跨标点、英文或数字；新增、编辑、查询、重建都采用同一规则，保留昵称别名展开 |
| [分词缓存](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/9d8c9fa) | 缓存短文本分词，最多 128 项；词典和停用词变化时失效，返回副本以免调用方污染缓存 |
| [整合隔离与回滚](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/a0ce689) | 时间与语义整合都按 `(session_id, persona_id)` 拆组；直接调用同样检查，结果保留作用域。部分来源删除时保留合并副本，避免损失唯一副本 |
| [历史消息排序](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/a9eff4d) | 同时间戳用消息 ID 作第二排序键，正向与倒序一致 |
| [衰减与任务收尾](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/709a236) | 字段级 SQL 保留并发元数据；整批失败回滚且不记录完成日期；取消后先结束事务并记录已成功的衰减。后台异常可见，存储任务限时等待后取消 |
| [迁移备份](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/31a6249) | 默认备份失败即中止；迁移失败用 SQLite backup API 尝试恢复，随后中止启动；尊重关闭迁移备份的配置 |
| [流式 LLM](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/db19cb7) | 增加 `llm_stream_mode`；完整流结束且最终响应有效才接受，自动模式仅成功后记住流式 Provider，普通错误不转流式 |
| [注入预算](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/de8f31f) | 总量与单条上限可配置；保留文本边界与伪工具 JSON 结构，数据库原文和独立用户档案不受展示裁剪影响 |
| [FAISS 中文路径](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/9801a3b) | 延迟加载并统一初始化、保存、重建的路径桥接；ASCII 临时文件中转，目标目录暂存后原子替换，跨盘失败不破坏旧索引 |
| [文档依赖更新](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/de4578f) | 更新 source-map-js 与 Vue 补丁；锁文件统一官方 npm 源 |
| [Embedding 配置说明](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/9c77431) | 明确建议固定 Provider；准确说明留空使用首个可用模型，兼容既有空配置 |

## 值得继续适配

| 方向 | 暂缓原因与后续适配要点 |
| --- | --- |
| [写入时近重复合并](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/079026a) | 能减少同一事件重复记忆。需要同时处理原始来源、用户档案归属和失败补偿，保证合并与显式删除后的档案清理正确 |
| 候选扩大与分数融合 | 有助于稀疏关键词查询。需与本分支的别名展开、近期记忆保底、图路检索及 Rerank 一起评估，防止召回噪声和排序退化 |
| 共享 Embedding 与 FAISS 读写锁 | 有节省资源与提高并发一致性的价值。需覆盖主索引、图索引、异步持久化和重建，单点替换会遗漏并发路径 |
| 过期原子的最终复检 | 有助于减少过时事实。必须同步处理展示优先使用的 `persona_summary` 与 `key_facts`，仅替换检索正文仍可能注入旧事实 |
| [历史编辑与 regenerate 对账](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/3afdd2d) | 需要引入修订号、来源追踪和取消补偿，改动跨多个生命周期，适合单独实现并验证 |
| [独立设置页](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/dd2ecfb) | 分类、搜索与折叠交互值得参考。迁移前需确保配置校验、国际化、保存和宿主配置页入口一致；本次继续提供 AstrBot 配置页 |

## 继续保留的本分支能力

保留身份锚定、昵称别名、独立用户档案及 WebUI 编辑、显式删除后的档案清理、写操作日志与恢复链、原始来源保存、已有 SQLite 锁重试和异步索引保存。上游删除写操作日志的简化方式暂不适用。本次没有改变用户档案的作用域与分组展示。

## 升级与验证

无需手动迁移数据库，新增配置有默认值。流式默认 `auto`；自动召回注入默认总量 6000 字符、单条 1200 字符，设为 `0` 可取消对应上限。参数见[配置参考](/configuration)。

旧 BM25 索引不会自动批量重写；如希望立即应用中文双字词规则，可手动运行 `/anam rebuild-index`。该命令会调用 Embedding Provider，执行前请考虑库大小和调用成本。

回归覆盖中文检索各写入路径、缓存失效、跨作用域整合、部分删除补偿、真实 SQLite 事务失败与取消、迁移恢复、流式完整性、JSON/文本预算及真实 FAISS 中文路径读写；同时验证完整 Python 测试、既有前端测试与文档构建。具体执行结果记录在本次提交说明中。
