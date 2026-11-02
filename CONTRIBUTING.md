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

- `main.py`：Star 生命周期、指令入口、帮助、任务查询、本人状态回复、手动签到入口、调度器生命周期和管理员准备、取消与停用入口。
- `permissions.py`：私聊事件校验、身份提取和权限判定。
- `config.py`：权限配置的解析和 QQ 号规范化。
- `task_config.py`：任务目录校验、Cron 计算和配置版本。
- `enrollment.py`：5 分钟私聊录入会话、原始文本读取、超时和待录入意图。
- `credentials.py`：主密钥读取、认证加密、绑定键和凭据原子保存。
- `bindings.py`：本人绑定偏好更新、删除确认指纹和绑定与凭据的原子删除。
- `state.py`：快照结构、JSON 校验和 schema 迁移。
- `storage.py`：基于 Star 异步 KV 的事务写入。
- `task_control.py`：管理员停用意图、目标活动批次取消标记、最新配置保存与恢复，以及关闭时排空配置写入。
- `executors/client.py`：完整来源的 Neo 连接解析、异步 REST、鉴权检查和有界响应。
- `executors/neo.py`：持久化执行意图、串行执行、资源归属检查、清理阻断与恢复。
- `execution_config.py`：解析签到冷却、排队有效期、保留天数和执行间隔。
- `execution_queue.py`：持久化手动与自动签到 FIFO 队列、计划去重、冷却、间隔、权限复核、管理员取消和终态记录。
- `scheduler.py`：未来计划的 Cron 观察与准入，只调用队列，不执行脚本或读取凭据。
- `execution_bridge.py`：把队列请求绑定到最新凭据、已准备制品和 Neo 执行器，并生成本人状态摘要。
- `notifications.py`：终态通知 outbox、按来源和偏好决定发送、有界重试和投递状态。
- `preparation.py`：从本地源码、许可和锁定 wheel 离线构建天翼 zipapp，校验输入与制品。
- `artifact_service.py`：下载锁定 wheel、校验缓存、构建制品，并把准备记录写入 KV。
- `scripts/prepare-tianyi.py`：使用本地 wheelhouse 生成制品的开发命令。
- `runners/tianyi_entry.py`：制品内的单账号入口，核对结果后只输出安全状态。
- `runners/bootstrap.py`：上传到 Neo 的独立 wrapper；消费凭据文件，以 argv 启动子进程并只返回安全结果信封。

向导字段原文只保存在进程内会话中，持久化前加密。明文凭据不得进入日志、回执、shell 或命令行参数。

修改公共行为时同步更新 README 或 `docs/configuration.md`。任务字段、权限规则或指令输出变化会影响用户，不能只改代码。

## 状态存储

`state.py` 定义快照格式，`storage.py` 通过 `get_kv_data` 和 `put_kv_data` 操作唯一业务键 `checkin_state`。插件不创建数据库或状态文件。

快照包含 `schema_version`、`revision`，以及 `users`、`bindings`、`credentials`、`batches`、`runs`、`outbox`、`cleanup_jobs`、`cooldowns`、`dedupe`、`prepared_artifacts` 集合。当前 schema 版本为 3。签到批次可以带有可选的 `cancel_requested` 布尔字段记录管理员取消意图；该字段不改变 schema 版本，也不新增集合或迁移。管理员停用使用同一 `dedupe` 集合中的 `task_disable:<task_id>` 意图记录，配置保存确认后删除；该记录同样不改变 schema 版本，不新增集合或迁移，也不作为第二个永久启用标记。读取版本 1 时补充空的 `prepared_artifacts`；读取版本 2 时把旧手动批次、通知、冷却和索引改为 `checkin`、`terminal_notification`、`checkin_cooldown` 和 `checkin` 索引，旧批次补 `source="manual"`、`manual_requested=true`、空的 `scheduled_at` 和 `batch_id`，旧通知按手动来源处理。迁移保留原有业务记录，结果作为完整快照重新保存并确认后才能使用。未知版本、结构损坏或超限都会报错，不会重置或裁剪记录。默认序列化上限为 8 MiB。

