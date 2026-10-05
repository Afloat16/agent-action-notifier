# 邮件优先的人工交互

[English](email-first.md) · [README](../README.zh-CN.md) · [安全说明](../SECURITY.md)

把所需步骤直接写进邮件。普通问题可通过邮件回复；必须在网站操作时，提供官方登录或操作链接。
不是所有交互都要返回 GPT。平台要求的确认表单或人工接管仍须在原指定界面完成。

这是 **0.2.0 的通知和回复输入层**，不是邮件审批智能体：不轮询收件箱、不自动接入 GPT、
不代为登录，也不恢复上游任务。使用回复前，另行授权的宿主必须验证所有者的真实意图，并遵守原确认策略。

## 1. 先审阅事件并本地体验

按 README 安装 aan；也可将命令中的 aan 替换为 python /absolute/path/agent-action-notifier.pyz。

```sh
aan --db ./email-first-demo.db --channels stdout --include-action emit --file examples/email-action.json
aan --db ./email-first-demo.db status
aan --db ./email-first-demo.db replies list
```

这里只使用终端和本地数据库，不发送邮件、不需要凭据，也不运行桌面命令。
示例询问不敏感的 PDF/DOCX 格式偏好。操作正文会保守脱敏后保存；--include-action 不会把它显示在终端或桌面通知中。
实际发送前，请自行审阅事件内容。

## 2. 明确配置并发送邮件

实际部署使用另一个数据库，生产方和发送进程共享同一路径。先自行配置邮件服务和回复邮箱。
以下地址和设置均为占位示例；工具不会自动读取 .env.example。

```sh
export AAN_DB=/absolute/private/path/state.db
export AAN_CHANNELS=email
export AAN_INCLUDE_ACTION=1
export AAN_SMTP_HOST=smtp.example.com
export AAN_SMTP_PORT=465
export AAN_SMTP_SECURITY=ssl
export AAN_SMTP_FROM=agent-notices@example.com
export AAN_SMTP_TO=owner@example.com
export AAN_SMTP_REPLY_TO=agent-replies@example.com
# 用受保护环境或密钥管理器安全提供 SMTP 用户名和密码。
aan worker --watch
```

在继承相同生产方设置和数据库路径的另一个终端中：

```sh
aan emit --queue-only --file examples/email-action.json
aan status
```

全局选项放在子命令前。--include-action 可替代 AAN_INCLUDE_ACTION=1；--channels email 可替代 AAN_CHANNELS=email。
渠道和操作正文启用状态随通知入队保存；只给发送进程配置，无法给已入队的普通通知补上操作正文或邮件渠道。
新运行使用新 ID，不能重复使用已经解除的请求 ID。发送进程必须在前台持续运行。

FROM、TO、REPLY_TO 各接受一个普通 ASCII 邮箱地址。包含 reply_prompt 的操作邮件要求单独配置
AAN_SMTP_REPLY_TO，不会自动借用 FROM 或 TO。请自行配置该回复邮箱；工具不读取它。
STARTTLS 使用 starttls 和服务商指定端口，不支持明文降级。账号凭据及持续访问权限仍需你自行处理。

若需要回复的邮件以 email_reply_configuration_required 变成 dead，配置有效回复邮箱后，
用 aan retry-dead 重排仍有效的通知。缺少必需回复地址时，不会尝试 SMTP 发送。

依赖此流程前，请先在真实收件箱检查无敏感内容的通知，并查看 aan status。
smtp_accepted 只代表服务器接受邮件。

## 3. 提供完整步骤和官方链接

版本 1 标准事件新增可选 action，只能用于 human_input_required：

- kind：login、question、review、external_action 或 host_confirmation
- steps：1–12 条非空字符串，每条最多 500 个字符
- reply_prompt：可选的普通回答提示，最多 400 个字符
- completion_hint：可选的完成核验说明，最多 400 个字符；不会改变任务状态
- 整个事件仍不得超过 16 KiB；不认识的字段会被拒绝

--include-action 仅在邮件中发送经过审阅的结构化操作正文，默认关闭，与 --include-summary 独立。
不要把链接写在操作正文中，保守脱敏会移除它。把经审阅的官方普通页面放在顶层 action_url，
并明确允许它的 HTTPS 精确域名。不能包含用户信息、查询参数、片段、非标准端口、魔法登录链接、
Bearer 令牌或带凭据的路径。子域名需分别允许；URL 校验不能证明任意路径都安全。

login 类型必须带 action_url。仅当工作流确实需要登录 GitHub 时，审阅并将以下示例保存为 login-action.json：

