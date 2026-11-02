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

- `main.py`：Star 生命周期、指令入口、帮助、任务查询、本人状态回复和管理员准备入口。
- `permissions.py`：私聊事件校验、身份提取和权限判定。
- `config.py`：权限配置的解析和 QQ 号规范化。
- `task_config.py`：任务目录校验、Cron 计算和配置版本。
- `enrollment.py`：5 分钟私聊录入会话、原始文本读取、超时和待录入意图。
- `credentials.py`：主密钥读取、认证加密、绑定键和凭据原子保存。
- `bindings.py`：本人绑定偏好更新、删除确认指纹和绑定与凭据的原子删除。
- `state.py`：快照结构和 JSON 校验。
- `storage.py`：基于 Star 异步 KV 的事务写入。
- `executors/client.py`：完整来源的 Neo 连接解析、异步 REST、鉴权检查和有界响应。
- `executors/neo.py`：持久化执行意图、串行执行、资源归属检查、清理阻断与恢复。
- `preparation.py`：从本地源码、许可和锁定 wheel 离线构建天翼 zipapp，校验输入与制品。
- `artifact_service.py`：下载锁定 wheel、校验缓存、构建制品，并把准备记录写入 KV。
- `scripts/prepare-tianyi.py`：使用本地 wheelhouse 生成制品的开发命令。
- `runners/tianyi_entry.py`：制品内的单账号入口，核对结果后只输出安全状态。
- `runners/bootstrap.py`：上传到 Neo 的独立 wrapper；消费凭据文件，以 argv 启动子进程并只返回安全结果信封。

向导字段原文只保存在进程内会话中，持久化前加密。明文凭据不得进入日志、回执、shell 或命令行参数。

修改公共行为时同步更新 README 或 `docs/configuration.md`。任务字段、权限规则或指令输出变化会影响用户，不能只改代码。

## 状态存储

`state.py` 定义快照格式，`storage.py` 通过 `get_kv_data` 和 `put_kv_data` 操作唯一业务键 `checkin_state`。插件不创建数据库或状态文件。

快照包含 `schema_version`、`revision`，以及 `users`、`bindings`、`credentials`、`batches`、`runs`、`outbox`、`cleanup_jobs`、`cooldowns`、`dedupe`、`prepared_artifacts` 集合。当前 schema 版本为 2；读取版本 1 时补充空的 `prepared_artifacts` 后迁移，原有记录保持不变。迁移结果要作为完整快照重新保存并确认后才能使用。未知版本、结构损坏或超限都会报错，不会重置或裁剪记录。默认序列化上限为 8 MiB。

`prepared_artifacts[task_id]` 保存 `task_version`、`source_commit`、`source_sha256`、`sha256`、`size` 和 `inputs_sha256`，不保存脚本字节、凭据或制品文件。制品文件按内容摘要存放在插件数据目录；state 层只校验快照结构，不读取或重建文件。

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

## 内部执行接口

`NeoExecutor.execute(PreparedExecution(...), guard=...)` 接收已经准备好的脚本字节、SHA256、固定来源 commit、任务版本、身份和本次环境变量。调用方负责脚本来源及依赖制品验收，并在同步 `guard(snapshot)` 中重新检查权限和版本；拒绝时抛异常，放行时返回 `None`。执行器本身不下载脚本、不安装依赖、不提供 shell 聊天入口。天翼离线制品及其单账号结果入口已实现，管理员准备指令和持久制品缓存已接入；绑定到执行请求的转换、队列和通知还没有接入。

执行器在 `checkin_state.runs` 和 `cleanup_jobs` 中记录 `kind=neo_execution` 的状态。先持久化 `phase=admitted` 表示尚未尝试分配，再单独确认 `phase=creating` 的创建意图，之后才能调用 Neo；创建和执行意图的写入返回后均再次同步检查授权。恢复时仅对结构合法、没有资源 ID 的 `admitted` 记录确定取消并解除阻断；`creating` 或之后的阶段不能凭缺少 ID 推断未创建。这样，即使无分配证明的终态写入被另一笔未确认提交阻挡，精确重试后仍可从持久化阶段恢复。每个运行预留 8 KiB 的快照空间用于资源 ID 和终态，最多保留 1000 个运行条目，容量不足拒绝新增；自动历史裁剪尚未接入。记录不保存脚本内容、环境变量或原始输出。

