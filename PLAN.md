# Chat ACITS — 项目说明 & 运维手册

> Session-wise Workspace、OpenClaw/Hermes 能力迁移和当前实现矩阵见
> [`SESSION_WORKSPACE_MIGRATION.md`](SESSION_WORKSPACE_MIGRATION.md)。下文部分早期架构描述仅用于历史参考。

ChatGPT 风格的内网聊天前端,接入团队三套本地 vLLM 模型。FastAPI + React,Docker Compose 部署。

## 访问

- 用户端:**http://10.10.132.126:5173**(同子网任意机器)
- 健康检查:`curl http://localhost:5173/api/health` → `{"status":"ok","title":"Chat ACITS"}`
- 注册账号:首页右上"注册" → 用户名 + 密码即可

## 架构

```
┌──────────────────────────────────────────────────────────────────┐
│ Browser (10.10.x.x)                                              │
└──────────────────────┬───────────────────────────────────────────┘
                       │ :5173 (HTTP)
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│ chat_acits_frontend  (nginx:1.27-alpine)                         │
│   /          → /usr/share/nginx/html  (打包好的 SPA)             │
│   /api/*     → http://backend:8010   (内部反向代理,SSE 不缓冲)  │
└──────────────────────┬───────────────────────────────────────────┘
                       │ Docker 内部网络 chat_net
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│ chat_acits_backend   (python:3.12-slim + uvicorn)                │
│   FastAPI :8010                                                  │
│   ├─ /api/auth/*           注册 / 登录(JWT)                    │
│   ├─ /api/conversations/*  会话 CRUD                             │
│   ├─ /api/chat/completions SSE 流式聊天 (经 harness)             │
│   ├─ /api/chat/models      列出可用模型 (从 harness 拉取)        │
│   ├─ /api/chat/skills      列出可用技能                          │
│   └─ /api/models/config    自定义模型增删                        │
└─────────┬──────────────────────┬─────────────────────────────────┘
          │ httpx (SSE)          │ aiosqlite
          ▼                      ▼
┌──────────────────────────┐  ┌──────────────────────────────────┐
│ chat_acits_harness :8020 │  │ ./data/chat_ds.db (挂载卷)      │
│   ├─ /v1/models          │  │  users / conversations /         │
│   ├─ /v1/chat/completions│  │  messages / custom_models       │
│   └─ tools: web_search,  │  └──────────────────────────────────┘
│            web_extract    │
└─────────┬────────────────┘
          │ httpx SSE (OpenAI 兼容)
          ▼
┌──────────────────────────────┐
│ 三个 LLM 端点(内网)         │
│ 10.10.132.2   MiniMax-M2    │
│ 10.10.132.1   DeepSeek-V4   │
│ 10.10.132.125 qwen3_6(多模)│
└──────────────────────────────┘
```

## 技术栈

**前端**(`frontend/`)
- Vite 8 + React 19 + Tailwind v4 + react-router 7
- react-markdown + remark-gfm + rehype-highlight(代码高亮)
- react-icons(Feather 图标集)

**后端**(`backend/`)
- FastAPI 0.115 + uvicorn (standard)
- SQLAlchemy 2 async + aiosqlite + lightweight `ALTER TABLE` 迁移
- passlib(bcrypt 4.0.1)+ python-jose(JWT)
- httpx 0.28(调 harness 流式)

**Harness 中转**(`harness/`)
- FastAPI 0.115 + uvicorn (standard)
- httpx 0.28(调 vLLM 流式)
- ddgs(DuckDuckGo)+ trafilatura(正文抽取)

## 文件结构

