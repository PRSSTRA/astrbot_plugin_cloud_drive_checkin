# 贡献指南

本文记录开发环境、测试方式、状态存储接口和仓库协作规则。面向用户的功能、安装和使用说明见 [README.md](README.md)；长期工程边界和安全约束见 [AGENTS.md](AGENTS.md)。

## 开发环境

插件声明支持 AstrBot `>=4.29.4,<5`，接口按 v4.29.4 核验。该版本 AstrBot 要求 Python 3.12 或更高版本，插件开发和离线测试使用同一 Python 版本要求。

以下命令建立独立测试环境，不安装完整 AstrBot：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

测试使用真实 croniter、cryptography 和受控的 KV、AstrBot 边界替身，不需要云盘账号。凭据和录入回归测试在 `tests/test_credentials.py`、`tests/test_enrollment.py`。测试不能替代完整 WebUI、真实 QQ、NapCat、Neo 沙箱或实际签到验证。

## 代码结构

- `main.py`：Star 生命周期、指令入口、帮助、任务查询和本人状态回复。
- `permissions.py`：私聊事件校验、身份提取和权限判定。
- `config.py`：权限配置的解析和 QQ 号规范化。
- `task_config.py`：任务目录校验、Cron 计算和配置版本。
- `enrollment.py`：5 分钟私聊录入会话、原始文本读取、超时和待录入意图。
- `credentials.py`：主密钥读取、认证加密、绑定键和凭据原子保存。
- `bindings.py`：本人绑定偏好更新、删除确认指纹和绑定与凭据的原子删除。
- `state.py`：快照结构和 JSON 校验。
- `storage.py`：基于 Star 异步 KV 的事务写入。

向导字段原文只保存在进程内会话中，持久化前加密。明文凭据不得进入日志、回执、shell 或命令行参数。

修改公共行为时同步更新 README 或 `docs/configuration.md`。任务字段、权限规则或指令输出变化会影响用户，不能只改代码。

## 状态存储

`state.py` 定义快照格式，`storage.py` 通过 `get_kv_data` 和 `put_kv_data` 操作唯一业务键 `checkin_state`。插件不创建数据库或状态文件。

快照包含 `schema_version`、`revision`，以及 `users`、`bindings`、`credentials`、`batches`、`runs`、`outbox`、`cleanup_jobs`、`cooldowns`、`dedupe` 集合。只接受 schema 版本 1 和合法 JSON 数据，默认序列化上限为 8 MiB。未知版本、结构损坏或超限都会报错，不会重置或裁剪记录。

集合内业务记录由对应业务操作定义。状态层不执行入队、历史保留策略或凭据加密；调用方不得写入明文凭据。`metadata.yaml` 的 author 和 name 决定框架 KV 命名空间，开始保存业务数据后必须保持稳定。

插件通过 `state_store` 暴露以下接口：

| 接口 | 行为 |
| --- | --- |
| `await initialize()` | 键不存在才创建新快照；读取、校验并重新保存完整快照后才能使用；已有待确认值时只重试原值 |
| `await mutate(callback)` | 在单锁内复制成功快照，运行同步修改函数，校验后提交；成功才更新内存并增加 revision |
| `snapshot()` | 返回最后已确认快照的深拷贝；尚无成功提交时拒绝读取 |
| `await retry_pending()` | 原样重试待确认快照，不读回确认、不增加 revision、不写旧快照回滚 |
| `stop_accepting()` | 立即拒绝新操作，已开始的提交仍需完成 |
| `await close()` | 停止接单并等待当前操作；有待确认写入时重试原值，成功后才关闭 |

写入默认超时为 30 秒。异常、超时或取消都保留待确认快照，`ready` 变为 false；新修改被拒绝，旧成功快照仍可读取。取消不保证框架队列中的写入没有完成，因此读取缓存不能替代写入确认。修改函数不得改动 `schema_version` 或 `revision`，也不得执行异步操作、网络请求或脚本。

同一 Context 上按 `plugin_id` 保存进程内写者登记，避免模块重载后丢失交接信息。活跃旧写者会阻止新实例；关闭失败则保留原写者与待确认快照，确认关闭后才允许接管。登记只保存运行时锁和对象引用，业务持久化仍只有 KV。仅支持单进程、单实例写入。

## 测试与验证

提交前至少运行上面的离线测试。涉及框架接口、OneBot 事件、KV 写入确认或调度时间的改动，需要补充对应的回归测试；不要用占位实现、吞异常或弱化断言通过测试。

PR 的“提交信息规范”任务会在 Python 3.12 上安装 `requirements.txt` 并运行 `unittest discover -s tests -p 'test_*.py'`。本地通过不代表真实 QQ、Neo 或云盘签到已经验证，集成后的行为由维护者验证并记录。

## Git hooks

克隆仓库后，在仓库根目录执行：

```bash
bash scripts/install-git-hooks.sh
```

安装脚本检查 Python 3，设置钩子执行权限并配置 `core.hooksPath=.githooks`，也可从其他目录通过脚本路径调用。配置只保存在本地仓库，linked worktree 共用，每个新克隆都要安装一次。

`pre-commit` 拒绝在 detached HEAD、主工作目录、`master`、`main`、`develop` 或 `dev/*` 上直接提交，也拒绝暂存 `.omp/agents/` 下的文件。`commit-msg` 校验提交信息格式。

