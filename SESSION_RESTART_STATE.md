# Session 重启恢复快照

保存时间：2026-07-03  
工作目录：`/nfs/yangbb/codes/chat_ds`  
当前目标：将本目录内 `openclaw/` 与 `hermes-agent/` 中适合当前 Web session-wise workspace 场景的能力迁移到本系统，并完成验证。

## 结论

本轮迁移已完成，尚未由本会话执行线上容器重启/部署。重启后优先阅读：

1. `SESSION_RESTART_STATE.md`：当前恢复入口，也就是本文。
2. `SESSION_WORKSPACE_MIGRATION.md`：OpenClaw/Hermes 能力迁移矩阵与排除边界。
3. `PLAN.md`：原系统架构与运维说明，顶部已链接迁移文档。

## 已完成的主要能力

- 会话级 workspace：每个用户/会话独立目录 `/nfs/temp/chat_ds/<user>/<session>/workspace`。
- 自动初始化上下文文件：`AGENTS.md`、`SOUL.md`、`USER.md`、`TOOLS.md`、`MEMORY.md`。
- workspace 上下文加载、安全扫描、嵌套 `AGENTS.md`/`CLAUDE.md` 渐进注入。
- 文件工具增强：`read_file`、`write_file`、`patch_file`、`search_files`，并加原子写入、路径穿越和符号链接防护。
- 会话运行配置：主模型、工具白名单、模型 fallback chain、Token 用量。
- 自定义模型：OpenAI/Anthropic 兼容协议，额外 headers 校验，流式事件标准化。
- 模型回退：认证、限流、超时、服务错误后按会话配置切换。
- MCP 会话隔离：用户级 + 会话级配置叠加，大工具目录自动折叠为 `tool_search/tool_describe/tool_call`。
- Skills 会话隔离：全局 skills 与 session skills 分层。
- 子代理委派：`delegate_task` 支持新上下文子代理，共享当前工作区，限制并发和递归。
- 跨会话工具：会话列表、历史、状态、发送上下文消息、fork。
- 会话 Fork：复制消息、工作区、会话配置和 session skills。
- 持久目标：目标状态、自动继续、完成/阻塞判定、独立 Token 预算基线。
- Cron 自动化：一次性、interval、cron、时区、手动触发、暂停、历史。
- Cron 安全扫描：创建、更新、运行时均拦截 prompt injection、秘密读取/外传、破坏性命令和隐藏 Unicode。
- 生命周期 Hooks：签名 Webhook，支持 session/message/run/goal/cron 事件，可暂停/启用。
- 运行审计与轨迹导出：AgentRun、ScheduledJobRun、模型切换、工具进度、Token、错误；导出时遮蔽常见 secret。
- 前端 `Session Workspace` 右侧面板：运行配置、工作区、目标、自动化、MCP、Hooks、轨迹。

明确不迁移内容：外部 IM 渠道、设备配对、桌面/移动原生能力、唤醒词、系统托盘、本机通知、Home Assistant/电话网关等。原因见 `SESSION_WORKSPACE_MIGRATION.md`。

## 关键新增/修改文件

### 根目录

- `.gitignore`：忽略 `.env`、数据库、构建产物、密钥类文件。
- `.env.example`：补充 `INTERNAL_API_TOKEN`。
- `docker-compose.yml`：统一 backend/harness 的内部 token、workspace 挂载、executor socket，并修正 Qwen 环境变量名。
- `SESSION_WORKSPACE_MIGRATION.md`：能力迁移矩阵。
- `SESSION_RESTART_STATE.md`：本恢复快照。

### Backend

- `backend/models.py`
  - 新增/扩展：`Conversation` 运行配置、目标和 Token 字段。
  - 新增：`AgentRun`、`ScheduledJob`、`ScheduledJobRun`、`EventHook`。
- `backend/database.py`
  - lightweight SQLite 迁移补齐新增列。
- `backend/schemas.py`
  - 新增会话设置、目标、workspace 文件、schedule、hook、custom model 校验 schema。
- `backend/workspace.py`
  - workspace 初始化、安全路径、上下文扫描、原子写入、fork、轨迹脱敏。
- `backend/scheduler.py`
  - session-scoped cron runner、模型解析、fallback、prompt 安全扫描。
- `backend/hooks.py`
  - 生命周期 Webhook 发送、HMAC 签名、URL 安全检查。
- `backend/routers/workspace_router.py`
  - workspace 文件、会话设置、目标、fork、runs、trajectory。
- `backend/routers/schedule_router.py`
  - 定时任务 CRUD、手动触发、历史、internal API。
- `backend/routers/hook_router.py`
  - Hooks CRUD、启停。
- `backend/routers/internal_session_router.py`
  - Harness 内部控制面：sessions、history、status、send、fork、goal。
- `backend/routers/chat_router.py`
  - 会话配置接入、fallback configs、usage/model-switch SSE、AgentRun、message events。
- `backend/routers/model_router.py`
  - 自定义模型保留字/协议校验。