```
chat_ds/
├── docker-compose.yml          编排三服务 + 卷 + 网络
├── .env                        SECRET_KEY(不进 git)
├── .env.example                .env 模板
├── PLAN.md                     本文档
├── tasks.md                    最初的项目需求
├── data/
│   └── chat_ds.db              SQLite,挂载到 backend:/app/data/
├── harness/                    模型路由 + 工具中转服务
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                 FastAPI: /v1/models + /v1/chat/completions (SSE)
│   ├── config.py               Provider 配置 (三个 vLLM 端点)
│   ├── agent.py                Agent 循环: 工具预处理 → LLM 流式
│   └── tools/
│       ├── __init__.py         注册 web_search + web_extract
│       ├── registry.py         工具注册中心
│       ├── web_search.py       DDG 搜索 (日期前缀 + region=wt-wt)
│       └── web_extract.py      trafilatura 正文抽取
├── backend/
│   ├── Dockerfile              python:3.12-slim + TUNA 镜像
│   ├── .dockerignore
│   ├── requirements.txt
│   ├── main.py                 FastAPI 入口
│   ├── config.py               pydantic-settings 配置
│   ├── database.py             async engine + 迁移
│   ├── models.py               SQLAlchemy 表
│   ├── schemas.py              Pydantic IO
│   ├── auth.py                 JWT + bcrypt
│   ├── skills.py               技能注册表 + async generator dispatcher
│   └── routers/
│       ├── auth_router.py      /api/auth/{register,login}
│       ├── conv_router.py      /api/conversations/{,id,id/messages}
│       ├── chat_router.py      /api/chat/{completions,models,skills}
│       └── model_router.py     /api/models/config/...
└── frontend/
    ├── Dockerfile              node:20-alpine build → nginx:1.27-alpine serve
    ├── nginx.conf              SPA fallback + /api 反代 + SSE 不缓冲
    ├── package.json
    ├── vite.config.js
    ├── index.html
    └── src/
        ├── main.jsx
        ├── App.jsx             路由
        ├── index.css           全局样式 + 字体栈 + 滚动条 + .hljs 透明化
        ├── api.js              fetch 封装 + 流式 SSE 解析
        ├── pages/
        │   ├── Login.jsx
        │   ├── Register.jsx
        │   └── Chat.jsx        外层布局
        └── components/
            ├── Sidebar.jsx     会话列表 + 用户菜单
            ├── ChatArea.jsx    聊天主区(欢迎页 + 输入框)
            ├── MessageBubble.jsx  消息气泡 + 思考块 + 技能链 + 代码块 + lightbox
            └── Settings.jsx    自定义模型管理弹窗
```

## 数据库 schema

```sql
users              (id, username, email, hashed_password, avatar_url, created_at, updated_at)
conversations     (id, user_id→users, title, model_id, created_at, updated_at)
messages          (id, conversation_id→conv, role, content, reasoning, skill_chain,
                   image_urls, model_id, created_at)
custom_model_configs (id, user_id→users, model_id, model_name, provider, base_url,
                   api_key, is_multimodal, extra_headers, created_at)
```

`reasoning` 和 `skill_chain` 是 lightweight ALTER 加上去的两列(见 `database.py` 的 `_LIGHTWEIGHT_MIGRATIONS`)。SQLite 文件 ~3MB。

## 内置模型

| 内部 id | 显示名 | 端点 | API model | 上下文 | 输出上限 |
|---|---|---|---|---|---|
| `AgentModel` (沿用旧 id) | MiniMax-M2 | 10.10.132.2:1025 | `AgentModel` | 196k | 48k |
| `deepseek_v4_flash` | DeepSeek-V4-Flash | 10.10.132.1:1025 | `AgentModel` | 1M | 256k |
| `qwen3_6` | Qwen3-6 (多模态) | 10.10.132.125:1025 | `qwen3_6` | 262k | 64k |

定义在 `backend/routers/chat_router.py` 的 `BUILTIN` dict + `harness/config.py` 的 `PROVIDERS` dict。模型列表从 harness `/v1/models` 拉取，backend `/api/chat/models` 合并自定义模型返回前端。

`api_model` 字段区分"前端选择的 id"和"发给 LLM 的 model 字段名"——两个 AgentModel 端点在自己服务里都叫 `AgentModel`。

## Harness 中转

三个 LLM 端点统一由 `harness` 服务管理。backend 不再直调 vLLM，所有 chat 请求经 harness 中转：

```
backend :8010 → harness :8020 → vLLM endpoints
```

Harness 暴露 OpenAI 兼容 API：
- `GET /v1/models` — 返回三模型列表
- `POST /v1/chat/completions` — SSE 流式 (delta / reasoning / tool_progress)

Tool registry 已注册 `web_search` + `web_extract`，当前以 "先执行工具→增强消息→再调 LLM" 模式工作。skills.py 仍负责 web_search/research 的预处理上下文拼接。

用户还可以在前端"设置"里加任意 OpenAI / Anthropic 兼容的自定义模型(走 `custom_model_configs` 表)。

## 技能(Skills)

| id | 名称 | 行为 |
|---|---|---|
| `general` | 普通对话 | 直接发用户消息给模型,无任何前处理 |
| `web_search` | 联网搜索 | DDG 搜 5 条 → 拼成上下文 + system note 要求引用 [n] |
| `research` | 深度研究 | qwen3_6 拆 3 个子查询 → 并行搜索 → trafilatura 抓正文 → 综合成 dossier,要求模型按 Background / Key findings / Open questions / Sources 结构作答 |