脚本只在独立 Sandbox 中执行。固定命令先建立执行用户拥有的 0700 目录和 0600 文件，再经文件 API 覆写；Ship 的上传实现使用 `wb`，保留已有文件的 owner/mode。wrapper 读出凭据后立即解除文件链接，校验脚本 hash，以 `shell=False` 和独立进程组启动 Python 或 Node。stdout 有界收集，stderr 只排空；两者均不透传到 Bay。子进程退出后也清理其残留进程组，Linux 下使用 subreaper 回收已脱离直接父进程的后代。

`json_protocol` 只接受完整 JSON 对象中已知的 `status`；自由文本原因和其他字段不会返回。退出码与业务状态分别保存，`process_only` 或无有效证据均为 `unknown`。安全信封包含 `protocol/process_status/exit_code/business_status/reason_code/duration_ms`，字段和范围由 `validate_result` 验证。业务请求可能已经发出后的本地取消记为 `interrupted/unknown`，不把取消 HTTP 等待或成功清理资源当作业务未发生的证据；未发送请求或取得明确 wrapper 取消信封时才能记录确定取消。

终止插件时先停止入口，再取消并等待执行及受管清理，最后关闭 StateStore。旧执行器未退出时新实例不能接管写者。待确认 KV 写入期间只进行已知资源清理，不另写快照；下一次恢复仍先确认原待写快照，再处理清理记录。清理不能确认时保留记录，不放行新执行。

### 接口依据与许可

