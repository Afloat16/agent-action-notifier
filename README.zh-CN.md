# Agent Action Notifier

**需要你参与时，及时收到通知，不必一直盯着智能体对话。**

[English](README.md) · [接入说明](docs/integrations.md) · [桌面限制](docs/desktop.md) · [安全说明](SECURITY.md)

Python 3.10+ · MIT · 无第三方运行时依赖 · 0.1.0

当智能体需要确认、授权、登录或回答问题时，它可能已经等待很久，而你以为工作仍在进行。
本工具将工作流明确上报的事件保存到 SQLite，并通过邮件和本机桌面发送通知。
待处理请求、发送失败、重试和过期状态都可检查。

通知不会替你授权、回答、登录或解除安全限制。需要授权的工作仍暂停；只有独立且已获授权的工作可以继续。
它不能自动接入 ChatGPT，也不会读取会话文件、安装后台服务、配置邮箱或转发云端桌面通知到你的电脑。

## 无账号、无邮件的体验

如果 GitHub Releases 中已有 agent-action-notifier.pyz，可直接下载；否则从源码构建：

```sh
git clone https://github.com/Afloat16/agent-action-notifier.git
cd agent-action-notifier
python scripts/build_zipapp.py
python dist/agent-action-notifier.pyz demo
```

演示只使用临时数据库和终端输出，不连接邮箱、不弹真实桌面通知、不操作智能体。
演示依次显示：等待人工参与，同时有独立工作 → 重复事件去重 → 请求解除，但是否恢复仍未知 → 工作流明确上报完成。

在虚拟环境中运行 python -m pip install . 可安装 aan 命令。
pip 可能下载构建依赖；直接构建 .pyz 只需 Python 标准库。本项目未宣称发布到 PyPI。

## 最小可用流程

生产事件的进程和发送进程必须使用同一个数据库路径：

```sh
aan --db ./private-state.db emit --queue-only --file examples/input-required.json
aan --db ./private-state.db worker
aan --db ./private-state.db status
```

在另一个终端保持前台发送进程运行：

```sh
aan --db ./private-state.db worker --watch
```

它大约每秒检查一次待发送队列。队列在重启后保留，但电脑关机、休眠或进程停止时不能发送。
Hook 只快速入队，不等待邮件发送，更不会向上游返回允许执行的决定。

## 启用邮件和桌面渠道

默认只输出到终端。请在产生事件或运行 Hook 的进程中明确设置：

```sh
export AAN_CHANNELS=email,desktop
```

渠道随通知入队保存。只给发送进程设置渠道，不会把已入队的终端通知转换为邮件。
Windows PowerShell 使用 $env:AAN_CHANNELS = 'email,desktop' 设置环境变量。

邮件发送进程需要通过受保护环境或密钥管理器获取：

- AAN_SMTP_HOST、AAN_SMTP_PORT、AAN_SMTP_SECURITY（ssl 或 starttls）
- AAN_SMTP_FROM、AAN_SMTP_TO（各一个普通邮箱地址）
- AAN_SMTP_USERNAME、AAN_SMTP_PASSWORD（安全地提供，不放命令参数、事件或仓库）

参考 .env.example 和服务商官方配置。工具不会自动读取 .env。
只允许验证证书的 TLS，不会降级成明文。支持免认证的 TLS 中继，但用户名和密码必须同时省略。
创建应用密码、OAuth 权限或系统通知权限仍需你自己处理。

Linux 需要已有 notify-send 和桌面通知会话；macOS 使用 osascript，但权限或专注模式可能阻止显示。
Windows 使用系统已有的 PowerShell 5.1/.NET Windows Forms，在已登录的交互桌面请求通知区域气泡。
它不是 WinRT 原生 Toast；标题和正文按系统限制截短，临时图标保持十秒后清理。
专注模式、系统抑制或其他气泡可能使它不显示，缺少运行时或交互会话会明确报错。
桌面通知出现在发送进程所在电脑，不会从云服务器自动传到你的笔记本。

依赖通知前，先用无敏感内容的事件实际测试邮箱和桌面，检查垃圾邮件及 aan status。
SMTP 接受邮件和桌面命令成功，都不等于邮件已进收件箱、弹窗已显示或你已读到。

Windows 下载 zipapp 后，可在独立前台 PowerShell 中运行：

```powershell
$env:AAN_CHANNELS = 'email,desktop'
$env:AAN_DB = "$env:LOCALAPPDATA\AgentActionNotifier\state.db"
python .\agent-action-notifier.pyz worker --watch
```

产生事件的另一个终端也需继承相同数据库和渠道设置。邮件还需安全提供 SMTP 配置。
这不会安装服务或修改执行策略。

## 状态和接入边界

- human_input_required：等待人工输入，status 为 waiting 或 blocked
- independent_work=true：生产方明确知道独立且已授权的工作正在继续；任务状态仍为等待
- request_resolved：上游确认请求已处理、取消或被替代；不会发出授权，也不会自动恢复上游
- 所有请求解除后状态先为 unknown，直到上游明确报告下一状态
- 仍有待处理请求时，不能声称整个任务已 running 或 completed
- Stop 只代表一轮对话结束，不代表用户任务完成
- 非终态报告超过五分钟标为 stale，不能据此宣称它还在运行或已经失败
- 通知带报告时间；排队超过五分钟才发送时，额外注明它是延迟通知，需要重新检查当前状态

已提供 Codex PermissionRequest/Stop 和 Claude PermissionRequest/AskUserQuestion/Stop 的命令 Hook 适配。
缺少稳定请求 ID 或解除事件的 Hook，只能启发式去重，不能完整证明待处理清单。
要做可靠的全生命周期接入，宿主应主动发送标准事件。
完整配置、Webhook 和官方 API 来源见 [接入文档](docs/integrations.md)。

## 隐私与可靠性

通知默认只有散列任务编号和通用状态。摘要必须用 --include-summary 或 AAN_INCLUDE_SUMMARY=1 明确启用；
启发式脱敏不能识别所有秘密，所以不要传入私密问题、命令、日志或密码。
可选操作链接必须是你明确允许域名上的普通 HTTPS 审阅页面，不得带查询参数、片段或凭据。

发送为至少一次：外部接受后、本地记录前崩溃，可能重复。稳定 Message-ID 不保证恰好一次。
可重试失败指数退避，最多八次；永久失败显示为 dead。
修复配置后用 aan retry-dead 重排仍有效的失败通知。已解除或替代的旧请求不会复活。
SQLite 本地文件不是加密存储，接收方、邮件服务商和系统通知历史都可能保留通知内容。

## 测试

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src scripts
python scripts/build_zipapp.py
python dist/agent-action-notifier.pyz demo
```

邮件及桌面测试使用模拟接口，Webhook 使用真实本机 HTTP；没有宣称已验证你的邮箱、电脑或实时智能体。
此工具由社区独立提供，不是 OpenAI 或 Anthropic 官方产品。