`skills.py` 的 `run_skill_stream` 是 async generator,边干活边 yield 进度事件(`{type: 'progress', msg: ...}`),chat_router 转成 `skill_delta` SSE 推给前端;最后 yield 一次 `{type: 'result', result: SkillResult}`。

添加新技能:`SKILLS` 列表追加一项,`run_skill_stream` 加一个 `if skill_id == 'xxx'` 分支。前端无需改动(自动从 `/api/chat/skills` 拉到)。

## 部署(Docker Compose)

**首次部署**:

```bash
cd /nfs/yangbb/codes/chat_ds

# 1) 准备 SECRET_KEY
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
# 把输出粘到 .env 的 SECRET_KEY=

# 2) (可选)迁移现有数据
mkdir -p data
cp backend/chat_ds.db data/chat_ds.db   # 跳过则容器建新 DB

# 3) 停 dev 进程(如果还在跑)
pkill -f 'uvicorn main:app' ; pkill -f 'vite'

# 4) 构建 + 启动
sudo docker compose up -d --build

# 5) 验证
curl http://localhost:5173/api/health
```

`restart: unless-stopped` → **机器重启后两个容器自动拉起,不用手动干预**。

## 运维 playbook

### 看状态 / 看日志
```bash
sudo docker compose ps                    # 谁活着
sudo docker compose logs -f backend       # 跟后端日志(harness 调用、SSE 流)
sudo docker compose logs -f harness       # 跟 harness 日志(LLM 调用、工具执行)
sudo docker compose logs -f frontend      # 跟 nginx 访问日志
sudo docker compose logs --tail=200       # 最近 200 行

### 改代码后重新部署
```bash
# 改 harness
sudo docker compose build harness && sudo docker compose up -d harness

# 改后端
sudo docker compose build backend && sudo docker compose up -d backend

# 改前端
sudo docker compose build frontend && sudo docker compose up -d frontend

# 改 docker-compose.yml 或环境变量
sudo docker compose up -d --force-recreate
```
首次后由于 layer cache,重 build 通常 < 30 秒。

### 进容器排查
```bash
sudo docker exec -it chat_acits_backend bash
sudo docker exec -it chat_acits_frontend sh    # alpine 用 sh
# 例:在 backend 里直接打 SQLite
sudo docker exec -it chat_acits_backend python3 -c "
import asyncio
from sqlalchemy import select
from database import async_session
from models import User
async def m():
  async with async_session() as s:
    for u in (await s.execute(select(User))).scalars().all():
      print(u.username, u.id)
asyncio.run(m())
"
```

### 备份 / 恢复 DB
```bash
# 备份
cp data/chat_ds.db data/chat_ds.$(date +%F).bak

# 恢复(先停服务再换文件)
sudo docker compose stop backend
cp data/chat_ds.YYYY-MM-DD.bak data/chat_ds.db
sudo docker compose start backend
```

### 停 / 重启 / 重建
```bash
sudo docker compose restart backend       # 软重启
sudo docker compose down                  # 全停(卷不删,DB 还在)
sudo docker compose up -d                 # 重新拉起
sudo docker compose down -v               # !!! 含 -v 会删命名卷;咱们用 bind mount,DB 不会丢,但别养成习惯
```

### 模型端点失联怎么办
- 浏览器看到 `⚠️ 无法连接到 Harness 服务` → harness 容器未启动或崩溃
- harneess → vLLM 连接失败会从 agent.py 透出错误信息到 SSE
- 检查 vLLM 服务器:`curl http://10.10.132.x:1025/v1/models`
- 三个端点定义在 `harness/config.py`,如果 IP/端口变了改这里再 `docker compose up -d --build harness`
- 也可以直接 `sudo docker compose logs harness` 看 agent 日志定位具体错误

### sudo 免输密码(可选,长期方便)
```bash
# 把 yangbb 加入 docker group 后,新开一个 shell 就不用 sudo 了:
newgrp docker
# 或者直接 logout/login。已经在 docker group 里,只是当前 shell 没继承。
```

## 关键设计决定 / 容易踩的坑