`prepared_artifacts[task_id]` 保存 `task_version`、`source_commit`、`source_sha256`、`sha256`、`size` 和 `inputs_sha256`，不保存脚本字节、凭据或制品文件。制品文件按内容摘要存放在插件数据目录；state 层只校验快照结构，不读取或重建文件。

集合内业务记录由对应业务操作定义。签到使用现有 `batches`、`runs`、`outbox`、`cooldowns` 和 `dedupe` 集合；自动计划在 `dedupe` 中保存 `scheduled_batch` 与 `schedule_cursor` 记录，不新增业务集合或快照版本。状态层不执行入队、历史保留策略或凭据加密；调用方不得写入明文凭据。`metadata.yaml` 的 author 和 name 决定框架 KV 命名空间，开始保存业务数据后必须保持稳定。

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

`NeoExecutor.execute(PreparedExecution(...), guard=..., run_id=None)` 接收已经准备好的脚本字节、SHA256、固定来源 commit、任务版本、身份和本次环境变量。调用方负责脚本来源及依赖制品验收，并在同步 `guard(snapshot)` 中重新检查权限和版本；拒绝时抛异常，放行时返回 `None`。传入 `run_id` 时执行器使用该稳定编号；省略时保持原有每次调用生成 UUID 的行为。执行器本身不下载脚本、不安装依赖、不提供 shell 聊天入口。天翼离线制品及其单账号结果入口、管理员准备指令和持久制品缓存已接入；手动和自动签到通过同一队列使用同一执行器。

执行器在 `checkin_state.runs` 和 `cleanup_jobs` 中记录 `kind=neo_execution` 的状态。先持久化 `phase=admitted` 表示尚未尝试分配，再单独确认 `phase=creating` 的创建意图，之后才能调用 Neo；创建和执行意图的写入返回后均再次同步检查授权。恢复时仅对结构合法、没有资源 ID 的 `admitted` 记录确定取消并解除阻断；`creating` 或之后的阶段不能凭缺少 ID 推断未创建。这样，即使无分配证明的终态写入被另一笔未确认提交阻挡，精确重试后仍可从持久化阶段恢复。每个运行预留 8 KiB 的快照空间用于资源 ID 和终态，最多保留 1000 个运行条目，容量不足拒绝新增；签到已结算批次会连同同 run_id 的已结束运行和 outbox 按 `history_retention_days` 裁剪，独立 Neo 运行只受 1000 条上限约束。记录不保存脚本内容、环境变量或原始输出。

脚本只在独立 Sandbox 中执行。固定命令先建立执行用户拥有的 0700 目录和 0600 文件，再经文件 API 覆写；Ship 的上传实现使用 `wb`，保留已有文件的 owner/mode。wrapper 读出凭据后立即解除文件链接，校验脚本 hash，以 `shell=False` 和独立进程组启动 Python 或 Node。stdout 有界收集，stderr 只排空；两者均不透传到 Bay。子进程退出后也清理其残留进程组，Linux 下使用 subreaper 回收已脱离直接父进程的后代。上传 `credentials.json` 前重新检查存储就绪并运行捕获的凭据 guard，暂停绑定或替换凭据后的旧请求不会继续进入业务阶段。手动和自动签到都通过同一队列进入该执行器，自动准入本身不加载脚本、不读取凭据。

`json_protocol` 只接受完整 JSON 对象中已知的 `status`；自由文本原因和其他字段不会返回。退出码与业务状态分别保存，`process_only` 或无有效证据均为 `unknown`。安全信封包含 `protocol/process_status/exit_code/business_status/reason_code/duration_ms`，字段和范围由 `validate_result` 验证。业务请求可能已经发出后的本地取消记为 `interrupted/unknown`，不把取消 HTTP 等待或成功清理资源当作业务未发生的证据；未发送请求或取得明确 wrapper 取消信封时才能记录确定取消。