本地钩子可以用 `git commit --no-verify` 跳过，也可以被本机修改，因此最终以 PR 必需检查为准。CI 能校验分支名、PR 标题和提交历史，但无法证明工作分支最初是否由 linked worktree 创建。

## 分支与 worktree

每项改动从指定的 `dev/*` 分支创建独立 worktree 和工作分支。以从 `dev/0.0.1` 开始任务 `chore/13-git-policy` 为例，在主工作目录执行：

```bash
git switch dev/0.0.1
git pull --ff-only
git worktree add -b chore/13-git-policy \
  ../astrbot_plugin_cloud_drive_checkin-git-policy dev/0.0.1
cd ../astrbot_plugin_cloud_drive_checkin-git-policy
```

后续编辑、提交和推送都在新 worktree 中完成。主工作目录用于更新长期分支和管理 worktree，不直接提交。

普通任务分支使用 `feature|fix|hotfix|chore/<编号>-<小写描述>`，完整正则为 `^(feature|fix|hotfix|chore)/[0-9]+-[a-z0-9-]+$`。`release/*` 不检查这一格式。普通任务分支不符合规范时，钩子和 PR 检查只警告，不阻断。

`.omp/agents/` 仅供本地使用，不得入库。PR 检查会拒绝新增历史中跟踪该目录内容的提交。

## 提交信息

首行格式为 `类型(scope): 描述`。允许的类型：

```text
功能 修复 文档 样式 重构 性能 测试 构建 持续集成 杂项 回滚
feat fix docs style refactor perf test build ci chore revert
```

`scope` 必须匹配 `^[a-z0-9][a-z0-9._/-]*$`。描述长度为 2 到 50 个字符，至少包含一个中文汉字，可以包含英文技术词、数字和标点；开头不能是空白或标点，结尾不能是空白或句号。

完整提交在首行后空一行，再写 1 到 2 句中文，每句必须包含中文汉字并以 `。`、`！` 或 `？` 结尾。正文可以包含 `linked worktree`、文件名等技术标识符。示例：

```bash
git add README.md CONTRIBUTING.md AGENTS.md docs/configuration.md
git commit \
  -m '文档(readme): 拆分用户说明与贡献指南' \
  -m '补充安装、指令和权限说明，并把开发流程迁到贡献指南。'
```

PR 标题只使用同一格式的首行。普通工作提交无需提前带编号；squash 合并提交必须在末尾追加半角 ` (#PR编号)`，编号不计入描述的 2 到 50 字符限制。

## PR 与合并

公共校验入口有三种模式：

```bash
bash scripts/check-pr-policy.sh title TITLE_FILE
bash scripts/check-pr-policy.sh commits BASE_SHA HEAD_SHA
bash scripts/check-pr-policy.sh branch BASE_BRANCH HEAD_BRANCH
```

`title` 读取标题文件并复用提交钩子的标题校验；`commits` 校验 `BASE_SHA..HEAD_SHA` 引入的每个完整提交，不检查 GitHub 生成的合成 merge commit；`branch` 校验目标分支和来源分支。

普通任务推送后向对应版本开发分支发 PR：

```bash
git push -u origin chore/13-git-policy
```

`dev/*` 接受任务分支，但拒绝以 `master`、`main`、`develop` 或另一个 `dev/*` 作为普通任务来源。`master` 只接受 `release/*` 和 `hotfix/*`。版本发布分支从对应的版本开发分支切出，再向 `master` 发 PR；紧急修复从发布主线切出 `hotfix/*`，合入 `master` 后同步到当前活跃开发线。发布完成后也要把主线结果同步到仍活跃的开发分支。

仓库只使用 squash 合并。合并标题为 `类型(scope): 描述 (#PR编号)`，例如 `功能(permissions): 增加普通私聊权限与帮助入口 (#12)`。保留原来的类型和 scope，编号使用半角括号。普通工作分支的提交和 PR 标题无需提前添加编号。

合并负责人须核对最终标题后缀及 1 到 2 句正文。使用 `gh pr merge --subject` 自定义标题时也必须显式包含 ` (#PR编号)`，不能用不合规的 PR 描述替代提交正文。

以下设置需要在 GitHub ruleset 和仓库合并设置中手动配置并核对：

- `master` 和 `dev/*` 必须通过 PR 合入。
- 必需状态检查名称为 `PR 标题规范` 和 `提交信息规范`。
- 禁止强制推送和删除受保护分支。
- 限制或清空规则绕过权限。
- 为必需状态检查启用“创建分支时不强制检查”，以便从已发布基线创建新的 `dev/*`；现有分支仍要求 PR 和必需检查。
- 仓库只开放 squash merge。
- 单人维护时必需审批数设为 0，由合并负责人自行复核。

## 合并后清理

确认 PR 已 squash 合并且任务 worktree 没有未提交修改后，回到主工作目录执行：

```bash
git switch dev/0.0.1
git pull --ff-only
git worktree remove ../astrbot_plugin_cloud_drive_checkin-git-policy
git branch -D chore/13-git-policy
git fetch --prune
```

Squash 不保留工作分支与目标分支之间的祖先关系，所以清理已合并的本地任务分支需要 `-D`。执行前必须先确认 PR 已合并。