Neo 接口按 [Shipyard Neo `f4d248b4274c1a2f05402f75a3b39e5c19be9ff5`](https://github.com/AstrBotDevs/shipyard-neo/tree/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5) 核验，该项目使用 AGPLv3；本插件客户端和 wrapper 为独立实现，没有移植上游代码。相关依据：

- [Bay API v1](https://github.com/AstrBotDevs/shipyard-neo/blob/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5/doc/bay_api_v1.md)：请求、响应与文件路径。
- [CargoManager](https://github.com/AstrBotDevs/shipyard-neo/blob/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5/pkgs/bay/app/managers/cargo/cargo.py) 与 [Docker driver](https://github.com/AstrBotDevs/shipyard-neo/blob/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5/pkgs/bay/app/drivers/docker/docker.py)：删除引用指向的数据后才删 Cargo 记录；bind 模式必须保证该宿主路径在 Bay 内仍指向真实 Cargo，不能只检查 API 404。
- [K8s driver](https://github.com/AstrBotDevs/shipyard-neo/blob/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5/pkgs/bay/app/drivers/k8s/k8s.py)：PVC 删除受理不等于清理完成，因此当前执行层仅接受已确认的固定 Docker 部署。
- [AstrBot v4.29.4 Context](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/star/context.py)：通过 `get_config()` 读取框架配置，不访问框架私有存储或借用聊天资源。

### 本仓库维护的天翼脚本

`third_party/tianyi/index.py` 和上游回归测试来自 [BlueSkyXN/189Cloud-Checkin 的 `43d2e7054dba7c5c2152e9c8c88fd23e6073a45b`](https://github.com/BlueSkyXN/189Cloud-Checkin/tree/43d2e7054dba7c5c2152e9c8c88fd23e6073a45b)，保留该目录下的 Apache-2.0 `LICENSE`。本地修改新增 `reason_code`、区分认证要求与未知结果，保留原有 `status/counts` 和通知行为；源码头标明来源及修改。后续修改在当前插件仓库提交 PR，不依赖上游接收修改。

设备验证结果 `-133` 参考 [OpenList `e72822f` 的 PC 登录客户端](https://github.com/OpenListTeam/OpenList/blob/e72822f5390f4272c9fe9da8cabba3c1a270182f/drivers/189pc/utils.go)（AGPL-3.0）的协议解释，未移植其实现。验证码标志和设备结果尚未用真实账号验收；不识别验证码、不发短信、不增加业务重试。`reason_code` 契约见 `main_handler` 文档字符串；`failed` 汇总同时包含需要认证和结果未知的情况，判断凭据问题前需查看 `reason_code`。

脚本专用依赖锁为 `third_party/tianyi/requirements.lock`，包含七个固定版本纯 Python wheel 的 SHA256，测试目标为 Python 3.12 到 3.14。没有移入上游的旧依赖压缩包，也不把 requests、rsa 安装到 AstrBot 宿主。以下命令仅运行禁网测试替身，不是宿主执行真实签到的入口：

```bash
cd third_party/tianyi
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
.venv/bin/python -m unittest discover -s tests -v
```

#### 离线制品准备

`preparation.build_tianyi(wheelhouse)` 校验本仓库固定天翼源码的 SHA256，以及七个 wheel 的版本、内容摘要和纯 Python 布局，生成包含源码、依赖、结果入口及许可的独立 zipapp。构建过程不下载、不安装依赖、不执行天翼源码，也不读取用户凭据；拒绝非普通输入文件、路径穿越、成员冲突、符号链接、原生扩展和不支持的 wheel `.data` 布局。制品不能超过现有执行器的 1 MiB 限制，同一输入与压缩运行环境下重复构建得到相同字节。

在仓库根目录执行以下开发准备命令；wheel 和制品留在临时目录，不纳入 Git：

```bash
WORKDIR=$(mktemp -d)
python3 -m pip download --require-hashes --only-binary=:all: --dest "$WORKDIR/wheels" -r third_party/tianyi/requirements.lock
python3 scripts/prepare-tianyi.py --wheelhouse "$WORKDIR/wheels" --output "$WORKDIR/tianyi.pyz"
```

CLI 拒绝覆盖已有输出，成功后只打印包含 `source_commit`、`source_sha256`、制品 `sha256` 和 `size` 的 JSON。源码摘要与制品摘要不同：调用内部执行器时使用 `PreparedArtifact.script` 及其 `sha256`，运行时为 `python`、传输结果适配为 `json_protocol`；制品上传成 `script.py` 后仍由原有 wrapper 校验并启动，不需要更改 Neo 执行器。

运行期由 `artifact_service.PreparationService(store, data_dir, *, client=None)` 提供准备和读取：

| 接口 | 行为 |
| --- | --- |
| `await prepare(task, *, guard)` | 先检查有效缓存，命中时不访问网络；否则下载锁定 wheel、构建制品、写入内容寻址缓存和 KV 记录 |
| `await load(task)` | 只读取并校验任务版本、输入指纹、记录和文件摘要，不下载、不构建、不执行 |
| `await close()` | 取消未完成的准备和读取操作，等待自身操作与线程退出，再关闭自有客户端 |

`PreparedArtifact` 包含 `script`、`sha256`、`source_commit`、`source_sha256` 和 `inputs_sha256`；`script` 不出现在 `repr` 中。

`guard` 在耗时阶段结束后、同步 KV 修改函数内和返回前重新检查，撤权或任务变化会中止准备。文件与网络操作不放在 KV 锁内。`PreparationError` 表示任务声明不匹配、输入、下载或缓存校验拒绝；`PreparationBusy` 继承 `PreparationError`，表示已有准备在进行；`StateUnavailableError` 和 `CommitUnconfirmedError` 按存储规则上抛。注入的 `client` 归调用方管理；生产路径使用服务自建客户端，禁用环境代理，只通过 HTTPS 连接 PyPI 与 `files.pythonhosted.org`，并校验目标解析为公网地址。只有自建客户端会在 `close()` 时关闭。

制品入口只接受单账号 `TY_ACCOUNTS` JSON，保留用户名/密码原值，强制禁用上游 Telegram，并且只调用一次 `main_handler`。它丢弃上游输出，严格核对单条结果、计数、`ok` 和 `reason_code`，只输出安全 `status`；验证码/设备验证为 `needs_auth`，接口明确拒绝为 `failed`，矛盾或缺失证据为 `unknown`，不自动重跑。不要在宿主向制品注入真实凭据运行签到。

已用实际锁定 wheel 构建制品并完成九个禁网 HTTP 替身场景；另经现有 `NeoExecutor`、受控 Bay 和真实 wrapper 上传/执行 zipapp，观察到缺少凭据的 `config_error` 终态和资源清理。这里的 Bay 是测试替身，不是真实 Neo 部署。CI 在三个 Python 版本构建真实制品，并用有效假字段和网络审计钩子验证请求在联网前被阻止、只尝试一次、输出 `unknown` 且无 stderr。

两个模板保持默认禁用；目前没有真实账号或真实 Neo 制品验收结果。


## 测试与验证

提交前至少运行上面的离线测试。涉及框架接口、OneBot 事件、KV 写入确认或调度时间的改动，需要补充对应的回归测试；不要用占位实现、吞异常或弱化断言通过测试。

PR 的“离线测试”任务分别在 Python 3.12、3.13、3.14 上安装宿主 `requirements.txt`，显式安装 Node 22，并运行 `unittest discover -s tests -p 'test_*.py' -v`；“天翼脚本离线测试”在相同 Python 矩阵的独立虚拟环境中校验脚本依赖哈希并运行脚本测试。既有必需检查“提交信息规范”等待两组矩阵，仅在全部成功后校验提交历史；任一失败、取消或跳过都不能通过，无需修改现有必需检查名称。普通 PR CI 不连接真实 QQ、Neo 或云盘服务。

执行层回归位于 `tests/test_neo_client.py`、`tests/test_neo_executor.py`、`tests/test_neo_lifecycle.py` 和 `tests/test_bootstrap.py`。制品构建与准备服务回归位于 `tests/test_preparation.py` 和 `tests/test_artifact_service.py`；管理员准备指令位于 `tests/test_admin_preparation.py`，schema 迁移与写入确认位于 `tests/test_state.py`、`tests/test_storage.py`。`tests/neo_support.py` 是受控 Bay 替身，会在临时目录运行本仓库 wrapper 与假脚本；这不是产品的宿主回退路径。本地未安装 Node 时对应测试会明确跳过；CI 显式安装并检查 Node，不依赖 runner 预装。macOS 测试只将 Node 的可执行路径映射到本机安装位置，实际运行 Node 子进程。

离线 smoke 通过真实 loopback HTTP 连接受控 Bay，覆盖两用户隔离、非零退出码的显式业务结果、超时、清理阻断及恢复不重跑。真实 Neo 假凭据测试与真实账号测试分别记录如下。

### 制品准备本地验收

- Python 3.14 下全量 328 项测试通过；`main.py`、`preparation.py`、`artifact_service.py` 及相关新测试通过 Ruff 检查。
- 真实 PyPI smoke 使用实际 `/签到 管理 准备` 入口和生产自建 HTTP 客户端：14 次 GET（7 次元数据、7 次 wheel），生成制品 614156 字节，SHA256 为 `f22f8b6bbbc7eb0777b23f28e1b987da9839e6b9504e46a4d9508c7c3eb5393c`。
- 同一服务实例再次准备命中缓存，插件对象重载后再次准备也未新增请求；schema 2 准备记录在重载后保留。
- AstrBot 事件和 KV 使用受控替身；文件读写和 PyPI HTTP/TLS 为真实连接。没有执行天翼源码、签到、Neo 或真实账号。
- 审查发现的压缩响应处理和 `InvalidURL` 错误边界已修复：修复前先复现，修复后回归。客户端读取 raw 流、协商 identity，并在读取正文前拒绝压缩响应。

### 真实 Neo 假凭据验收

目标为 Docker Bay 0.4.2，revision `f4d248b4274c1a2f05402f75a3b39e5c19be9ff5`，镜像 digest `sha256:b8920aed5bcc098242aa989c8b1eb5420c393e9a0f3dff5207979aba4a88403b`；Ship digest 为 `sha256:4a50b8290ce9f20cd615af3ded31613a89e61a2b0197a9b7a7c551a5ac3bc774`，profile 为 `python-default`，匿名访问关闭。只运行生成的假脚本和假凭据；执行器使用真实 Bay REST，Star/KV 边界使用受控内存适配器。

首次验收的 API 场景通过，但独立宿主检查发现 5 个 Cargo bind 目录残留，因此整体判为失败。原因是 Bay 内宿主绝对路径指向容器层 shadow 目录，实际 Cargo 通过另一条路径挂载。仅清理了这 5 个已记录的自有测试目录，并确认无卷或容器残留。随后经操作者授权，在没有其他 Sandbox 或 Neo runtime 运行时，为 Bay 增加宿主 Cargo 根目录的同路径挂载并仅重建 Bay；镜像、端口、网络、环境和其他服务未修改。

路径对齐后复验创建 6 组独立 Sandbox/Cargo，发送 5 次业务 wrapper 请求，另 1 次在凭据上传后、wrapper 启动前拒绝授权：

| 场景 | 已观察结果 |
| --- | --- |
| 两身份同时请求 Python / Node 假脚本 | Sandbox、Cargo 和输入隔离；最大并行业务请求数为 1 |
| 业务结果与退出码 | Python 退出 0 返回 `needs_auth`；Node 退出 3 返回 `already_signed` |
| 文件与输出 | 6 次上传后确认目录 0700、凭据文件 0600 且归执行用户所有；4 次完整返回的 wrapper 确认凭据文件已解除链接；6 个自有 Sandbox 历史中未出现假凭据标记 |
| 超时 | `timeout`，并在沙箱内确认后代进程已不存在 |
| 请求已发送后取消 | `interrupted/unknown`，不重发业务请求 |
| 清理失败恢复 | 对真实自有资源注入客户端清理失败，保留阻断；恢复后只清理、不重跑业务。未破坏 Docker daemon 来模拟故障 |
| wrapper 前撤权 | 确认凭据文件仍在后拒绝执行；该目录随清理删除，未发送业务 wrapper 请求 |
| 独立清理证明 | 每组同时确认 Sandbox/Cargo API 不存在、对应宿主 bind 目录不存在、无同名卷和关联容器；6 组全部通过 |

该结果仅覆盖上述固定版本和路径对齐后的 bind 拓扑。未验证 Docker named volume、Kubernetes、完整 AstrBot/QQ 入站流水线、跨进程崩溃或真实账号签到；没有运行真实云盘脚本。新增回归测试另外覆盖了 KV 等待期间撤权和远端副作用后取消的边界。

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

### 入库边界

根 `.gitignore` 集中定义路径规则：本地 `.omp/` 资料、虚拟环境、缓存、日志、下载的 wheel、生成的 zipapp/压缩包、已知凭据文件和运行状态文件不入库。源码、必要测试、许可证、固定版本与 SHA256 依赖锁、脱敏后的验收结论需要保留；不要按“第三方文件”或“长随机串”一概删除。`.env.example`、`.env.sample`、`.env.template` 仅允许无真实值的示例，其他必要测试资源须通过精确忽略例外和用途说明审核，不能用 `git add -f` 绕过。

本地 pre-commit 与 PR 历史检查共用 `scripts/check-repository-content.py`：本地检查暂存树及暂存的根 `.gitignore`，不读取未跟踪文件；CI 用当前策略检查 PR 新增的每个提交树，先加入再删除的制品也会被拒绝。检查不使用个人全局忽略文件或 `.git/info/exclude`，不会修改真实索引或工作区。天翼源码的 `accounts`、`TG_BOT_TOKEN`、`TG_CHAT_ID` 顶层声明必须为空，检查只解析 AST、不执行源码、不输出值。

```bash
python3 scripts/check-repository-content.py index
python3 scripts/check-repository-content.py tree COMMIT_SHA
```

这些检查覆盖已定义的路径规则和天翼声明，允许路径中的真实凭据、原始私聊/验收记录、动态写入配置等仍须人工内容审查；测试假值不得连接真实服务，也不能对整个 `tests/` 目录豁免。

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
