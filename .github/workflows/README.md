# GitHub Actions 工作流说明

本目录包含了项目的自动化工作流配置。

## 📋 工作流列表

### 1. 测试 (`test.yml`)

**触发条件**: 向 `master` 推送、提交目标为 `master` 的 Pull Request，或手动触发

**功能**:
- 检出官方 AstrBot 和当前 Anamnesis 版本，复现真实插件目录结构（`AstrBot/data/plugins/astrbot_plugin_anamnesis`）
- 使用 Python 3.12 安装 AstrBot 与插件依赖
- 运行 `python -m pytest -q` 完整单元与集成测试集
- 使用 Node.js 24 运行 `npm run test:frontend` 前端测试
- 同一分支有新提交时取消旧的测试任务

### 2. 自动发布 (`auto-release.yml`)

**触发条件**: 当 `metadata.yaml` 文件发生变化并推送到 `main` 或 `master` 分支时

**功能**:
- 对比 `HEAD~1` 与 `HEAD` 的 `metadata.yaml`，检测版本号是否变化
- 版本变化时，按 Conventional Commits 前缀（`feat`/`fix`/`docs`/`refactor` 等）对上一版本标签到 `HEAD` 之间的提交分类，生成带提交链接的 Release Notes
- 使用 `softprops/action-gh-release@v2` 创建 GitHub Release，tag 名即 `metadata.yaml` 中的版本号
- 版本号包含 `beta`/`alpha`/`rc`/`pre` 时自动标记为预发布
- 仅使用内置的 `GITHUB_TOKEN`，不需要额外配置 secret

**使用方法**:
1. 更新 `CHANGELOG.md` 中对应版本的更新内容
2. 更新 `metadata.yaml` 中的版本号
3. 提交并推送到主分支
4. 工作流会自动创建 Release

> Release Notes 由 Git 提交历史生成，`CHANGELOG.md` 面向人类阅读，两者需要各自维护。

**示例 CHANGELOG.md 格式**:
```markdown
## 3.0.0

### 新增
- 添加了新功能 A
- 支持功能 B

### 修复
- 修复了 Bug C
```

### 3. Issue 自动分类 (`auto-label-issues.yml`)

**触发条件**: 当有新 Issue 被创建或编辑时

**功能**:
- 根据标题前缀自动添加标签（如 `[Bug]`、`[功能]` 等）
- 根据内容关键词智能分类
- 自动检测优先级
- 识别适合新手的议题
- 添加欢迎评论和提示信息
- 打标签或评论失败时只记录 warning，不让工作流失败（例如仓库尚未创建对应标签）

**支持的分类标签**:
- `bug` - Bug 报告
- `enhancement` - 功能增强
- `documentation` - 文档相关
- `question` - 问题咨询
- `performance` - 性能问题
- `security` - 安全相关
- `ui` - 界面相关
- `api` - API 相关
- `database` - 数据库相关
- `memory` - 记忆功能相关
- `config` - 配置相关
- `priority: high/medium/low` - 优先级
- `needs more info` - 需要更多信息
- `good first issue` - 适合新手

### 4. 文档站部署 (`docs.yml`)

**触发条件**: 向 `main` 或 `master` 推送且改动了 `docs/**`、`package.json`、`package-lock.json` 或本工作流文件时，或手动触发

**功能**:
- `npm ci` 安装依赖后执行 `npm run docs:build` 构建 VitePress 站点
- 将 `docs/.vitepress/dist` 作为 Pages 产物上传并部署到 GitHub Pages
- `base` 默认为 `/astrbot_plugin_anamnesis/`，可用 `DOCS_BASE` 环境变量覆盖

**配置说明**:
- 需要在仓库 `Settings -> Pages` 中把 **Source** 设为 **GitHub Actions**，否则部署步骤会失败
- `docs/.vitepress/config.mts` 中 `ignoreDeadLinks: false`，站内死链会直接让构建失败

## 📝 Issue 模板

项目提供了以下 Issue 模板：

### Bug 报告 (`bug_report.yml`)
用于报告 Bug，包含：
- Bug 描述
- 复现步骤
- 预期行为 vs 实际行为
- 错误日志
- 环境信息（版本、操作系统等）

### 功能建议 (`feature_request.yml`)
用于提交新功能建议，包含：
- 功能描述
- 使用场景
- 优先级选择

### 问题咨询 (`question.yml`)
用于询问使用问题，包含：
- 问题描述
- 相关背景
- 配置信息
- 文档查阅情况

### 模板配置 (`config.yml`)
- 禁用空白 Issue
- 提供文档和讨论区链接（讨论区链接需要仓库开启 Discussions 功能）

## 🔧 维护建议

### 定期更新 CHANGELOG.md
1. 在开发过程中持续更新 `CHANGELOG.md`
2. 使用标准的 changelog 格式
3. 在发布新版本前，将 `[Unreleased]` 部分移动到新版本号下
4. 上游 `astrbot_plugin_livingmemory` 的历史记录保留在 `CHANGELOG_upstream.md`，不要往里追加新内容

### 监控工作流执行
1. 定期检查 Actions 标签页的工作流执行情况
2. 如果发现失败，及时查看日志并修复
3. 关注自动分类的准确性，必要时调整关键词

### 优化关键词库
根据实际使用情况，可以在 `auto-label-issues.yml` 中：
- 添加新的分类关键词
- 调整现有关键词的权重
- 增加新的标签类别

## 🚀 最佳实践

1. **版本发布流程**:
   ```bash
   # 1. 更新 CHANGELOG.md
   # 2. 更新 metadata.yaml 中的版本号和 core/managers/backup_manager.py 的 PLUGIN_VERSION
   # 3. 提交并推送
   git add CHANGELOG.md metadata.yaml core/managers/backup_manager.py
   git commit -m "chore: release v3.x.x"
   git push origin master
   ```

2. **Issue 管理**:
   - 鼓励用户使用模板提交 Issue
   - 定期审查带有 `needs more info` 标签的 Issue
   - 关注 `good first issue` 标签，引导新贡献者

3. **工作流调试**:
   - 使用 GitHub Actions 的日志功能
   - 本地测试工作流配置（使用 `act` 工具）
   - 在测试分支上验证修改

## 📚 参考资源

- [GitHub Actions 文档](https://docs.github.com/actions)
- [工作流语法](https://docs.github.com/actions/reference/workflow-syntax-for-github-actions)
- [Issue 表单语法](https://docs.github.com/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms)