终止插件时先关闭调度器，停止队列 worker 和已接受的执行，等待 Neo 执行与清理停止，再对账 `running` 批次并排空当前到期的终态通知；未过期 queued 保留，不依赖下一次重载才发送终态。启动时调度器在队列初始化完成后创建，关闭时先于队列停止。旧执行器未退出时新实例不能接管写者。待确认 KV 写入期间只重试原快照，恢复后继续处理清理记录，不另写快照覆盖待确认值。清理不能确认时保留记录，不放行新执行。

### 签到队列

`ExecutionQueue` 接收状态存储、执行回调、同步 `guard`、配置读取、阻断判定、终态回调和通知预留回调。`enqueue(...)` 是手动入口，持久化 `batches[run_id]` 后返回 run_id。批次保存身份键、任务 ID 与版本、制品摘要、来源会话、排队和过期时间、序号、来源、是否手动参与、计划时间、批次 ID 及终态，不保存凭据、密文、事件对象或脚本字节。手动批次来源为 `manual`、`manual_requested=true`；自动批次来源为 `automatic`，同一计划共享一个 `batch_id`。

同一用户同一任务已有活动手动批次时，重复手动提交返回原 run_id。若活动批次是自动批次，手动提交在同一原子修改中把该批次标记为 `manual_requested=true`，采用本次合法来源会话，保留原 run_id，并保证最终只产生一条手动终态通知。

`enqueue_scheduled(task=..., scheduled_at=..., requests=[...])` 是自动入口。每个请求包含身份、来源会话和可空的制品摘要；队列校验后按平台和 QQ 身份稳定排序，并拒绝同一绑定的重复请求。整个计划在一次 `StateStore.mutate` 中完成：

1. 校验并清理过期记录，检查计划去重、游标、批次与通知容量。
2. 以 `schedule:<task_id>:<UTC ISO>` 作为计划键；已存在相同计划时返回 `duplicate` 和记录的 run_ids。`schedule_cursor:<task_id>` 保存任务水位，阻止已清理的旧计划重放。
3. 同一任务存在 `queued`、`running` 或 `cooldown_pending` 的批次时，记录 `skipped_overlap` 并返回，不创建请求。`finished` 且仅等待通知发送的批次不阻断下一次计划，但其记录继续保留，直到通知结算后才能清理。
4. 逐个运行同步 `guard`。权限撤销、绑定缺失或暂停、录入向导中、来源无效的用户本次跳过；执行器不可用、制品变化或凭据缺失仍纳入队列，由 worker 以配置错误正常结算；其他拒绝中止整个触发。
5. 没有可用用户时记录 `skipped_empty` 并返回。
6. 为所有可用用户预留终态和通知槽位，按连续序号写入批次，最后记录 `admitted` 及 run_ids。返回 `admitted`、`skipped_overlap`、`skipped_empty` 或 `duplicate`。

计划记录上限为 1000，任务游标上限为 256；清理只移除已结算、没有活动 run 且没有未结算通知关联的计划，游标保留最近水位。自动准入不检查手动冷却，自动和手动实际执行都会更新同一条全局执行间隔。自动用户与手动请求共用单 worker FIFO，没有优先级插入或独立 worker。

队列是单 worker FIFO，同一时间只执行一个签到。`MAX_ACTIVE_ITEMS=256`、`MAX_BATCH_ITEMS=1000`、`MAX_OUTBOX_ITEMS=1000`；入队时预留终态和通知容量，达到上限时拒绝新请求，不为新记录删除活动记录。手动冷却按用户和任务绑定分别记录，只对来源为手动或已标记手动参与的批次写入；用户间隔和任务间隔不分别计时，队列只保留一条最近全局执行与清理完成记录。只有 Neo 运行的 `business_attempted` 为真时才推进全局记录，手动冷却仅在手动参与时写入；未发送业务请求的取消或失败不产生间隔。下一条切换任务时使用 `task_interval` 且不叠加 `user_interval`；任务不变但用户变化时使用 `user_interval`；同一用户同一任务的手动请求还受手动冷却限制。跳过、未发送业务请求就取消或未执行的请求不改变这条全局记录；发送业务请求后才取消的记录按实际执行更新。排队超过 `queue_expiry_seconds` 时记为取消。

