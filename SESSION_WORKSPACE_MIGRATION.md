# Session-wise Workspace 能力迁移说明

本轮以当前 Web 会话为边界，分析了目录内 `openclaw/` 与
`hermes-agent/` 的可复用能力。迁移原则是：能力必须能在浏览器会话、
服务端 Harness、用户隔离工作区和现有数据库模型中成立。

## 已迁移能力

| 能力 | 来源思路 | 当前实现 |
|---|---|---|
| 会话独立工作区 | OpenClaw workspace、Hermes cwd/context files | 每个用户每个会话使用 `/nfs/temp/chat_ds/<user>/<session>/workspace` |
| 启动上下文文件 | `AGENTS.md`、`SOUL.md`、`USER.md`、`TOOLS.md`、`MEMORY.md` | 创建会话时自动初始化，Harness 每轮构造系统提示时加载 |
| 目录级渐进上下文 | OpenClaw/Hermes 嵌套 `AGENTS.md` / `CLAUDE.md` | 文件工具进入子目录后按需注入，不一次性占用上下文 |
| 上下文安全扫描 | Hermes skills/cron guard | 拦截指令覆盖、秘密读取、隐藏 Unicode 等高风险内容 |
| 工作区编辑 | Hermes file operations | 浏览器文件管理、`read_file`、`write_file`、`patch_file`、`search_files` |
| 原子写入与路径隔离 | Hermes atomic replace / traversal hardening | 原子替换、拒绝绝对路径、`..`、任意符号链接和跨会话路径 |
| 会话运行配置 | OpenClaw agent config | 每会话主模型、工具白名单、模型回退链和 Token 用量 |
| 自定义模型传输 | Hermes provider abstraction | OpenAI/Anthropic 兼容协议、额外请求头、流式事件标准化 |
| 模型自动回退 | OpenClaw/Hermes fallback chain | 按会话配置在认证、限流、超时和服务错误后切换模型 |
| 上下文压缩 | Hermes conversation compressor | 根据模型上下文用量压缩长会话工具轨迹 |
| MCP 会话隔离 | OpenClaw MCP、Hermes MCP runtime | 用户级配置叠加会话级配置，连接状态和工具目录按会话隔离 |
| 大工具目录渐进披露 | OpenClaw tool search | 大型 MCP catalog 自动折叠为 `tool_search/tool_describe/tool_call` |
| Skills 会话隔离 | OpenClaw/Hermes skills | 全局 skills 与会话 skills 分层，上传、扫描、查看和管理 |
| 子代理委派 | Hermes `delegate_task` | 新上下文子代理、共享当前工作区、并发和递归深度限制 |
| 跨会话工具 | OpenClaw session tools | 会话列表、历史、状态、发送上下文消息和 fork |
| 会话 Fork | OpenClaw sessions spawn/fork | 复制消息、工作区、会话配置和会话 skills，目标默认暂停 |
| 持久目标循环 | OpenClaw goal loop / Ralph loop | 目标状态、完成判定、自动继续、阻塞判定和独立 Token 预算 |
| 定时 Agent 任务 | OpenClaw/Hermes cron | 一次性、间隔、cron、时区、立即执行、暂停、历史和错误状态 |
| Cron 安全防护 | Hermes cron prompt scanner | 创建、更新、运行三层检查注入、秘密外传和破坏性命令 |
| 生命周期 Hooks | OpenClaw hooks | 签名 Webhook，支持 session/message/run/goal/cron 事件和启停 |
| 运行审计 | Hermes trajectory | AgentRun、CronRun、模型切换、工具进度、Token 用量和错误记录 |
| 轨迹导出 | Hermes batch trajectory | 导出会话、目标、消息、运行和定时任务，自动遮蔽常见秘密 |
| 隔离代码执行 | Hermes approval/execution safety | 独立无网络 executor，危险代码扫描、超时和大小限制 |
| 多模态路由 | Hermes image enrichment | 文本模型先由视觉模型描述图片，并将图片保存在会话工作区 |

## Web 控制面

聊天页右侧 `Session Workspace` 面板提供：

- 运行配置：主模型、回退模型、工具开关、Token 统计。
- 工作区：文件浏览、编辑、新建和删除。
- 目标：持久目标、状态、备注和预算。
- 自动化：定时任务创建、立即运行、暂停和删除。
- MCP：HTTP/SSE/stdio 会话级 Server 管理。
- Hooks：生命周期 Webhook 创建、暂停和删除。
- 轨迹：运行状态查看和脱敏 JSON 导出。

## 明确不迁移

以下能力依赖桌面、移动设备或外部消息渠道，不属于当前纯 Web
session workspace 的执行边界：

- Telegram、Discord、Slack、WhatsApp、Signal、邮件、短信等渠道适配器。
- 原生设备配对、摄像头、麦克风、屏幕控制、唤醒词和系统托盘。
- macOS/iOS/Android/Windows 桌面守护进程及本机通知。
- Home Assistant、电话网关和渠道身份路由。
- 面向单机 CLI 的 shell profile、终端皮肤、安装器和自动更新器。

这些功能不是缺失的工作区能力；若未来增加对应客户端或消息网关，
应作为独立接入层实现，不应进入当前会话 Harness 的信任边界。

## 验证

- Backend：工作区、调度、安全扫描、模型配置和数据库模型测试。
- Harness：会话隔离、MCP、工具上下文、原子补丁、工具检索和 Anthropic 转换测试。
- Frontend：ESLint 和 Vite production build。