- `backend/routers/conv_router.py`
  - workspace 初始化、上传路径安全、session delete 清理。
- `backend/tests/`
  - 新增 backend 层 session workspace / schedule / security / model validation 测试。

### Harness

- `harness/agent_loop.py`
  - 动态 provider/fallback chain。
  - OpenAI/Anthropic 请求转换和流式事件归一化。
  - workspace context、MCP 自动连接、tool search、目标自动续跑。
  - 按 provider 保留/剥离图片内容，兼容拒绝 `stream_options` 的 OpenAI-like 服务。
- `harness/workspace_context.py`
  - workspace context 加载、安全扫描、嵌套规则注入。
- `harness/tools/file_tools.py`
  - workspace 文件工具改为 `/workspace`，新增 `patch_file`。
- `harness/tools/path_security.py`
  - 组件级路径隔离，拒绝 symlink parent。
- `harness/tools/backend_control.py`
  - sessions、goals、cron 的内部控制面工具。
- `harness/tools/delegation.py`
  - 子代理委派。
- `harness/tools/tool_search.py`
  - 大型 MCP 工具目录渐进披露。
- `harness/tools/__init__.py`
  - 注册新增工具。
- `harness/tests/`
  - 新增 workspace context、patch、tool search、Anthropic conversion、multimodal sanitization 测试。

### Frontend

- `frontend/src/components/SessionWorkspace.jsx`
  - 新增右侧 Session Workspace 控制面。
- `frontend/src/api.js`
  - 新增 workspace、settings、goals、runs、trajectory、schedules、hooks、MCP API。
- `frontend/src/components/ChatArea.jsx`
  - 接入会话设置、模型选择和 workspace 面板。
- `frontend/src/pages/Chat.jsx`
  - 传递模型列表和 fork 回调。
- `frontend/src/components/Sidebar.jsx`
  - 会话 fork 操作。
- `frontend/src/components/Settings.jsx`
  - 自定义模型 extra headers。
- `frontend/src/index.css`
  - 通用 input/button 样式。

## 验证状态

最后一轮验证均通过：

```bash
python -m compileall -q backend harness executor

cd backend && pytest -q
# 5 passed

cd harness && pytest -q
# 11 passed

cd frontend && npm run lint && npm run build
# lint 通过，Vite build 通过

docker compose config --quiet
# 通过，无输出

cd backend && python -c 'import main; print("backend import ok")'
cd harness && python -c 'import main; print("harness import ok")'
```

前端 build 仍有 Vite chunk >500kB 的 warning，不影响当前功能。

## 重启/恢复步骤

### 1. 回到目录

```bash
cd /nfs/yangbb/codes/chat_ds
```

### 2. 确认 `.env`

确保 `.env` 至少有：

```bash
SECRET_KEY=<生产 JWT 随机密钥>
INTERNAL_API_TOKEN=<backend 与 harness 共享的独立随机 token>
```

生成方式示例：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

`INTERNAL_API_TOKEN` 必须 backend/harness 一致；不要提交 `.env`。

### 3. 构建并启动

```bash
docker compose up -d --build
```

### 4. 检查容器

```bash
docker compose ps
docker compose logs --tail=100 backend
docker compose logs --tail=100 harness
docker compose logs --tail=100 frontend
```

本次保存前执行 `docker compose ps --format json` 没有返回容器记录，因此需要重启后确认实际运行状态。

### 5. 健康检查

```bash
curl http://localhost:5173/api/health
```

预期包含：

```json
{"status":"ok"}
```

### 6. 功能抽查

建议依次抽查：

1. 新建会话，确认 workspace 自动生成 `AGENTS.md` 等文件。
2. 打开右侧 `Session Workspace`：
   - 保存模型/工具配置；
   - 编辑 workspace 文件；
   - 创建/清除 goal；
   - 创建 paused/resumed hook；
   - 创建并手动触发一个 `in 1m` 或 `30m` cron job。
3. 上传或配置 MCP server，确认会话内 `mcp_server_list` 返回正确 catalog。
4. 发送一条普通消息，确认 runs 里出现 AgentRun。
5. 导出 trajectory，确认常见 secret 被遮蔽。

## 如果重启后让 Codex 继续

建议直接对 Codex 说：

> 请读取 `/nfs/yangbb/codes/chat_ds/SESSION_RESTART_STATE.md` 和 `SESSION_WORKSPACE_MIGRATION.md`，继续从这里恢复当前 chat_ds 的 session-wise workspace 迁移状态。先运行验证命令，再根据结果决定是否部署或修复。

## 注意事项

- 旧文件 `frontend/SESSION_STATE.md` 是之前的 Claude Code 快照，包含远程服务器密码等敏感信息；本快照未复制那些 secret。后续建议清理或移出仓库。
- 根目录不是标准 Git 工作树；如果要做版本留存，建议先初始化 Git 或复制整个目录备份。
- 数据库文件和 `.env` 已加入 `.gitignore`，但现有文件仍在磁盘上，不代表已从任何外部历史中清除。
- 当前变更未自动推送，也未自动创建 PR。