队列在 Neo 恢复后启动恢复：保留未过期排队记录，过期记录转为取消；已经开始或结果不明的记录不重放，使用已确认的 Neo 结果或 `interrupted/unknown`。待确认 KV 写入期间停止新工作，保留记录，按原快照重试后再恢复。`guard` 在入队、业务请求前和执行期间复查权限、任务版本、制品摘要、绑定暂停/删除状态及是否在录入向导中；取消后先完成资源清理，再处理下一条。

管理员取消通过 `request_cancel(run_id, guard=...)` 完成，只接受 32 位小写十六进制 run_id。请求在 `mutate` 写入确认后才生效；已结束、已请求或不存在的 run_id 返回对应状态，不重写批次、不补发通知，也不重跑业务。worker 在准入、认领、凭据上传边界、执行回调和轮询守卫中检查 `cancel_requested`：尚未发送业务请求时以 `cancelled/admin_cancelled` 结算，不执行脚本；凭据尚未上传时也不会继续上传，且不写冷却或全局间隔。业务可能已开始且没有明确结果时以 `unknown` 结算，已有可靠结果保持原值，只附加 `queue_reason=admin_cancelled`。取消不修改任务、Cron、绑定或后续计划，终态通知仍按现有规则发给运行所属用户。`tests/test_admin_cancellation.py` 覆盖权限、排队与运行阶段、已知结果保留、未确认写入和重载恢复。

执行回调加载已准备制品，解密最新已确认凭据，生成 `TY_ACCOUNTS`，并以同一 run_id 调用 `NeoExecutor.execute`。凭据明文不写入批次记录，也不进入日志。

### 管理员停用

`TaskControlService(store, config)` 接受 `StateStore` 和带可等待 `save_config_async` 的配置映射。`disable(task_id, *, guard)` 在当前管理员和任务目录检查通过后，以 `mutate` 在同一笔状态写入中保存 `dedupe["task_disable:<task_id>"]` 意图，并为该任务所有 `queued`、`running` 签到批次设置 `cancel_requested`；状态写入确认后才重新读取最新配置，深拷贝 `tasks`，只把目标任务的 `enabled` 设为 `false`，再通过框架公共异步方法保存。保存返回 `True` 后，删除意图的同步提交再次检查最新配置；只有目标仍禁用或已移除时才删除精确匹配的意图记录。若复查发现目标已重新启用，则不提交删除，保留意图、报告 `TaskControlSaveUnconfirmed` 并继续阻断目标执行；复查线性化点之后的配置保存属于后续显式修改。

`recover()` 在 Neo、队列和调度器初始化前处理遗留意图；`close()` 停止接单并等待所有已跟踪的保存与意图清理任务，调用方取消被 shield 隔离，关闭后不会留下晚到的配置写者。`disabling_task_ids(snapshot)` 和 `task_is_disabling(snapshot, task_id)` 供队列准入、执行 guard 和调度器排除停用状态使用；意图结构不合法、数量超过上限或与目标不一致时拒绝继续，不覆盖其他 `dedupe` 记录。配置保存异常、返回 `False`、最新配置无效或未体现停用时抛 `TaskControlSaveUnconfirmed`，意图保留并继续阻断目标执行。`tests/test_task_control.py` 和 `tests/test_admin_disablement.py` 覆盖该边界。

### 自动调度

`CronScheduler` 只读取状态存储、队列、配置回调和时钟。初始化时先执行一次 tick，建立每个启用任务的未来计划基线，再启动单 worker 循环；默认每 1 秒检查一次。tick 在存储未就绪时清空计划并返回，不补跑；全局 `enabled` 关闭时清空计划，重新打开后从新的未来计划开始。

