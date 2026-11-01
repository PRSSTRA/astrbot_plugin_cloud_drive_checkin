# 云盘签到助手

AstrBot 插件，在普通 QQ 私聊中查询云盘签到任务、预览计划，并录入任务所需凭据。当前版本保存绑定和加密凭据，但不执行签到。

## 功能

- 查询帮助、任务列表、任务详情和未来三次计划时间。
- 通过私聊向导逐个录入任务字段，凭据认证加密后保存。
- 查看本人已绑定的任务 ID，以及每个任务的凭据是否已配置。
- 当前版本不下载脚本，不创建定时器，不访问云盘，也不执行签到。任务显示“配置启用”只表示配置开关为真。

## 环境要求

- AstrBot `>=4.29.4,<5`。
- Python 3.12 或更高版本。
- 通过 aiocqhttp 适配器接入的 OneBot v11 实现，例如 NapCat；消息上报格式使用 `array`。
- 当前为 v0.0.1 开发版本，尚未发布正式版本。

## 安装

当前代码位于 `dev/0.0.1` 开发分支。本文描述的功能以该分支为准，仓库默认分支或其他安装来源不保证包含这些功能。

```bash
cd /path/to/AstrBot/data/plugins
git clone --branch dev/0.0.1 https://github.com/PRSSTRA/astrbot_plugin_cloud_drive_checkin.git astrbot_plugin_cloud_drive_checkin
```

然后在 AstrBot WebUI 的插件页确认插件已加载并打开。运行依赖在 `requirements.txt` 中声明；如果 AstrBot 没有自动安装，请在 AstrBot 实际使用的 Python 环境中安装。加载失败时查看日志，修复后重载插件。

录入凭据前需要部署主密钥 `CLOUD_DRIVE_CHECKIN_MASTER_KEY`。没有主密钥时，帮助、任务查询和本人状态仍可使用，但录入会被拒绝。生成、设置和备份步骤见 [配置说明](docs/configuration.md)。

## 快速上手

1. 使用 AstrBot 管理员账号私聊机器人，或把普通用户 QQ 加入 `whitelist_qqs`。
2. 按 AstrBot 当前配置的唤醒前缀发送指令。默认前缀是 `/`，发送 `/签到` 或 `/签到 帮助`。
3. 回复会显示用法、角色、服务开关和状态存储状态。

要开始录入，发送 `/签到 录入 <任务ID>`，例如 `/签到 录入 tianyi_cloud`。已有任务包括配置禁用的任务都可以录入，录入本身不会执行任务。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/签到`、`/签到 帮助` | 显示用法、当前角色、服务开关和状态存储状态。 |
| `/签到 任务 [任务ID]` | 不带 ID 时列出任务；带 ID 时显示字段要求、Cron、时区、未来三次计划时间和配置版本。 |
| `/签到 录入 <任务ID>` | 开始 5 分钟字段录入。 |
| `/签到 我的` | 显示本人全部绑定。 |
| `/签到 状态 [任务ID]` | 显示本人绑定；带 ID 时只看该任务。 |

`/签到 我的` 和 `/签到 状态` 只显示本人任务 ID 和“凭据已配置”或“凭据未配置”，不显示字段值、密文，也不显示字段 ID 清单。

## 凭据录入

- 录入限时 5 分钟，每条消息作为一个字段输入。只接受纯文本，图片、引用和文件会被拒绝。字符串按原文保存，不做裁剪。
- 可选字段可以发送 `/跳过`，必填字段不能跳过。全部字段完成后发送 `/确认` 保存凭据；字段未完成时不会保存。
- 发送 `/取消` 放弃本次录入并清除未提交内容。
- 如果字段值正好是 `/取消`、`/确认`、`/跳过`，或以 `/值 ` 开头，发送 `/值 原文`。插件只去掉一次最前面的 `/值 `。
- 向导期间的消息不会交给 LLM。超时或插件重载后，未完成的录入仍会拦截你的后续私聊文本，避免密码误发给模型；发送 `/取消` 退出后，普通聊天才会恢复，然后再重新开始录入。
- 录入内容会作为 QQ 消息传输。QQ、OneBot 实现或框架的入站记录可能保留这些消息，插件无法清除。不能接受这一风险时，不要录入密码或令牌。
- 存储提交未确认时，插件不会报告保存成功。请让管理员恢复存储后查询本人状态；仍被向导拦截时发送 `/取消` 退出。不要重复提交。

## 权限

只处理普通 QQ 好友私聊。群聊、临时会话、其他平台以及平台或 QQ 身份字段矛盾的事件会被静默忽略，管理员也不豁免。

| 身份 | 获得方式 | 可用指令 |
| --- | --- | --- |
| AstrBot 管理员 | 宿主框架管理员 | 全部查询和本人录入 |
| 附加管理员 | 加入 `plugin_admin_qqs` | 全部查询和本人录入 |
| 白名单用户 | 加入 `whitelist_qqs` | 全部查询和本人录入 |
| 其他用户 | 无 | 收到无权提示 |

普通用户白名单为空时，所有非管理员用户都会被拒绝。管理员不需要加入白名单。权限配置在每条指令到达时重新读取，撤销后对后续指令立即生效。

## 配置

在 AstrBot WebUI 的插件配置页维护 `enabled`、`plugin_admin_qqs`、`whitelist_qqs`、`timezone`、`timeout_seconds` 和 `tasks`。`enabled` 当前只影响帮助中显示的服务状态，不拦截查询，也不会启动签到。

内置模板为 `tianyi_cloud`（天翼云盘）和 `mobile_cloud_experimental`（移动云盘，实验），默认禁用。模板只在未配置 `tasks` 时提供，显式设置 `tasks=[]` 不会补回。移动模板必须保持 `enabled=false` 和 `experimental=true`。

完整配置表、任务字段、Cron、脚本来源、适配器和主密钥部署步骤见 [配置说明](docs/configuration.md)。

## 数据与安全

业务状态保存在 AstrBot 异步 KV 中，插件不创建数据库或状态文件，只支持单进程、单实例运行。

所有录入字段值都会认证加密后保存，主密钥与密文分开。插件只从环境变量读取主密钥，不生成、不打印；聊天回复不展示凭据值或密文。请保管原主密钥，不要直接更换；当前不提供自动密钥迁移，密钥丢失后需要重新录入。

## 许可证与来源

本仓库以 [AGPLv3](LICENSE) 发布。

- 基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件接口，AstrBot 以 AGPLv3 发布。
- [croniter 6.2.4](https://pypi.org/project/croniter/6.2.4/) 用于 Cron 计算，许可证为 MIT。
- [cryptography 50.0.2](https://pypi.org/project/cryptography/50.0.2/) 用于凭据认证加密，许可证为 Apache-2.0 或 BSD-3-Clause。
- 天翼模板引用 [BlueSkyXN/189Cloud-Checkin 的固定提交](https://github.com/BlueSkyXN/189Cloud-Checkin/tree/8e02d162ec6d1b5c00736afffb727d9dcfe8ba7b)，移动模板引用 [smallfawn/QLScriptPublic 的固定提交](https://github.com/smallfawn/QLScriptPublic/tree/2d4c26fdc72c3206bb9cf9afc1fb4991d337f888)。脚本本身不在本仓库中。

## 参与开发

开发环境、测试命令、状态存储接口、Git hooks、分支和 PR 规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。