```json
{
  "version": 1,
  "event_id": "github-example:login-needed",
  "task_id": "github-example",
  "type": "human_input_required",
  "request_id": "github-login",
  "status": "waiting",
  "action_url": "https://github.com/login",
  "action": {
    "kind": "login",
    "steps": [
      "打开本邮件中的 GitHub 官方登录页面。",
      "直接在 GitHub 上登录你自己的账号。",
      "在该浏览器中完成所需的通行密钥或多因素验证。",
      "不要通过邮件发送密码、通行密钥、恢复代码或安全验证码。"
    ],
    "completion_hint": "已授权宿主核验实际登录状态后，才能继续。"
  }
}
```

```sh
aan --channels email --include-action --action-host github.com emit --queue-only --file login-action.json
```

[GitHub 登录页](https://github.com/login)是普通官方页面，不是带令牌的登录链接。
示例不会配置 GitHub 或验证会话。任务专用的官方操作地址必须向服务方核实，不能猜测；其余步骤写在邮件中。
host_confirmation 可说明必须在哪个界面完成确认，并在有安全官方页面时附上该链接，不能用邮件回答替代强制确认。

## 4. 回复并导入真实邮件

包含 reply_prompt 的通知邮件带有 24 位十六进制请求引用、当前修订号，以及以 AAN-REPLY 开头的标记。
对这封通知使用“回复”，把它提供的整行标记复制到回复正文第一行，再在下面写普通回答。
以下仅示意；占位内容必须替换为通知给出的准确整行：

```text
AAN-REPLY <24-hex-request-reference> <current-revision>
PDF
```

不要转发通知、附加文件、粘贴秘密，或修改引用的旧通知作为回答。
将实际收到的回复导出/下载为保留原始邮件头和纯文本正文的 message.eml，然后明确导入：

```sh
aan --db /absolute/private/path/state.db replies ingest --file message.eml
aan --db /absolute/private/path/state.db replies list
aan --db /absolute/private/path/state.db status
```

导入要求恰好一个 In-Reply-To，准确指向先前被 SMTP 接受、允许回复的通知；请求必须仍待处理且修订号为当前版本，
正文第一行标记也必须准确匹配（24 位小写十六进制字符和正整数修订号）。.eml 最多 65,536 字节；新回答不能为空，且最多 2,000 个字符。
Message-ID、In-Reply-To 和 From 各须恰好一个。支持纯文本，或最多两个直接部分且恰好一个 text/plain 的
multipart/alternative。附件、嵌套 MIME、只有 HTML、格式错误或有歧义的内容，以及识别为自动或转发的邮件会被拒绝。
检测不能发现所有伪造或自动邮件。引用的旧标记不能回答新版本请求。这些检查只关联输入，不能验证作者身份。

成功导入只保存保守脱敏后的“未经验证的待处理输入”，绝不解除请求、授权、执行或恢复任务。
导入结果包含 state: pending_unverified 和 can_authorize: false。列表最多显示 100 条最新待处理输入。
请求更新或解除后，其输入标为过时；replies list --include-obsolete 可检查它们。
没有 --verified 开关。可信宿主必须另行确认所有者针对该请求和修订号的真实意图，核验回答，
遵守取消和确认策略，然后通过标准事件上报实际状态变化。本项目不提供此宿主桥接。

## 邮件头不能等同于授权

From、请求标记、Message-ID 和 In-Reply-To 都可复制或伪造。DKIM/DMARC 和 Authentication-Results
属于邮件认证信号，不能证明所有者批准了某项操作。即使邮件带 SENT 标签且 From 匹配，也可能是 API 插入的邮箱数据，
这一点见 [Gmail 标签规则](https://developers.google.com/workspace/gmail/api/guides/labels#types_of_labels)。因此，只读取邮箱或检查标签不能证明所有者意图。
不能据此自动授权，也不能用调用方传入的验证标志代替认证。

2026-10-05 核查的官方来源：

- [RFC 5322 §3.6.4](https://www.rfc-editor.org/rfc/rfc5322.html#section-3.6.4)：邮件和回复关联，不验证所有者身份
- [RFC 3834 §5](https://www.rfc-editor.org/rfc/rfc3834.html#section-5)：自动回复信号
- [RFC 8601 §7](https://www.rfc-editor.org/rfc/rfc8601.html#section-7)：伪造或误导性的认证头
- [Gmail messages.insert](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/insert)：直接插入邮箱与发送邮件不同
- [Gmail 系统标签](https://developers.google.com/workspace/gmail/api/guides/labels#types_of_labels)：From 匹配用户的插入邮件也会带 SENT 标签

SMTP 和解析测试为合成/模拟测试，不能证明你的邮箱已收到、邮件客户端兼容、所有者身份真实，或智能体已端到端恢复。