任务配置版本变化、任务重新启用或时钟回拨时，计划重新建立。运行中只准入落在当前分钟的计划；已经错过的旧计划丢弃，不排空积压。每个到期计划从快照收集未暂停、不在录入向导中的绑定，按平台和 QQ 排序后交给 `enqueue_scheduled(...)`，不加载脚本、不读取明文凭据。

到期任务准入或入队时的已分类错误只记录在该任务下，其他到期任务继续；`parse_task_catalog` 整体配置无效会使 tick 失败并暂停调度；未预期内部错误会退出定时 worker。状态存储不可用时清空计划并暂停，恢复后重新建立未来基线。scheduler 的 `last_error` 由 `/签到 我的` 和 `/签到 状态` 输出提示；配置修复后或重载插件后从后续计划继续，内部错误导致的 worker 退出需要重载恢复。关闭时取消 worker，必须先于队列关闭。

### 终态通知

`notifications.enqueue_terminal(snapshot, item, result)` 与终态批次在同一修改函数中填充 `outbox[run_id]`，记录安全文本、身份、来源、是否手动参与和固定结果字段。所有被准入的用户在入队时通过 `reserve_terminal(snapshot, item)` 创建 `phase=reserved` 占位；终态时填入同一记录。

发送判断在终态保存 outbox 时进行：来源为 `manual` 或 `manual_requested=true` 时必发；否则业务状态不是 `success`、`already_signed` 时发送；否则读取当时的本人绑定 `notify_all`，为真时发送。保存后、发送前修改偏好不会重算已保存通知。成功自动结果不发送时使用 `phase=suppressed`，不进入发送队列，也不重试；该记录视为已结算，可按历史保留策略清理。手动加入自动批次后，通知按手动参与渲染并强制发送。绑定在终态保存前被删除时按仅失败处理。

`MAX_OUTBOX_ITEMS` 为 1000；`terminal_capacity(snapshot)` 供队列在入队时检查槽位。

`NotificationService(store, send)` 在外部发送前先持久化 `phase=sending`：`send` 返回 `True` 记为 `sent`，返回 `False` 记为 `pending` 并在上限内重试，重试用尽后记为 `failed`，异常或取消记为 `unknown`，不盲目重发。默认最多 3 次尝试，重试窗口 30 秒。KV 写入失败时停止发送，但不重跑签到。通知发送失败不改变批次中的业务终态，`/签到 状态` 仍可查询，抑制状态显示为“按偏好静默”。

### 接口依据与许可

