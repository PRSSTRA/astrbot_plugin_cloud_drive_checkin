# 配置说明

本文说明权限、主密钥、凭据录入、绑定管理和任务目录。当前版本校验和预览任务，录入并加密保存凭据，可暂停本人绑定、调整自动通知偏好或删除绑定；不下载脚本，不执行签到，也不发送结果通知。用户指令和快速上手见 [README](../README.md)。

## 权限设置

| 字段 | 默认值 | 规则 |
| --- | --- | --- |
| `enabled` | `false` | 服务开关；当前只影响帮助中显示的状态，不拦截查询，也不执行签到 |
| `plugin_admin_qqs` | `[]` | 附加插件管理员 QQ；AstrBot 管理员默认拥有管理员权限 |
| `whitelist_qqs` | `[]` | 普通用户白名单；空列表不放行普通用户 |

QQ 号必须是正整数，或由数字组成、不含空白、符号和前导零的字符串。白名单为空时，所有非管理员用户都会被拒绝。管理员不需要加入白名单。

权限只在普通 QQ 好友私聊中生效。群聊、临时会话、其他平台以及平台或 QQ 身份字段矛盾的事件会被静默忽略，管理员也不豁免。权限配置在每条指令到达时重新读取。

## 全局设置

| 字段 | 默认值 | 规则 |
| --- | --- | --- |
| `timezone` | `Asia/Shanghai` | IANA 时区名称，用于计算未来计划时间；名称必须能被宿主的 `zoneinfo` 识别 |
| `timeout_seconds` | `180` | 全局任务超时，允许 1 到 240 秒；任务中的 `0` 表示继承此值 |
| `tasks` | 内置模板 | 任务数组；未设置时使用内置模板，显式设为 `[]` 时任务列表为空 |

任务目录最多 32 个任务。`tasks` 不是数组、任务不是对象、任务 ID 重复或任一任务校验失败时，整份任务目录被视为无效。加载阶段发现无效会导致插件加载失败；运行期间发现无效时，插件拒绝处理指令并返回固定提示。

## Neo 连接

尚未部署 Neo 时保持 `neo_connection.enabled=false`。查询、录入和绑定管理不需要 Neo。启用连接只会初始化内部执行服务并恢复遗留资源清理，当前没有聊天执行指令、定时 worker 或结果通知发送器。连接配置无效、鉴权失败或恢复记录不匹配会导致插件初始化失败；先修正配置，不要删除业务状态。

`neo_connection` 的字段如下：

| 字段 | 默认值 | 规则 |
| --- | --- | --- |
| `enabled` | `false` | 是否连接 Neo 并在启动时恢复清理 |
| `docker_cleanup_confirmed` | `false` | 管理员在目标部署独立验证 Cargo 物理删除后才能设为 `true`；仅确认 Docker 版本或 API 404 不足以放行 |
| `endpoint` | 空字符串 | 备用 Bay API 地址；必须明确填写 HTTP 或 HTTPS 地址，不含用户名、密码、查询参数或片段 |
| `profile` | `python-default` | 备用 profile，须提供 shell 和 filesystem 能力，镜像中须有 Python；Node 脚本还需要 Node |
| `access_key_env` | `CLOUD_DRIVE_CHECKIN_NEO_KEY` | 备用访问密钥的环境变量名，只填写名称，不填写密钥值 |
| `ttl` | `900` | 本插件沙箱 TTL，允许 300 到 3600 秒；不继承聊天沙箱 TTL |

启用后，插件通过 AstrBot 公共 `Context.get_config()` 读取 `provider_settings.sandbox`。当 `booter=shipyard_neo`，且 `shipyard_neo_endpoint`、`shipyard_neo_access_token` 均已明确配置时，优先使用这一完整连接和 `shipyard_neo_profile`。框架连接不完整、地址为空或为 `__auto__` 时，改用插件的完整备用连接，不把一个来源的地址与另一个来源的密钥拼在一起。插件不读取 Neo 的 `credentials.json`，不启动 Bay，也不获取聊天沙箱。

