# AstrBot 云盘签到插件

业务状态通过 Star 异步 KV 保存。插件提供受权限控制的私聊帮助，加载时确认状态快照，卸载时停止写入；不接收凭据、不启动签到、定时任务或 Neo 资源。

## 快速验证

使用 Python 3.10 或更高版本，在仓库根目录执行：

```bash
python3 -m unittest discover -s tests
```

测试使用标准库及受控的 KV、AstrBot 边界替身，不需要云盘账号。框架接口按 AstrBot v4.29.4 源码核对；实际部署仍需单独验证加载、重载和持久化行为。

## 私聊帮助与授权

在 AstrBot 插件配置页维护以下字段，QQ 号填写为正整数或不带空白、符号及前导零的数字字符串：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `false` | 签到服务开关；关闭时仍可查看帮助，开启也不会让当前版本执行签到脚本 |
| `plugin_admin_qqs` | `[]` | 附加插件管理员 QQ；AstrBot 管理员默认拥有管理员权限 |
| `whitelist_qqs` | `[]` | 普通用户白名单；空列表不放行普通用户，管理员无需重复加入 |

按 AstrBot 当前唤醒前缀发送指令。例如前缀为 `/` 时，使用 `/签到 帮助`，或直接 `/签到`。授权用户可查看自己的权限类别、服务开关及状态存储是否就绪；其他子命令仅返回固定用法，不回显输入。

仅接受 `aiocqhttp` 的普通 QQ 私聊。群聊、临时会话、其他平台及身份字段矛盾的事件一律静默拒绝，管理员也不豁免；合法私聊中的未授权用户会收到无权限提示。命中指令后关闭默认 LLM 并停止后续事件传播，帮助和拒绝均不写 KV、不修改配置。

每次处理指令都重新读取权限配置，撤销白名单立即生效；运行时配置损坏时拒绝处理并返回安全提示。授权 QQ 列表适用于各合法 OneBot 实例，但用户身份始终保留平台实例 ID，不将不同实例的数据合并。

## 状态存储

`state.py` 定义快照格式，`storage.py` 通过 `get_kv_data` 和 `put_kv_data` 操作唯一业务键 `checkin_state`，不创建数据库或状态文件。

- 快照含 `schema_version`、`revision`，以及 `users`、`bindings`、`credentials`、`batches`、`runs`、`outbox`、`cleanup_jobs`、`cooldowns`、`dedupe` 集合。
- 只接受版本 1 和合法 JSON 数据，默认 UTF-8 序列化上限为 8 MiB。未知版本、结构损坏或超限均报错，不重置、不裁剪记录。
- 集合内业务记录由对应业务操作定义；本层不执行入队、历史保留策略或凭据加密。调用方不得写入明文凭据。
- `metadata.yaml` 的 author/name 决定框架 KV 命名空间，开始保存业务数据后应保持稳定。

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

## 开发资料

- [AstrBot 仓库](https://github.com/AstrBotDevs/AstrBot)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

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