Neo 接口按 [Shipyard Neo `f4d248b4274c1a2f05402f75a3b39e5c19be9ff5`](https://github.com/AstrBotDevs/shipyard-neo/tree/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5) 核验，该项目使用 AGPLv3；本插件客户端和 wrapper 为独立实现，没有移植上游代码。相关依据：

- [Bay API v1](https://github.com/AstrBotDevs/shipyard-neo/blob/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5/doc/bay_api_v1.md)：请求、响应与文件路径。
- [CargoManager](https://github.com/AstrBotDevs/shipyard-neo/blob/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5/pkgs/bay/app/managers/cargo/cargo.py) 与 [Docker driver](https://github.com/AstrBotDevs/shipyard-neo/blob/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5/pkgs/bay/app/drivers/docker/docker.py)：删除引用指向的数据后才删 Cargo 记录；bind 模式必须保证该宿主路径在 Bay 内仍指向真实 Cargo，不能只检查 API 404。
- [K8s driver](https://github.com/AstrBotDevs/shipyard-neo/blob/f4d248b4274c1a2f05402f75a3b39e5c19be9ff5/pkgs/bay/app/drivers/k8s/k8s.py)：PVC 删除受理不等于清理完成，因此当前执行层仅接受已确认的固定 Docker 部署。
- [AstrBot v4.29.4 Context](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/star/context.py)：通过 `get_config()` 读取框架配置，不访问框架私有存储或借用聊天资源。
- [AstrBot v4.29.4 astrbot_config](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/config/astrbot_config.py)：管理员停用只调用公共异步方法 `save_config_async(replace_config)`；该方法先合并替换值，再以原子文件替换保存，返回 `False` 表示保存被更新提交取代。

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

执行层回归位于 `tests/test_neo_client.py`、`tests/test_neo_executor.py`、`tests/test_neo_lifecycle.py` 和 `tests/test_bootstrap.py`。制品构建与准备服务回归位于 `tests/test_preparation.py` 和 `tests/test_artifact_service.py`；管理员准备指令位于 `tests/test_admin_preparation.py`，schema 迁移与写入确认位于 `tests/test_state.py`、`tests/test_storage.py`。签到队列、恢复和容量位于 `tests/test_execution_queue.py`，自动计划准入、去重和生命周期位于 `tests/test_automatic_execution.py`，终态通知位于 `tests/test_notifications.py`，聊天入口和集成位于 `tests/test_manual_execution.py`，管理员取消位于 `tests/test_admin_cancellation.py`，管理员停用位于 `tests/test_admin_disablement.py` 和 `tests/test_task_control.py`。`tests/neo_support.py` 是受控 Bay 替身，会在临时目录运行本仓库 wrapper 与假脚本；这不是产品的宿主回退路径。本地未安装 Node 时对应测试会明确跳过；CI 显式安装并检查 Node，不依赖 runner 预装。macOS 测试只将 Node 的可执行路径映射到本机安装位置，实际运行 Node 子进程。

离线 smoke 通过真实 loopback HTTP 连接受控 Bay，覆盖两用户隔离、非零退出码的显式业务结果、超时、清理阻断及恢复不重跑。真实 Neo 假凭据测试与真实账号测试分别记录如下。

### 手动签到本地验收

- Python 3.14 下全量 390 项测试通过；本轮代码和相关测试通过 Ruff 检查。
- 独立 smoke 使用实际 `/签到 立即` 入口、原生 KV 替身和加密假凭据，经队列、NeoClient、受控 Bay 到实际 wrapper 与假脚本，通过真实 loopback HTTP 发出 37 次请求。两用户分别得到 `success` 和 `already_signed`，最大并行 1，业务请求 2 次；重复手动请求返回同一 run_id；终态分别投递到两个来源，未串用。Sandbox 和 Cargo 残留均为 0，`cleanup_jobs` 为空；KV、回执和 Bay history 未出现假密码。
- 未连接真实 QQ、生产 Neo 或云盘账号。
- 单测另覆盖排队后凭据更新、暂停和撤权、KV 写入失败不分配、通知异常不重跑、终止和重载不重跑；随机、固定和零间隔，由最近全局完成记录驱动的任务和用户间隔及不叠加，排队过期，历史保留和配额由队列回归覆盖。
- 新增回归在修复前复现失败、修复后通过：暂停、替换凭据和未确认暂停均不产生凭据上传或业务请求；未发送业务请求时不写入冷却或全局间隔；终止时无需重载即可发送终态。

### 自动签到本地验收

- Python 3.14 下全量 420 项测试通过，耗时 18.639 秒；包含三个审查缺陷的失败前/修复后回归，以及 11:01 不补跑边界。本轮源码和执行相关测试通过 Ruff 检查。旧 `tests/test_state.py`、`tests/test_storage.py` 的既有严格风格告警未做无关格式重写，不声明全仓 Ruff 全绿。
- 独立 smoke 使用实际调度器和队列入口，经真实 loopback HTTP 连接受控 FakeBay，运行实际 wrapper 和假天翼制品：68 次 HTTP 请求，1 个定时批次，4 个用户业务执行，最大并发 1。业务结果依次为 `success`、`success`、`failed`、`already_signed`，通知状态依次为 `sent`、`suppressed`、`sent`、`sent`。
- 同轮覆盖：第一个自动 run 被手动请求合并并复用 run_id，强制反馈；第二个默认成功静默；第三个失败发送通知；第四个在 `notify_all` 开启后已签到仍发送。两个平台来源分别投递，未串用。
- 全量测试另覆盖同任务重叠跳过、准入提交屏障、到期分钟内重载不补跑、事件循环延迟后只准入当前命中分钟、错过分钟不补跑、关闭后重开从未来计划开始、暂停和撤权过滤、缺少制品以 `config_error` 结束、时钟回拨不重放、时区日和星期 OR 及周日 `7`、schema 2 升级后仅未开始的手动请求继续执行。
- Sandbox、Cargo、宿主物理目录和 `cleanup_jobs` 均为 0；假凭据 canary 未出现在快照、Bay 历史或回执中。
- 该 smoke 的 Bay 是受控替身，不是真实 Neo 部署；未连接真实 QQ 或云盘账号。

### 管理员取消本地验收

- Python 3.14 下全量 430 项测试通过，耗时 18.700 秒；包含管理员取消回归。scoped Ruff 检查通过，范围为 `main.py`、`execution_queue.py`、`execution_bridge.py`、`notifications.py` 和新增取消测试；不声明全仓 Ruff 全绿。
- 新增真实 wrapper 脚本 SHA 失配回归：结果为可靠的 `start_error/config_error/runner_error`。清理期间管理员取消时，修复前会被误改为 `cancelled`；修复后保留 `config_error` 并附加 `admin_cancelled` 原因，终态通知使用同一结果。
- 独立 smoke 使用实际队列和执行入口，经真实 loopback HTTP 连接受控 FakeBay，运行实际 wrapper 和假脚本，共发出 32 次 HTTP 请求。取消对象得到 `cancelled`，另一用户得到 `already_signed`；取消对象没有上传凭据，也没有业务执行；后续用户只执行 1 次业务，最大并发为 1。
- 重复取消同一 run_id 不增加 revision。终态按来源投递到原用户和第二用户各自的 owner 来源（跨平台），没有投递给管理员。
- Sandbox、Cargo、宿主物理目录和 `cleanup_jobs` 均为 0；假凭据 canary 未出现在 KV、Bay 历史或回执中。
- 未连接真实 Neo、真实 QQ 或云盘账号。

### 管理员停用本地验收

- Python 3.14 下全量 453 项测试通过，耗时 23.059 秒；`main.py`、`execution_queue.py`、`execution_bridge.py`、`scheduler.py`、`task_control.py`、`tests/astrbot_support.py`、`tests/test_task_control.py` 和 `tests/test_admin_disablement.py` 通过 scoped Ruff 检查，不声明全仓 Ruff 全绿。
- 新增删除停用意图前等待状态锁期间重新启用的回归：修复前命令未抛错且停用意图被删除；修复后抛出 `TaskControlSaveUnconfirmed`，持久化停用意图保留。
- 独立 smoke 使用 AstrBot v4.29.4 原始 `save_config_async`、`_prepare_config_snapshot` 和 `_write_config_snapshot`，在临时配置文件执行真实写入、fsync 和 replace；同轮经真实 loopback HTTP 连接受控 FakeBay 和实际 wrapper，共 35 次请求。
- 真实目录路径错误导致配置保存失败时，KV 停用意图保留，目标没有上传凭据、没有执行业务并以取消结束；修复路径后重试保存 `enabled=false` 并清除停用意图；从实际配置文件重载后目标继续禁用。另一任务得到 `already_signed`，执行 1 次业务，最大并发 1。
- 绑定和密文保持不变，两个 owner 来源分别投递；Sandbox、Cargo、物理目录和 `cleanup_jobs` 均为 0，假凭据 canary 未出现在快照、Bay 历史或回执中。
- 该 smoke 使用受控 FakeBay 和临时配置文件，不是完整 AstrBot/QQ 部署；未连接真实 Neo、真实 QQ 或云盘账号。

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
