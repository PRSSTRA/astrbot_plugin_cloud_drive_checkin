# AstrBot 云盘签到插件

业务状态通过 Star 异步 KV 保存。插件提供受权限控制的私聊帮助、任务列表与计划预览，加载时确认状态快照，卸载时停止写入；不接收凭据、不启动签到、定时任务或 Neo 资源。

## 快速验证

插件声明支持 AstrBot `>=4.29.4,<5`，接口按 v4.29.4 核验，该版本宿主要求 Python 3.12 或更高版本。离线插件测试支持 Python 3.10 或更高版本；以下命令只建立独立测试环境，不安装完整 AstrBot：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

测试使用真实 croniter 及受控的 KV、AstrBot 边界替身，不需要云盘账号。宿主依赖锁定在 `requirements.txt`，不包含签到脚本依赖。框架接口按 AstrBot v4.29.4 源码核对；已执行其原始配置默认值函数及 CommandFilter 的离线 smoke，未验证完整 WebUI、真实 QQ、Neo 或账号签到。

## 私聊帮助与授权

在 AstrBot 插件配置页维护以下字段，QQ 号填写为正整数或不带空白、符号及前导零的数字字符串：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `false` | 签到服务开关；关闭时仍可查看帮助，开启也不会让当前版本执行签到脚本 |
| `plugin_admin_qqs` | `[]` | 附加插件管理员 QQ；AstrBot 管理员默认拥有管理员权限 |
| `whitelist_qqs` | `[]` | 普通用户白名单；空列表不放行普通用户，管理员无需重复加入 |

按 AstrBot 当前唤醒前缀发送指令。例如前缀为 `/` 时，使用 `/签到 帮助`，或直接 `/签到`。授权用户可查看自己的权限类别、服务开关及状态存储是否就绪；`/签到 任务` 查看任务列表，`/签到 任务 tianyi_cloud` 查看字段要求、时区和未来三次计划时间。未知指令、未知任务或多余参数不回显输入。

仅接受 `aiocqhttp` 的普通 QQ 私聊。群聊、临时会话、其他平台及身份字段矛盾的事件一律静默拒绝，管理员也不豁免；合法私聊中的未授权用户会收到无权限提示。命中指令后关闭默认 LLM 并停止后续事件传播，帮助和拒绝均不写 KV、不修改配置。

每次处理指令都重新读取权限配置，撤销白名单立即生效；运行时配置损坏时拒绝处理并返回安全提示。授权 QQ 列表适用于各合法 OneBot 实例，但用户身份始终保留平台实例 ID，不将不同实例的数据合并。

## 任务配置与计划预览

管理员在配置页维护 `tasks`，普通用户只有查询权限。顶层任务使用 AstrBot `template_list`；v4.29.4 的条目渲染器不支持嵌套 `template_list`，因此 `user_fields` 使用 JSON 文本编辑器，内容是字段定义数组。程序传入配置时也可直接使用数组，两种表示产生相同配置版本。此处只填字段元数据，不能填实际账号凭据。

| 配置 | 规则 |
| --- | --- |
| `timezone` | IANA 时区，默认 `Asia/Shanghai`；预览时间带 UTC 偏移 |
| `timeout_seconds` | 全局默认 180 秒，范围 1–240；每任务的 0 或缺省值继承全局 |
| `task_id` | 小写字母开头，可含数字与下划线，最长 64 字符；不可重复，改名不改变 ID |
| `runtime` | `python` 或 `node`；不接受任意 shell |
| `script_url/source_commit/sha256` | 固定 HTTPS GitHub 来源、40 位提交及 64 位预期内容摘要 |
| `cron` | 五段常规表达式，支持数字、月份/星期简称、列表、正向范围和步长；周日 0/7，日与星期同时受限时为 OR |
| `user_fields` | 每项含 `field_id/label/env_name/value_type/required/secret/hint`；类型为 `string/int/bool`，`hint` 可省略 |
| `fixed_env` | 仅声明非秘密字符串常量；不在私聊查询中返回名称或值 |
| `input_adapter/result_adapter` | 通用 `direct_env` 配 `json_protocol/process_only`；天翼和移动使用对应专用组合 |
| `dependency_bundle` | 制品声明，不表示依赖已准备或经过验证 |