访问检查使用受保护的沙箱查询入口，查询一个不会由 Neo 自动生成的探测 ID：未带密钥须返回 401，带密钥须返回 Neo 的 `not_found`，随后读取公开 profile 目录确认目标能力。不会读取或借用聊天资源；匿名访问未关闭时拒绝执行。HTTP 仅用于可信的本机或隔离内部网络；跨不可信网络应使用 HTTPS。插件不自动修改服务鉴权、网络、权限或镜像。

### Docker 清理条件

Neo 提交 `f4d248b4274c1a2f05402f75a3b39e5c19be9ff5` 的 Docker bind 实现使用宿主绝对路径创建和删除 Cargo。如果该路径在 Bay 容器内指向另一处目录，就可能只删除容器层的空目录，而宿主 Cargo 数据仍在；此时 API 也会返回 `not_found`。因此不能仅凭该版本、Docker 类型或 API 404 认定物理清理成功。

使用 bind Cargo 时，宿主 Cargo 根目录必须以同一绝对路径挂入 Bay，并确认它与 Bay 配置中的 Cargo 根目录指向同一实际存储。保留原有挂载并增加同路径映射是已验收的一种做法。部署或升级后，先用独立假凭据 canary 验证 API 删除和宿主目录删除都成功，再设置 `docker_cleanup_confirmed=true`。命名卷模式也须独立验证，不能用 bind 模式结果代替；当前真实验收仅覆盖路径对齐后的 bind 模式。

资源创建至清理期间须保持同一 Docker daemon、存储配置和鉴权身份。不能凭 Cargo 的 `backend` 字段自动判断拓扑，该版本把它固定写为 `docker_volume`。插件不会自动更改挂载或重建 Bay；这些部署修改需要操作者授权。

每次执行都创建独立 Sandbox 和托管 Cargo。清理只使用本插件已经记录的资源 ID，检查 Cargo 的管理沙箱，删除 Sandbox 后再查询 Cargo；仍有残留时只重试该 Cargo 的删除，并要求查询返回 Neo 的 `not_found`。不会调用全局 GC、按创建时间猜测归属或删除聊天资源。这里验证的是资源删除，不是存储介质安全擦除，也不代表 Bay 历史记录已删除。

Kubernetes 不适用上述条件：PVC 删除受理后可能仍处于 Terminating，Cargo 记录却已经消失。该模式不能勾选 Docker 清理确认。

### 中断和清理失败

创建请求结果不明时不会重发创建请求；业务执行超时或断连也不会重发。资源 ID 不明、清理未确认或连接指纹变化时保留 `cleanup_jobs`，阻断新执行。连接指纹覆盖地址、密钥和 profile，不保存这些密钥的明文；存在待清理资源时不要切换连接，应先用原连接完成清理。

启动恢复只清理已记录资源，不补跑业务。仍处于初始接单阶段且没有资源 ID 的记录，可以确定尚未尝试分配并记为取消；已经提交创建意图的未完成记录按 `interrupted/unknown` 处理。创建结果不明且未取得资源 ID 时需要操作者核查，不能通过清空 KV 绕过。KV 写入未确认时仍遵守原快照重试规则，清理结果不能用来覆盖待确认写入。

本地取消不等于远端业务未发生。业务请求可能已经发出、但未收到有效结果时，终态为 `interrupted/unknown`；只有尚未发送业务请求，或 wrapper 明确返回取消结果时，才记录确定的业务取消。创建和执行意图的 KV 写入完成后，还会再次同步检查授权，避免写入等待期间撤权后继续执行。

