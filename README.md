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

## 提交和 PR 标题

提交标题和 PR 标题使用 `类型(scope): 描述`，例如 `构建(ci): 添加 PR 检测工作流`。括号和冒号须为半角；scope 使用小写英文字母或数字开头，后续可包含小写英文字母、数字及 `.`、`_`、`/`、`-`；描述须至少包含一个中文汉字，可以夹带英文和数字。校验需要 Python 3：如果 `PATH` 中没有 `python3`，钩子会提示安装并阻止提交。

新克隆仓库后执行 `git config core.hooksPath .githooks` 启用本地钩子。此设置仅保存在当前仓库的本地 Git 配置中，不会随仓库共享。钩子禁止直接在 `main`、`master`、`develop` 和 `dev/*` 上提交；其他分支命名不符合规范时只发出警告。本地合并提交不受分支限制。

PR 检查流程验证：标题与提交信息均通过校验后再合并。
