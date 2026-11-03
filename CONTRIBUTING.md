# 贡献指南

本文是开发环境、测试、状态存储和仓库协作的入口。面向用户的安装、指令和签到行为见 [README.md](README.md)，长期工程边界见 [AGENTS.md](AGENTS.md)，权限、主密钥、执行设置、管理员准备与任务字段见[配置说明](docs/configuration.md)，此处不重复。

## 开发环境

`metadata.yaml` 声明支持 AstrBot `>=4.29.4,<5`；接口只在 v4.29.4 上核验过，4.x 的其他版本未逐一验证。AstrBot 4.29.4 要求 Python 3.12 或更高版本，开发与离线测试使用同一 Python。宿主依赖固定在 `requirements.txt`，装进开发虚拟环境或 AstrBot 实际使用的解释器，不装到系统 Python。

天翼脚本依赖单独隔离在 `third_party/tianyi`：`requirements.lock` 锁定 7 个纯 Python wheel 的版本与 SHA256，测试目标为 Python 3.12 到 3.14。requests、rsa 等脚本依赖不安装到 AstrBot 宿主，也不从宿主导入。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

测试使用真实 croniter、cryptography 和受控的 KV、AstrBot 边界替身，不需要云盘账号。本地没有 Node 时相关用例会明确跳过，CI 显式安装 Node 22。测试不能替代 WebUI、真实 QQ、NapCat、Neo 沙箱或实际签到验证。

入口在 `main.py`，状态层是 `state.py` 与 `storage.py`，执行边界在 `executors/`、`runners/`，制品构建在 `preparation.py`、`artifact_service.py`，维护命令在 `scripts/`。

## 离线测试与制品准备

天翼脚本测试只运行禁网替身，不执行真实签到：

```bash
cd third_party/tianyi
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
.venv/bin/python -m unittest discover -s tests -v
```

天翼离线制品需要本地 wheelhouse，在仓库根目录执行，wheel 和制品留在临时目录、不入库：

```bash
WORKDIR=$(mktemp -d)
python3 -m pip download --require-hashes --only-binary=:all: --dest "$WORKDIR/wheels" -r third_party/tianyi/requirements.lock
python3 scripts/prepare-tianyi.py --wheelhouse "$WORKDIR/wheels" --output "$WORKDIR/tianyi.pyz"
```

`build_tianyi` 校验固定源码摘要，以及 7 个 wheel 的版本、内容摘要和纯 Python 布局，然后生成 zipapp，不下载、不安装依赖、不执行天翼源码，产物不超过 1 MiB。CLI 拒绝覆盖已有输出，成功后打印 `source_commit`、`source_sha256`、制品 `sha256` 和 `size`。上传后由 wrapper 校验摘要并启动，不需要改执行器。

