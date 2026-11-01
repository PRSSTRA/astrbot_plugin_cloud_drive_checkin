# astrbot-plugin-helloworld

AstrBot 插件模板 / A template plugin for AstrBot plugin feature

> [!NOTE]
> This repo is just a template of [AstrBot](https://github.com/AstrBotDevs/AstrBot) Plugin.
> 
> [AstrBot](https://github.com/AstrBotDevs/AstrBot) is an agentic assistant for both personal and group conversations. It can be deployed across dozens of mainstream instant messaging platforms, including QQ, Telegram, Feishu, DingTalk, Slack, LINE, Discord, Matrix, etc. In addition, it provides a reliable and extensible conversational AI infrastructure for individuals, developers, and teams. Whether you need a personal AI companion, an intelligent customer support agent, an automation assistant, or an enterprise knowledge base, AstrBot enables you to quickly build AI applications directly within your existing messaging workflows.

# Supports

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)

## 提交与 PR 协作

### 安装本地钩子

克隆仓库后，在仓库根目录执行：

```bash
bash scripts/install-git-hooks.sh
```

安装脚本检查 Python 3、设置钩子执行权限并配置 `core.hooksPath`，也可从其他目录通过脚本路径调用。配置仅保存在本地仓库，linked worktree 共用；每个新克隆都要安装一次。`.omp/agents/` 仅供本地使用，不得入库；PR 检查会拒绝新增历史中跟踪该目录内容的提交。

本地钩子可以被 `git commit --no-verify` 跳过，也可以被本机修改，因此最终以 PR 必需检查为准。CI 能校验分支名、PR 标题和提交历史，但无法证明工作分支最初是否由 linked worktree 创建。

### 每个任务使用 linked worktree

以从 `dev/0.0.1` 开始任务 `chore/13-git-policy` 为例，在主工作目录执行：

```bash
git switch dev/0.0.1
git pull --ff-only
git worktree add -b chore/13-git-policy \
  ../astrbot_plugin_cloud_drive_checkin-git-policy dev/0.0.1
cd ../astrbot_plugin_cloud_drive_checkin-git-policy
```

后续编辑、提交和推送都在新 worktree 中完成。主工作目录用于更新长期分支和管理 worktree，不直接提交。普通任务分支使用 `feature|fix|hotfix|chore/<编号>-<小写描述>`；名称不符合规范时检查只警告，不阻断 PR。

### 提交信息

首行格式为 `类型(scope): 描述`。类型沿用 `功能|修复|文档|样式|重构|性能|测试|构建|持续集成|杂项|回滚|feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert`；scope 匹配 `^[a-z0-9][a-z0-9._/-]*$`。描述为 2–50 个字符，至少包含一个中文汉字，可以包含英文技术词、数字和标点；开头不能是空白或标点，结尾不能是空白或句号。

完整提交在首行后空一行，再写 1–2 句中文表述，每句以 `。`、`！` 或 `？` 结尾。正文可以包含 `linked worktree`、文件名等技术标识符。例如：

```bash
git add README.md .github/workflows/pr-check.yml \
  scripts/check-pr-policy.sh tests/test_pr_policy.py
git commit \
  -m '构建(hooks): 完善工作树与提交校验' \
  -m '补充 linked worktree、提交信息与 PR 检查说明。'
```

PR 标题只使用同一格式的首行，不附加正文。公共校验入口有三种模式：

```bash
bash scripts/check-pr-policy.sh title TITLE_FILE
bash scripts/check-pr-policy.sh commits BASE_SHA HEAD_SHA
bash scripts/check-pr-policy.sh branch BASE_BRANCH HEAD_BRANCH
```

`title` 读取标题文件并复用提交钩子的标题校验；`commits` 校验 `BASE_SHA..HEAD_SHA` 引入的每个完整提交，不检查 GitHub 生成的合成 merge commit；`branch` 校验 PR 的目标分支和来源分支。

### PR 与发布分支

普通任务推送后向对应版本开发分支发 PR：

```bash
git push -u origin chore/13-git-policy
```

`dev/*` 接受任务分支，但拒绝以 `master`、`main`、`develop` 或另一个 `dev/*` 作为普通任务来源。`master` 只接受 `release/*` 和 `hotfix/*`：版本发布分支从对应的版本开发分支（例如 `dev/0.0.1`）切出，再向 `master` 发 PR；紧急修复从发布主线切出 `hotfix/*`，合入 `master` 后同步到当前活跃开发线。发布完成后也要把主线结果同步到仍活跃的开发分支。

仓库只使用 squash 合并。合并负责人在 GitHub 最终确认 squash 提交信息，确保首行和 1–2 句正文都符合上述格式；不能直接采用不合规的默认 PR 正文。

以下设置不能由仓库内文件代替，需在 GitHub ruleset 和仓库合并设置中手动配置并核对：

- `master` 和 `dev/*` 必须通过 PR 合入；
- 必需状态检查名称为 `PR 标题规范` 和 `提交信息规范`；
- 禁止强制推送和删除受保护分支；
- 限制或清空规则绕过权限；
- 为必需状态检查启用“创建分支时不强制检查”，以便从已发布基线创建新的 `dev/*`；现有分支仍要求 PR 和必需检查；
- 仓库只开放 squash merge；
- 单人维护时必需审批数设为 0，由合并负责人自行复核，不配置无法满足的他人审批要求。

### 合并后清理

确认 PR 已 squash 合并且任务 worktree 没有未提交修改后，回到主工作目录执行：

```bash
git switch dev/0.0.1
git pull --ff-only
git worktree remove ../astrbot_plugin_cloud_drive_checkin-git-policy
git branch -D chore/13-git-policy
git fetch --prune
```

Squash 不保留工作分支与目标分支之间的祖先关系，所以清理已合并的本地任务分支需要 `-D`；执行前必须先确认 PR 已合并。