已在路径对齐的 Docker Neo 上完成真实假凭据验收，范围和结果见 [贡献指南](../CONTRIBUTING.md#真实-neo-假凭据验收)。该验收的 Star/KV 边界为受控内存适配器，不代表真实 AstrBot/QQ 或真实账号签到已经通过；新部署仍须单独验证清理条件。

## 凭据主密钥

只有录入凭据需要主密钥。帮助、任务查询、`/签到 我的`、`/签到 状态`，以及暂停、恢复、通知和删除等绑定管理操作都不读取主密钥。

| 项目 | 要求 |
| --- | --- |
| 环境变量 | `CLOUD_DRIVE_CHECKIN_MASTER_KEY` |
| 值 | 32 字节随机密钥的 URL-safe Base64 编码，按标准带填充 |
| 校验 | 必须能严格解码为 32 字节，重新编码后要与原值完全一致；其他长度或非规范 Base64 都视为无效 |
| 读取时机 | 每次开始录入时从进程环境读取 |

Linux/macOS 可在可信终端执行下面的命令，将新密钥写入独立环境文件。命令不显示密钥，不覆盖已有文件：

```bash
python3 - <<'PY'
import base64
import os
from pathlib import Path

path = Path.home() / ".config" / "cloud-drive-checkin" / "master-key.env"
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    stream.write("CLOUD_DRIVE_CHECKIN_MASTER_KEY=" + key + "\n")
print("已创建密钥文件：", path)
PY
```

通过现有服务管理器把这个文件中的环境变量注入 AstrBot 进程，例如 systemd 的 `EnvironmentFile` 或 Docker Compose 的 `env_file`。文件应与 AstrBot 数据备份分开保管，不能提交到仓库。需要重启才能更新进程环境时，由部署管理员确认并操作；插件不会修改服务配置或重启服务。

- 插件不生成、不打印主密钥，也不从插件配置读取密钥。
- 不要写进任务配置、`fixed_env`、QQ 消息、聊天记录、仓库或日志。
- 自行备份并保管原主密钥，不要直接更换。当前没有自动密钥迁移；丢失原密钥后，需要重新录入。
- 主密钥缺失或无效时，录入返回固定提示并拒绝开始，不会创建待录入记录；其他查询继续可用。

## 凭据录入

- `/签到 录入 <任务ID>` 可以录入任意已配置任务，包括 `enabled=false` 的任务。录入本身不执行任务。
- 整段录入限时 5 分钟。每条消息作为一个字段输入，插件只接受全部由文本段组成的私聊消息；图片、引用、文件和其他非文本段会被拒绝。
- 字符串按原文保存，不裁剪空白、引号或换行。整数使用十进制写法，可以带正负号，不能包含空白。布尔值输入 `true` 或 `false`，不区分大小写。
- 必填字符串不能为空。含 NUL 的值会被拒绝。录入时按 UTF-8 文本计算大小，单字段不超过 16 KiB，全部字段合计不超过 64 KiB。
- 可选字段可以发送 `/跳过`，必填字段不能跳过。全部字段完成后发送 `/确认` 保存；未完成时发送 `/确认` 不会提交。
- 发送 `/取消` 放弃本次录入并清除未提交内容。
- 字段值正好是 `/取消`、`/确认`、`/跳过`，或以 `/值 ` 开头时，发送 `/值 原文`。插件只去掉一次最前面的 `/值 `。
- 超时或插件重载后，未完成录入仍会拦截该用户的后续私聊文本，避免内容交给 LLM；发送 `/取消` 后才能退出并恢复普通聊天。
- 存储提交未确认时，插件不会报告保存成功。请管理员恢复存储后查询本人状态，不要重复提交。
- 录入和保存成功后只建立绑定并保存加密凭据，不开始签到。

`/签到 我的` 和 `/签到 状态 [任务ID]` 列出本人任务 ID、凭据是否已配置、是否暂停和自动通知偏好。它们不显示字段值、密文或字段 ID 清单。没有匹配绑定时返回“没有符合条件的本人绑定。”。

## 绑定管理

暂停、恢复、通知和删除只修改本人绑定，不改变任务的 `enabled`，也不影响其他用户。任务从任务目录移除后，原有绑定仍会保留，可以继续按任务 ID 管理或删除。

`/签到 暂停 <任务ID>` 和 `/签到 恢复 <任务ID>` 修改本人该任务的暂停标记。当前版本不执行签到，标记只保存为本人偏好，暂不影响实际运行。`/签到 通知 <任务ID> 开|关` 保存自动通知偏好，`开` 表示希望接收全部自动结果通知，`关` 表示只接收自动失败通知。当前版本不执行任务，也不发送结果通知，这里只保存偏好。新绑定默认未暂停，通知偏好为 `关`。

删除需要两步确认：

1. `/签到 删除 <任务ID>` 创建 60 秒有效的确认请求，只保存在运行内存中，不修改已保存的绑定。
2. 在 60 秒内发送 `/签到 删除 <任务ID> 确认`，才会删除该绑定和凭据密文。发送 `/签到 删除 <任务ID> 取消` 放弃请求，不修改绑定。

确认请求只对发起时的平台、QQ 和任务有效，只能使用一次，插件重载后失效。超过 60 秒，或该绑定在确认前发生变化（例如重新录入凭据、修改暂停状态或通知偏好），原来的确认会失效，需要重新发起。其他用户或其他绑定的变化不会使该确认失效。

删除只移除插件当前保存的绑定和凭据密文，不删除 QQ、OneBot 实现或框架的入站记录，也不清理管理员制作的备份。任务的 `enabled` 和其他用户的绑定不受影响。存储提交未确认时，插件不会报告删除成功；确认请求在尝试提交后也会失效。请让管理员恢复存储后再查询 `/签到 状态`，不要重复提交。

录入向导尚未退出时，先发送 `/取消`，再执行绑定管理指令。

## 任务字段

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `task_id` | string | 小写字母开头，后续只能是小写字母、数字或下划线，总长 1 到 64；同一目录内不可重复 |
| `name` | string | 非空任务名称 |
| `enabled` | bool | 配置启用状态，默认 `false`；当前版本即使为 `true` 也不执行 |
| `runtime` | string | `python` 或 `node` |
| `script_url` | string | 固定提交的 HTTPS GitHub 脚本地址，规则见下文 |
| `source_commit` | string | 40 位十六进制提交；必须与 `script_url` 中的提交一致 |
| `sha256` | string | 64 位十六进制预期内容摘要；当前只做静态校验 |
| `cron` | string | 五段 Cron 表达式，规则见下文 |
| `user_fields` | array 或 JSON 文本 | 用户字段元数据数组；WebUI 中使用 JSON 文本编辑器 |
| `fixed_env` | object | 固定的非秘密环境变量，名称到字符串值的映射 |
| `input_adapter` | string | `direct_env`、`tianyi_account` 或 `mobile_single_account` |
| `result_adapter` | string | `json_protocol`、`process_only`、`tianyi` 或 `mobile_unverified` |
| `dependency_bundle` | string | 依赖包声明，可以为空；非空不代表依赖已经准备完成 |
| `timeout_seconds` | int | `0` 继承全局值；其他值允许 1 到 240 |
| `experimental` | bool | 实验任务标记，默认 `false` |

`user_fields` 只描述字段要求。实际值只在 `/签到 录入` 过程中接收，认证加密后保存；任务配置中不要填写字段值。`secret` 目前用于字段声明和展示，所有录入字段值都会加密保存。

### 用户字段

`user_fields` 的每一项包含以下键：

| 键 | 类型 | 规则 |
| --- | --- | --- |
| `field_id` | string | 与 `task_id` 相同的标识符规则；同一任务内不可重复 |
| `label` | string | 非空的显示名称 |
| `env_name` | string | 合法 ASCII 环境变量名，保留声明的大小写 |
| `value_type` | string | `string`、`int` 或 `bool` |
| `required` | bool | 是否必填 |
| `secret` | bool | 是否标记为秘密字段 |
| `hint` | string | 可省略的提示文本 |

环境变量名必须匹配 `[A-Za-z_][A-Za-z0-9_]*`。用户字段之间不能有仅大小写不同的重名，用户字段和 `fixed_env` 之间也不能重叠，比较时不区分大小写。以下名称不可使用：

```text
PATH HOME SHELL ENV BASH_ENV IFS
PYTHON* NODE_* LD_* DYLD_*
```

`user_fields` 既可以直接传数组，也可以传 UTF-8 JSON 文本。JSON 文本上限为 64 KiB，嵌套深度最多 32 层，不接受 `NaN` 等非标准 JSON 常量。一个任务最多 32 个用户字段。

下面是从天翼模板摘出的字段声明，只描述元数据，不是凭据值：

```json
[
  {
    "field_id": "username",
    "label": "用户名",
    "env_name": "TY_USERNAME",
    "value_type": "string",
    "required": true,
    "secret": true,
    "hint": "天翼云盘登录用户名"
  }
]
```

## 脚本来源

`script_url` 只接受两种形式：

```text
https://github.com/<owner>/<repo>/blob/<commit>/<path>
https://raw.githubusercontent.com/<owner>/<repo>/<commit>/<path>
```

GitHub 页面地址会规范化为 `raw.githubusercontent.com` 形式。URL 必须使用 HTTPS，提交必须是 40 位十六进制，并且与 `source_commit` 相同。以下内容会被拒绝：

- URL 中的用户名、密码、查询参数或片段；
- 非标准端口，443 可以省略或显式写出；
- 空路径段、`.`、`..`、反斜杠和经过过度百分号编码的路径；
- `github.com` 上不是 `blob` 的地址，或缺少脚本路径的地址。

当前只做静态校验，不下载脚本。`sha256` 是声明值，不能证明远端内容已经核对。

## Cron

Cron 表达式固定为五段：分钟、小时、日、月、星期。支持：

- `*` 和数字；
- 月份和星期的英文简称，例如 `JAN`、`MON`；
- 逗号列表，例如 `1,15`；
- 正向范围，例如 `1-5`；
- 步长，例如 `*/10` 或 `1-30/5`。

星期允许 0 到 7，0 和 7 都表示周日；月份允许 1 到 12。日和星期同时受限时按 OR 计算，所以 `0 9 31 2 MON` 仍然有效，因为二月的周一满足星期条件。`@daily`、秒字段、年字段、随机 `H`/`R` 以及 `L`、`W`、`#` 扩展不支持。

插件要求表达式能产生至少三次未来运行时间。无法产生未来日期的表达式会被拒绝。预览时间按 `timezone` 计算并带 UTC 偏移。

## 适配器组合

| 输入适配器 | 运行时 | 结果适配器 | 额外要求 |
| --- | --- | --- | --- |
| `direct_env` | 任意 | `json_protocol` 或 `process_only` | 无 |
| `tianyi_account` | `python` | `tianyi` | 必须有 `username`、`password` 字段，类型为 `string`，且 `required=true`、`secret=true` |
| `mobile_single_account` | `node` | `mobile_unverified` | 必须有 `authorization`、`phone`、`auth_token` 字段，类型为 `string`，且 `required=true`、`secret=true` |

`tianyi` 结果适配器只能与 `tianyi_account` 搭配，`mobile_unverified` 只能与 `mobile_single_account` 搭配。

以下任一条件成立时，任务必须保持 `enabled=false` 且 `experimental=true`：

- `task_id` 为 `mobile_cloud_experimental`；
- `input_adapter` 为 `mobile_single_account`；
- `result_adapter` 为 `mobile_unverified`。

否则配置校验失败。

## 内置模板

默认任务只在未配置 `tasks` 时提供，统一维护在 `_conf_schema.json`。显式设置 `tasks=[]` 不会补回模板。

| 任务 ID | 名称 | 计划 | 运行时 | 适配器 | 字段 | 默认状态 |
| --- | --- | --- | --- | --- | --- | --- |
| `tianyi_cloud` | 天翼云盘 | 每日 07:10 | `python` | `tianyi_account` / `tianyi` | `username`、`password` | 禁用 |
| `mobile_cloud_experimental` | 移动云盘（实验） | 每日 07:20 | `node` | `mobile_single_account` / `mobile_unverified` | `authorization`、`phone`、`auth_token` | 禁用，实验状态 |

天翼模板引用 [BlueSkyXN/189Cloud-Checkin 的固定提交](https://github.com/BlueSkyXN/189Cloud-Checkin/tree/8e02d162ec6d1b5c00736afffb727d9dcfe8ba7b)，移动模板引用 [smallfawn/QLScriptPublic 的固定提交](https://github.com/smallfawn/QLScriptPublic/tree/2d4c26fdc72c3206bb9cf9afc1fb4991d337f888)。本仓库没有移植这些脚本。准备启用前需要自行核实许可、依赖和实际行为。

移动任务必须保持禁用和实验状态。其他任务即使配置为启用，当前版本也不会执行。

## 配置版本

任务详情中的“配置版本”是规范化任务内容的 SHA256 指纹。版本包含时区和任务字段，`__template_key`、对象键顺序以及等价的 JSON 表示不影响版本。修改任务配置会使正在进行的录入失效，需要重新开始；已保存的凭据不会因此自动替换。

名称、字段顺序、字段元数据、脚本声明、时区或有效超时等业务配置变化会改变版本。版本用于识别任务配置是否变化，不是脚本内容摘要；脚本摘要单独由 `sha256` 声明。