1. **三个内网模型都是 reasoning 模型** ——所有内部短任务(标题生成、子查询规划)必须传 `"chat_template_kwargs": {"enable_thinking": False}` + 流式;否则 token 全用在思考上,`content` 为空。只有 qwen3_6 真正认这个 kwarg,所以内部辅助调用都走 qwen3_6。主聊天保留思考(用户看到 "思考中 → 思考完成" 折叠块)。
2. **`min-h-0` 必须** —— Flex column 里 `flex-1 overflow-y-auto` 子项默认 `min-height: auto`,内容撑高会让整页滚动而非内部滚动,从而把侧栏顶上去。`Chat.jsx` 根容器加了 `overflow-hidden`,所有 flex 子项加了 `min-h-0`。
3. **CodeBlock 视觉一致** —— `<.hljs>` 默认 `#0d1117` 跟外壳 `slate-950` 不同色,看起来有内框。`index.css` 里 `.hljs { background: transparent !important; padding: 0 !important; }` 拍平。内联 `<code>` 不要任何 pill,只 `font-mono text-rose-600`。
4. **SSE 反代** —— nginx 默认会 buffer SSE,前端就看不到流式效果。`nginx.conf` 的 `/api/` 块加了 `proxy_buffering off; chunked_transfer_encoding off; proxy_read_timeout 1800s;`。
5. **背景任务保存** —— 流式取消(用户刷新)不能丢 assistant 消息。`chat_router._spawn_persist` 用 `asyncio.create_task` + 独立 db session,**生还于请求取消**。同时只在 `full_content / full_reasoning / skill_chain` 至少有一非空时才存,避免脏空消息。
6. **passlib + bcrypt 版本钉死** —— `bcrypt==4.0.1`,4.1+ 跟 passlib 1.7.4 兼容警告很吵。
7. **Docker 构建必须走国内镜像** —— Dockerfile 里 TUNA(apt + pip)+ npmmirror(npm)。改回官方源会卡死在 `npm ci` ECONNRESET。
8. **登录态闭包陷阱** —— `ChatArea.send()` 里捕获 `activeConv` 的闭包,如果不用本地 `convAnnounced` flag 屏蔽,每个 SSE chunk 都会触发一次 `onConvCreated`,导致 sidebar 抖动 + 浏览器连接槽位耗尽。

## 修过的代表性 bug(简表,详细参考 git 历史)

| 症状 | 根因 | 修复 |
|---|---|---|
| 前端白屏 | DeepSeek 生成的 ChatArea 里有 `useStateHardware` `msg嗜血` 等幻觉 token | 整体重写 ChatArea + MessageBubble |
| 流式输出看不到 | 后端只 forward `delta.content`,reasoning 模型先吐 reasoning | 同时 forward `reasoning_delta`,前端渲染"思考"块 |
| 流到一半刷新,回答没了 | 保存逻辑在 generate() 主路径里,取消时丢 | 改 `asyncio.create_task` 背景保存 + 独立 session |
| 新会话不自动命名 | qwen3_6 偷懒把第一句原话当标题 | 同时喂 user+assistant 给标题模型,prompt 强制总结、不准复述 |
| 代码块灰背景里包白条 | inline `<code>` 用 `bg-indigo-50`(近白色) | inline code 完全去背景,只 `text-rose-600 font-mono` |
| 翻消息时侧栏被挤没 | flex column 缺 `min-h-0`,内容撑破整页滚动 | 给 flex 链路全部加 `min-h-0` + 根容器 `overflow-hidden` |
| 后端 ConnectError 时前端永远空气泡 | 异常被吞,空消息还入库 | try/except 捕获后通过 SSE delta 显示错误,空内容不入库 |

## 已知待优化

- 多轮多模态历史里图片上下文丢失(`_chat_stream` 重建 history 只读 `m.content`,不读 `m.image_urls`)
- 图片以 base64 data URL 入库,大图会撑 DB,可换对象存储
- 没有用户级"设置"页(主题、字号、默认模型),目前只有"自定义模型"一个 tab
- 没接 HTTPS / 域名;LAN 内 HTTP 直连
- 还没加 `.gitignore`,`.env` 有泄漏 SECRET_KEY 的风险
- Settings 弹窗里没法编辑已有自定义模型,只能删+重建

## 演进时间线(浓缩版)

| 阶段 | 关键事件 |
|---|---|
| 初始 | DeepSeek 生成骨架,前端跑不起来,代码满身非英文幻觉 token |
| 第一轮修复 | 阻塞 bug 全清(语法错、版本号、import),前端跑通 |
| 功能补齐 | Markdown 渲染、Skills 框架、Web Search、Deep Research、自定义模型 UI |
| 视觉迭代 | 暗 → 浅 → 中文化 → Chat ACITS 品牌 → 代码块细节 → 浅色精细化 |
| 健壮性 | 背景保存、SSE 错误透出、各种 flex 布局陷阱 |
| Level 3 部署 | Docker Compose 化、nginx 反代、SQLite 卷挂载、auto-restart |