最多 32 个任务、每任务 32 个字段；字段 JSON 文本上限 64 KiB。环境变量名必须为合法 ASCII 名称，保留声明大小写，但禁止仅大小写不同的重名；用户字段与固定环境变量不得重叠。`PATH/HOME/SHELL/ENV/BASH_ENV/IFS` 及 `PYTHON*/NODE_*/LD_*/DYLD_*` 均不可声明，判禁不区分大小写。

来源暂限 `github.com/.../blob/<commit>/...` 和 `raw.githubusercontent.com/.../<commit>/...`，前者规范化为后者。拒绝用户信息、查询参数、片段、非标准端口与路径穿越，URL 中的提交必须匹配 `source_commit`。当前只做静态校验，不下载脚本，也不把填写的 SHA256 当成内容已经验证的证据。

配置版本是规范化任务内容的 SHA256 指纹，不写 KV。名称、字段顺序、脚本声明、时区或有效超时等业务配置变化会改变版本；WebUI 的 `__template_key`、字典键序及等价 JSON 表示不影响版本。指纹与脚本内容摘要是不同字段。

插件在加载和处理指令时校验配置；无效时拒绝使用并返回固定安全错误，未提供 WebUI 保存前拦截。未来三次时间通过私聊详情查询；禁用任务同样可以预览，但预览不代表已有定时器或任务已准备。无法产生未来日期的 cron 被拒绝；`0 9 31 2 MON` 仍有效，因为二月的周一满足 OR 条件。`@daily`、秒/年字段、随机 H/R 及 L/W/# 扩展不支持。

### 两个禁用模板

默认任务只在未配置 `tasks` 时提供；显式设置 `tasks=[]` 不会补回模板。默认值统一维护在 `_conf_schema.json`。

| ID | 计划 | 必填秘密字段 |
| --- | --- | --- |
| `tianyi_cloud` | 每日 07:10 | `username/password` |
| `mobile_cloud_experimental` | 每日 07:20 | `authorization/phone/auth_token` |

天翼模板引用 [BlueSkyXN/189Cloud-Checkin 的固定提交](https://github.com/BlueSkyXN/189Cloud-Checkin/tree/8e02d162ec6d1b5c00736afffb727d9dcfe8ba7b)，移动模板引用 [smallfawn/QLScriptPublic 的固定提交](https://github.com/smallfawn/QLScriptPublic/tree/2d4c26fdc72c3206bb9cf9afc1fb4991d337f888)。本仓库仅记录来源及预期摘要，未移植脚本；准备或启用前仍须核实许可、依赖和实际行为。

移动模板及其适配器必须保持 `experimental=true`、`enabled=false`。当前所有任务均不执行，开启其他任务的配置开关也不会绕过管理员准备、来源校验或执行层验收。

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
- 接口与离线核验来源：[AstrBot v4.29.4](https://github.com/AstrBotDevs/AstrBot/tree/v4.29.4)，[AGPLv3 许可证](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/LICENSE)；原始配置及指令解析代码未复制进插件运行代码。
- Cron 计算依赖：[croniter 6.2.4](https://pypi.org/project/croniter/6.2.4/)，MIT 许可证；源码由安装依赖提供，本仓库不内置其副本。

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

仓库只使用 squash 合并。合并标题为 `类型(scope): 描述 (#PR编号)`，例如 `功能(permissions): 增加普通私聊权限与帮助入口 (#12)`；保留原来的类型和 scope，编号使用半角括号。编号不计入摘要的 2–50 字符长度限制，摘要末尾仍不加句号。

普通工作分支提交和 PR 标题无需提前添加编号。合并负责人须核对最终标题后缀及 1–2 句正文；使用 `gh pr merge --subject` 自定义标题时也必须显式包含 ` (#PR编号)`，不能用不合规的 PR 描述替代提交正文。

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