通用任务没有本地 CLI，制品由运行期的管理员准备按任务声明构建。构建过程不执行下载内容，也不在宿主机安装依赖；制品尺寸、成员上限和拒绝规则见[配置说明](docs/configuration.md#管理员准备)。

原生框架验证把集成后的插件源码打成 ZIP，在装有 AstrBot 4.29.4 的独立 venv 中执行：

```bash
python scripts/verify-native.py --archive /tmp/plugin.zip
```

前提是可导入 `AstrBot==4.29.4`、`node` 在 `PATH` 中、未使用 `-O` 或 `PYTHONOPTIMIZE`。harness 用真实 `PluginManager.install_plugin_from_file`、原生 Star KV 和 loopback OneBot 客户端，不是真实 QQ。

## 状态存储

`storage.py` 通过 Star 异步 KV 的 `get_kv_data`、`put_kv_data` 维护唯一业务键 `checkin_state`，不建数据库或状态文件。快照包含 `schema_version`、`revision` 和业务集合，当前 schema 版本 3，默认序列化上限 8 MiB。读取旧版本时迁移到当前结构并补字段，未知版本、结构损坏或超限一律报错，不重置或裁剪记录。`metadata.yaml` 的 author 和 name 决定 KV 命名空间，开始保存业务数据后必须保持稳定。

| 接口 | 行为 |
| --- | --- |
| `await initialize()` | 键不存在才创建新快照；读取、校验并重新保存完整快照后才可用；已有待确认值时只重试原值 |
| `await mutate(callback)` | 单锁内复制成功快照，运行同步修改函数，校验后提交；确认成功才替换内存并增加 revision |
| `snapshot()` | 返回最后已确认快照的深拷贝；尚无成功提交时拒绝 |
| `await retry_pending()` | 原样重试待确认快照，不读回确认、不增加 revision、不写旧快照回滚 |
| `stop_accepting()` | 立即拒绝新操作，已开始的提交仍需完成 |
| `await close()` | 停止接单并等待当前操作；有待确认写入时重试原值，成功后才关闭 |

写入默认超时 30 秒。异常、超时或取消都保留待确认快照，`ready` 变为 false，新修改被拒绝，旧成功快照仍可读取。取消不保证框架队列中的写入没有完成，因此读回缓存不算持久化确认。修改函数不得改 `schema_version` 或 `revision`，也不得执行异步操作、网络请求或脚本。同一 Context 上按 `plugin_id` 登记进程内写者，旧写者未退出时新实例不能接管；登记只保存运行时锁和对象引用。仅支持单进程、单实例。

队列单次迭代复用已校验的快照，无业务时不重复复制或校验。新凭据记录不再保存 `field_ids`，但旧记录仍参与完整记录指纹与比较，不能只比较部分字段。录入意图只保存存在性标记，仍须在超时和重载后拦截迟到的敏感输入。

向导字段原文只保存在进程内会话中，持久化前加密；明文凭据不得进入日志、回执、shell 或命令行参数。修改任务字段、权限规则或指令输出时，同步更新 README 或[配置说明](docs/configuration.md)。

## 测试与验证

提交前至少运行上面的离线测试。涉及框架接口、OneBot 事件、KV 写入确认或调度时间的改动，补充对应回归测试；不得用占位实现、吞异常或弱化断言通过。PR 的“离线测试”在 Python 3.12、3.13、3.14 上安装 `requirements.txt` 和 Node 22 后跑全量用例，“天翼脚本离线测试”在同样矩阵的独立环境校验依赖哈希，必需检查“提交信息规范”等待两者后校验提交历史。普通 CI 不连接真实 QQ、Neo 或云盘。

验证分三级报告：离线与替身测试、真实 Neo 假凭据测试、真实账号测试。只执行了前一级时不得暗示后一级通过；原生框架 smoke 使用真实 AstrBot 4.29.4、原生 Star KV 和 loopback OneBot，覆盖两次 SIGKILL 后的冷恢复（加密绑定保留、已开始业务不重放、未过期排队继续、结果不明通知不重发），仍不是真实 QQ、生产 Neo 或真实账号。

### 真实 Neo 假凭据验收

目标为固定提交 `f4d248b4274c1a2f05402f75a3b39e5c19be9ff5` 的 Docker Bay，仅运行生成的假脚本和假凭据；执行器使用真实 Bay REST，Star/KV 边界为受控内存适配器。验收要求 Bay 内宿主路径与宿主实际 Cargo 目录指向同一位置（见[配置说明](docs/configuration.md#docker-清理条件)），不能只凭 API 返回 `not_found` 判断已清理。

路径对齐后逐组确认 Sandbox/Cargo API 不存在、对应宿主 bind 目录不存在、无同名卷和关联容器；上传目录为 0700、凭据文件为 0600 且归执行用户，历史记录中未出现假凭据标记。未验证 Docker named volume、Kubernetes、完整 AstrBot/QQ 入站流水线、跨进程崩溃和真实账号签到。

#### 通用依赖包

同一 Bay 版本，双路径挂载在现场确认保持，未改部署配置。用真实 Python 依赖模块和 Node `node_modules` 在假程序中执行业务，并验证客户端注入清理失败后的阻断恢复；清理未确认时保留记录、不放行新执行。物理清理证明与上一节相同：API、宿主 bind 目录、同名卷和关联容器均确认不存在。该记录不证明管理员准备入口或公网 GitHub 下载已通过验收。

Star/KV 仍是受控内存适配器，未连接真实 QQ 或账号，不声明真实 AstrBot + Neo 全链路。

## 来源与许可

本仓库以 AGPLv3 发布。插件接口基于 AstrBot，AstrBot 以 AGPLv3 发布；只在 v4.29.4 核验，管理员重载使用的 `_pm_lock`、`_terminate_plugin`、`_unbind_plugin` 和 `load(specified_module_path=...)` 属于管理器私有接口，升级 4.x 版本必须重新核对，失败时不得改用其他重载入口。

- Neo 接口按 Shipyard Neo `f4d248b4274c1a2f05402f75a3b39e5c19be9ff5` 核验，该项目为 AGPLv3；插件客户端和 wrapper 是独立实现，没有移植上游代码。执行层只接受已确认清理条件的固定 Docker 部署，判断依据见[配置说明](docs/configuration.md#docker-清理条件)。
- `third_party/tianyi` 来自 BlueSkyXN/189Cloud-Checkin 的 `43d2e7054dba7c5c2152e9c8c88fd23e6073a45b`，保留 Apache-2.0 `LICENSE`。本地修改新增 `reason_code`、区分需要认证和结果未知，保留原有 `status/counts`；源码头标明来源，后续修改在当前仓库提 PR。
- 设备验证结果 `-133` 参考 OpenList `e72822f5390f4272c9fe9da8cabba3c1a270182f` 的 PC 登录客户端（AGPL-3.0）的协议解释，未移植实现。验证码与设备结果尚未用真实账号验收。
- 移动模板仍引用 smallfawn/QLScriptPublic 的固定提交，未引入脚本，模板默认禁用。

## Git、提交与合并

克隆后安装仓库自带的 hooks：

```bash
bash scripts/install-git-hooks.sh
```

脚本检查 Python 3，设置执行权限并配置 `core.hooksPath=.githooks`。配置只作用于本地仓库，linked worktree 共用，每个新克隆安装一次。

`pre-commit` 拒绝 detached HEAD，拒绝任意工作树中的 `main`、`master`、`develop`，也拒绝主工作目录中的其他任务分支，并运行入库内容检查；`commit-msg` 校验提交信息。`dev/*` 可以在主工作目录或 linked worktree 直接提交，此时内容检查和 `commit-msg` 仍会执行；也可以走任务分支加 linked worktree 再发 PR。

任务分支名为 `feature|fix|hotfix|chore/<编号>-<小写描述>`，正则为 `^(feature|fix|hotfix|chore)/[0-9]+-[a-z0-9-]+$`；`dev/*` 和 `release/*` 不校验这一格式，其他不符合的分支名只警告，不阻断。钩子可用 `--no-verify` 跳过或被本机修改，最终以 PR 必需检查为准。

从 `dev/0.0.1` 开始任务时，在 linked worktree 中新建任务分支并提交；`dev/*` 也可直接在主工作目录提交。squash 合并不保留祖先关系，任务分支删除要用 `git branch -D`，且必须先确认 PR 已合并、worktree 无未提交修改。

### 入库边界

根 `.gitignore` 是路径规则入口：本地 `.omp/` 资料、虚拟环境、缓存、日志、下载的 wheel、生成的 zipapp/压缩包、已知凭据文件和运行状态文件不入库。源码、必要测试、许可证、固定版本与 SHA256 依赖锁、脱敏后的验收结论要保留，不要按“第三方文件”或“长随机串”一概删除。`.env.example`、`.env.sample`、`.env.template` 只允许无真实值的示例；其他必要测试资源需精确的忽略例外和用途说明。不得用 `git add -f` 绕过。

本地钩子和 PR 检查共用 `scripts/check-repository-content.py`：本地检查暂存树和暂存的根 `.gitignore`，不读未跟踪文件；CI 检查 PR 新增的每个提交树，先加入再删除的制品也会被拒绝。检查不使用个人全局忽略文件或 `.git/info/exclude`，不修改索引和工作区。天翼源码顶层的 `accounts`、`TG_BOT_TOKEN`、`TG_CHAT_ID` 声明必须为空，检查只解析 AST、不执行源码、不输出值。

```bash
python3 scripts/check-repository-content.py index
python3 scripts/check-repository-content.py tree COMMIT_SHA
```

路径规则和天翼声明之外，真实凭据、原始私聊或验收记录、动态写入的配置仍须人工审查；测试假值不得连接真实服务，也不能整体豁免 `tests/`。只按明确路径暂存，不覆盖他人改动。

### 提交信息

首行格式为 `类型(scope): 描述`，标题摘要 2 到 50 个字符，至少含一个中文汉字，不能以空白或标点开头、以空白或句号结尾。允许的类型：

```text
功能 修复 文档 样式 重构 性能 测试 构建 持续集成 杂项 回滚
feat fix docs style refactor perf test build ci chore revert
```

`scope` 必须匹配 `^[a-z0-9][a-z0-9._/-]*$`。标题后空一行，正文写 1 到 2 句中文，每句含中文汉字并以 `。`、`！` 或 `？` 结尾。

PR 标题使用同一首行格式。工作提交无需提前带编号；squash 合并提交必须在末尾追加半角 ` (#PR编号)`，编号不计入摘要长度。

### PR 与合并

本地可先用同一条校验脚本检查标题、提交历史和分支来源：

```bash
bash scripts/check-pr-policy.sh title TITLE_FILE
bash scripts/check-pr-policy.sh commits BASE_SHA HEAD_SHA
bash scripts/check-pr-policy.sh branch BASE_BRANCH HEAD_BRANCH
```

`commits` 校验 BASE..HEAD 引入的每个提交，不检查 GitHub 生成的合成 merge commit。`dev/*` 接受任务分支，拒绝以 `master`、`main`、`develop` 或另一个 `dev/*` 作为普通任务来源。`master` 只接受 `release/*` 和 `hotfix/*`；发布分支从对应开发分支切出，hotfix 从发布主线切出，合入 `master` 后同步回仍活跃的开发线。

只使用 squash 合并，标题为 `类型(scope): 描述 (#PR编号)`，合并负责人核对后缀与正文；用 `gh pr merge --subject` 时同样要显式带编号。

远端 ruleset 与合并设置需在 GitHub 页面核对，本地钩子不能反映其状态。当前约定：`master` 和 `dev/*` 通过 PR 合入，必需检查为 `PR 标题规范` 和 `提交信息规范`，只开放 squash merge，禁止强推和删除受保护分支；未现场核实的远端限制不作为已生效的事实。