# ChatDS 当前会话交接（2026-07-21）

> 这是本项目当前唯一的权威续接入口。Codex、Claude Code 或人工维护者在新会话开始时应先完整阅读本文件，再查看 Git 状态和生产容器状态。旧的 `_SESSION_STATUS.md`、`_HARNESS_CHANGES.md`、`_REMOTE_OPS.md` 与 `harness/HARNESS_V23_CONTEXT_2026-07-16.md` 仅用于追溯历史，不能代表当前部署。

## 1. 六十秒恢复工作状态

1. 工作目录：`/nfs/yangbb/codes/chat_ds`。
2. 当前分支：`fix/generic-skill-harness-20260717`。
3. 当前通用 Harness 实现提交：`4dd69e0a4f8dac20b20e368eecf4c21c6c5f952b`，父提交为 `649181e33dc242a64bcfc77f5b4d6444e8c96730`。
4. 生产机 `10.10.130.178` 与用户访问地址 `172.30.100.145` 是同一台主机的两个地址；前端端口为 `5173`。
5. 搜索机是 `10.10.132.126`（另一个地址 `172.30.100.126`），SearXNG 对 Harness 提供 `http://10.10.132.126:8088`。
6. 下一项业务验收不是继续自动跑模型，而是等待用户手工新开 V2.3 Skill E2E；拿到新 session ID 后，检查 debug JSONL、AgentRun/tool events、workspace 产物并与 ground truth 做结构和证据密度对比。
7. 不要 `git add -A`，不要还原用户自己的两个 tracked deletion，不要把 runtime/session/reference 大目录提交进 Git。

## 2. 用户目标与不可违反的约束

- Harness 的目标是准确执行任何符合通用格式规范的 Skill；V2.3 只是高复杂度测试用例，不能写 GAL3、阿尔茨海默病、特定文件名或特定 worker 数量的 case-wise hack。
- Skill 是执行权威：入口说明、资源闭包、intent、worker DAG、依赖、工具能力、aggregation、artifact contract 与 post-merge checks 都应由 Skill 声明驱动。
- 复杂 Skill 应真正使用所声明的 multi-agent/workers，并以 runtime-owned capability boundary 限定子代理；不能仅靠主模型模拟多代理输出。
- 最终 Markdown 要接近 `skills_and_refs` 中的 ground truth：比较结构、覆盖、证据链、表格、附录、traceability 和可用性，不要求逐字节相同。
- 不得因某个 `complete`、`full report` 或中间 `.md` 文件存在就提前宣布完成；必须验证 Skill 声明的最终 cohort 和终止合同。
- Agentic 主模型尽量使用 GLM-5.2 `AgentModel`。Qwen3.5 仅作为辅助/多模态端点，不应因为简单请求误入长 Agent 循环。
- 用户要求 Git 只做本地 commit，不向远端 Git remote push。
- 用户已明确要求当前阶段不要自动跑 V2.3 的模型重型 E2E；由用户手工跑，Harness 侧做诊断与系统性修复。
- 不在 Markdown、Git、日志或命令输出中保存/打印密码、token、API key。

## 3. 测试资产与参考文件

`skills_and_refs/` 当前包含：

- `xClinicalTrial-Design-V2.3.zip`：主要 V2.3 Skill 测试包。
- `xClinicalTrial_Design_V2.3.html`：设计思路说明。
- `GAL3_AD_FULL_REPORT_v2.3_glm52.md`：GLM-5.2 ground truth。
- `GAL3_AD_FULL_REPORT_v2.2_deepseekv4pro.md`：旧版对照。
- `xClinicalTrial-Design-V2.2.zip`：旧版 Skill 包。

这些文件当前属于本地参考/测试资料，不能因为清理工作树而删除，也不要未经确认批量加入代码提交。

## 4. 本会话诊断过的关键 session

### `ecbc00c03a404e0a97ad892f0adf837a`

- 大量 `write_file` / `execute_code` 参数被 conversation compaction 替换成 `_chatds_argument_omitted`，随后又被模型当成真实内容重放。
- 还存在 malformed JSON 被误报为缺少字段、相对 workspace 文件操作进入临时 executor cwd、主循环没有真正执行 Skill DAG 等问题。
- 修复方向是通用的：不可复制的历史摘要、结构化 omission guard、parse error 优先、managed workspace、确定性 Skill 编译与能力边界。

### `a993d814d2bd41a2900b7d5f210c214b`

- 简单图片问题出现约 60 次模型调用，实际是 60 次 `finish_reason=length`，0 tool、0 child，并非 Harness 判断不出 chat/agent。
- 约 1 MB image data URL 被按普通文本估成约 251K tokens，输出预算被错误压到 512；旧 continuation 又未携带 partial assistant，导致模型不断从头 OCR。
- 已使用 multimodal-aware token estimator、有界 length continuation、partial assistant 保留与无新增检测修复。

### `a317a79ea6874b2a84e089f379fe6515`

- 流中第二批 corrupt tool-call fragments 在一次修复后仍失败，旧逻辑终止并显示不完整草稿。
- 当前实现加强 streamed tool-call accumulator，保留一次有界 non-stream repair；已经产生可见内容或工具副作用时不透明重放整轮，避免重复内容或重复执行。

### `25af419847c842869a036cddad1a2479`

- Verifier 把一个约 63,950 bytes 的旧 `complete.md` 与真正约 213,540 bytes 的 final 文件一起审计，因旧文件证据密度不足而误杀整个响应。
- 当前 terminal cohort 规则：存在 `final/最终/终稿/定稿/正式版/최종` 强终稿时，只审计强终稿 cohort；`complete/full/完成版` 是弱候选。若有多个强终稿则全部审计。

### `10e653f9a4bc42b7bf0b308ec523900c`

- 暴露了 standard-format Skill 的能力计划不能把一次 required receipt 误当成工具永久耗尽的问题。
- 当前 `required` 表示最低执行证明；Skill 选出的有限工具在收到 receipt 后仍可按工作流复用，correction 阶段只暴露尚未解决的能力。

### `0f49566048024ff78afee1c13163d115`

- 对应 run：`e76e19d6f36d4505a2ec3e389a0cbd19`。
- Debug：`/nfs/temp/chat_ds/40ca1118122540c3891b9d69c8684ccf/0f49566048024ff78afee1c13163d115/workspace/debug/agent_runs/e76e19d6f36d4505a2ec3e389a0cbd19.jsonl`。
- 第五次 AgentModel 请求输入约 51,924 tokens、最大输出 235,564；运行到旧的 1500 秒硬上限时已有约 116,099 个隐藏 reasoning 字符，但 0 visible output、0 tool fragment、0 dispatch。
- 因此根因不是前端主动中断，也不是错误选择 agent/chat，而是 Harness 的绝对流超时低于 GLM-5.2 的生产性长思考时间。
- 当前只在“本轮 0 visible、0 tool fragment、0 current-turn dispatch 且确有 hidden reasoning”时允许一次新的逻辑恢复；恢复关闭 thinking 并保留 caller/context-clamped 输出预算。相同异常第二次出现则终止，避免无限重放。

## 5. 当前已提交的通用实现

### 5.1 Skill 选择、解析和指令遵循

- 只有一个精确 Skill 时可确定性复用，避免不必要的重复选择。
- semantic selector 只有在恰好产生一个严格合法的 expected tool call 时才接受 `stop`。
- Standard Skill 的自由格式正文先完整披露，再通过 runtime-owned catalog 提交 capability plan；模型不能凭参数扩大权限。
- 资源、脚本、声明式命令、HTTPS prefix、artifact write set 分别建账；读取 Skill 不自动授权脚本执行。
- compiled workflow 的 intent、bootstrap、worker waves、ordered aggregation、artifact synthesis、mandatory merge 都由 Harness 的 phase state 推进。
- worker 只能读取精确 worker contract 与声明的 prerequisite result paths；同波 sibling 不互相泄漏输出。
- receipt 必须来自同一 tool call 的 successful completion，失败读取不能伪装成已满足。

### 5.2 大参数、上下文压缩与输出预算

- `write_file.content`、`patch_file` 大内容和 `execute_code.code` 压缩后不再保留可复制的原参数字段，而使用不可执行的 omission metadata。
- 所有写入/执行入口都会拒绝 dict、list 或 JSON string 形式的 compacted-history placeholder。
- malformed tool JSON 保留真实 parse error，不再被 schema 清洗误报成缺 `filepath`/`code`。
- 8192 tokens 只用于 mandatory frontier 中全部工具均为保守小参数工具的情况；`write_file`、`patch_file`、`execute_code` 和未知 MCP 保留 caller/context 允许的输出预算，只在必要阶段关闭 thinking。
- 图片 transport/base64 不再按普通正文 token 计数；真实 caption、metadata 和文本仍计入上下文。

### 5.3 流式恢复与超时

- `LLM_STREAM_TOTAL_TIMEOUT_SECONDS` 默认从 1500 提升为 2400 秒（40 分钟），生产当前有效值也是 2400。
- 这与 AgentModel 的约 303,872 token 最大上下文是两个独立概念：前者是时间，后者是容量。
- 当前还保留：首包/初始 lease 600 秒、material-progress grace 180 秒、单次读取停滞 120 秒、连接 30 秒。因此真正无进展的连接不会一律等待 40 分钟。
- 绝对上限可通过生产 `.env` 的 `LLM_STREAM_TOTAL_TIMEOUT_SECONDS` 调整，无需改代码；不要把它降回 1500。
- provider 已产生 visible/reasoning delta 后，普通网络错误不透明 replay 整轮；这防止已经向用户或工具端产生的内容重复。

### 5.4 最终产物验证

- 复杂产物 verifier 使用可替换、可多语言的结构/证据通道，不绑定 GAL3 标题。
- 保留 worker/script outputs、source evidence、trace-step mapping、appendices、tables 和结果块，不鼓励把所有证据压缩成几段 prose。
- strong-final/weak-final cohort 规则消除了旧 `complete.md` 对真正 final 的误杀，同时仍会审计所有同等级强终稿。

### 5.5 Web Search / SearXNG

- Harness search broker 已实现 bounded cache、singleflight、并发 semaphore、endpoint circuit breaker、总 deadline、来源/engine provenance 和结果质量判断。
- semaphore 排队超时不会错误打开 endpoint circuit。
- 混合中英文查询中，只有一个 engine 支持的 Latin-only 结果通常需要第二 engine 共识；显式 `site:` 查询例外。
- 默认只使用 SearXNG；DuckDuckGo 是部署显式开启的可选 fallback，不是默认依赖。
- 10.10.132.126 上使用固定版本的官方 SearXNG 与 Valkey，而不是将整个上游源码复制进应用：
  - SearXNG：`docker.io/searxng/searxng:2026.7.9-8456831a0@sha256:a7b2b16eb4d79c2f0f6cff84fab9c41137e4e6dd29a1f64e2d785d27acb5a2e0`
  - Valkey：`docker.io/valkey/valkey:9-alpine@sha256:c9b77919daeba2c02ad954d0c844cc4e7142069d177b89c5fd771f405daf9e02`
- 真实冒烟查询 `Galectin-3 阿尔茨海默病` 返回 Baidu 和 360 等相关结果；当时 Mojeek timeout、Yahoo protocol error 被作为诊断信息降级，不会让整个搜索失败。
- 免费通用引擎的 CAPTCHA、协议变化和临时 suspension 是动态上游条件。当前方案提供聚合、缓存、熔断和优雅降级，不声称绕过反爬或保证无限容量。

### 5.6 Browser Sidecar

- Chromium 独立 sidecar，非 root UID 65532、read-only、cap-drop、no-new-privileges，与业务 `chat_net` 分离。
- CDP 不开放 TCP 服务，通过权限为 0700 的 named-volume Unix socket 连接 Harness；Harness 再建立带随机 token 的精确 loopback WebSocket relay。
- 原始 `/json/version` 和未授权 WebSocket path 不在 Harness loopback 直接暴露。
- request、redirect、subresource、iframe、popup 均经过 URL/DNS/private-origin policy；WebSocket、Service Worker、download、QUIC、直接 WebRTC/WebTransport 路径受限。
- 每个 agent run 使用独立 browser context，正常完成、取消、非流和流式结束都会清理。
- 生产部署时发现真实 Chromium 对 `/json/version` 返回完整 `Content-Length` 后仍保持连接；旧 healthcheck 等 EOF 导致误报 unhealthy。现已按 declared body length 判断完整响应，并增加回归。

### 5.7 内部 API 与 MCP

- Harness 对 `/v1/chat/completions` 和所有 `/internal/*` 要求 `X-Internal-Token`；`/health`、`/v1/models` 保持只读免鉴权。
- Backend chat、scheduler、MCP、Skill auto-register/remove、session cleanup 都自动携带 token。
- 生产 `.env` 使用部署生成的高熵 token、权限 0600；值绝不能写入文档或 Git。
- stdio MCP 经 `python -I` 启动可信 launcher，清除 ambient token，drop groups/GID/UID 到 65534，设置 no-new-privs、dumpable 与 rlimits，再 exec 配置命令。
- MCP 配置拒绝路径穿越，文件采用 atomic 0600、目录 0700；raw spec 限制为 64 KiB，临时 HOME 在断开时清理。
- 重要剩余边界：stdio MCP 虽已降权和隔离密钥，但仍不是完整 mount/network namespace sandbox；只应注册可信 MCP 配置。

## 6. 模型和运行参数

- Primary/agentic endpoint：`http://10.10.132.2:1025/v1`。
- 模型 ID：`AgentModel`，后端权重为 GLM-5.2，服务报告最大上下文约 303,872。
- Auxiliary multimodal endpoint：`http://10.10.132.128:1025/v1`（Qwen3.5）。
- 生产 provider admission 默认最多 3 个并发请求、估算 token 总额 240,000；普通 admission wait 为 0，即等待本次 run/batch 自身取消。
- delegation batch timeout 默认 3600 秒；单个 provider productive stream 绝对上限默认 2400 秒。

## 7. 验证证据

最新代码内容完成了以下验证：

- Harness 完整回归：`Ran 1190 tests ... OK (skipped=5)`。用户最初要求重跑的 1088 项已被新增回归扩展到 1190 项。
- Backend 回归：`Ran 24 tests ... OK`。
- Browser Sidecar：新增真实 healthcheck regression 后 `Ran 6 tests ... OK`。
- MCP sandbox 生产容器内：5 项全部通过，包括真实 launcher 降权和 ambient secret 不可见。
- `python3 -m py_compile`、`git diff --check`、默认 Compose config 和 `local-search` profile config 均通过。
- 生产真实冒烟：
  - Frontend HTTP 200；
  - Harness `/health` HTTP 200；
  - 内部接口无 token 为 401、Backend 正确 token 为 200；
  - Browser 真实打开 `https://example.com/` 并得到页面 snapshot；
  - Harness 真实调用 10.10.132.126:8088 得到聚合结果。
- 遵照用户要求，本轮收尾没有自动执行 V2.3 模型 E2E。

## 8. 当前生产状态

生产主机：`10.10.130.178` / `172.30.100.145`。

| 服务 | 状态 | 当前镜像 ID |
|---|---|---|
| `chat_acits_browser` | running / healthy | `sha256:dfe9e49d893e2618c9fec58c095a8254d5e91da4f4f06ce032e0090fe56d6fca` |
| `chat_acits_harness` | running / healthy | `sha256:a8c9660a827dcdfa94ca50c5e274209ac77eddbf1a97d07163b065ec4905211e` |
| `chat_acits_backend` | running | `sha256:b64ca344d04751f17e4c2acc8a191d00bb99fc57e7e2665bc3e1df1b326bf421` |
| `chat_acits_executor` | running / healthy | `sha256:fd3f9225fbf5c14a999571babbf63a81beadc3a98cc22f3fc5f889d8467da089` |

- 旧 Harness/Backend 镜像分别保留本地 rollback tag：`rollback-20260721-pre-generic-skill`。
- 生产 `.env` 的 `SEARXNG_BASE_URL=http://10.10.132.126:8088`，`LLM_STREAM_TOTAL_TIMEOUT_SECONDS` 当前由 Compose 默认解析为 2400。
- 生产机原先独立运行、绑定 `0.0.0.0:8080` 的 `searxng:latest` 已确认最近一小时无流量且不被本项目引用；已执行 `docker stop searxng`，容器保留未删除，端口 8080 已关闭。其 restart policy 仍是 `unless-stopped`，显式 stop 后不会自动启动；如确需恢复可手工 `docker start searxng`。
- 搜索机 `/healthz` 已从生产 Harness 内验证为 `OK`。

## 9. Git 与工作树边界

- 通用实现本地 commit：`4dd69e0a feat: harden generic skill execution harness`。
- 该提交共 38 个文件，8342 insertions / 521 deletions；未向 remote push。
- 当前仓库没有配置 Git remote；除非用户后续明确改变要求，否则只做本地 commit。
- 生产镜像使用的源代码内容与该提交一致。
- 以下两个 tracked deletion 是用户已有工作树状态，没有进入 `4dd69e0a`，不要擅自 restore、stage 或提交：
  - `XGAL-101_Galectin-3_AD_Comprehensive_Development_Plan_v1.0_claudecode执行参考.md`
  - `xClinicalTrial-Design-V2.2.zip`
- `data/skills/**`、`data/workspace/**`、`data/runtime_envs/**`、`harness/data/memories/**`、`workspace/**`、`skills_and_refs/**`、`searxng-master/**` 等包含运行数据、测试资产或上游源码副本；不要批量 stage。
- 任何后续提交都应使用显式文件列表，并在 commit 前执行 `git diff --cached --name-status`、`git diff --cached --check` 和 secret scan。

## 10. 凭据与安全操作

- 不要把用户在聊天中给出的密码写回任何 Markdown。
- 生产 SSH 凭据仅从 `.local_secrets/remote_10.10.130.178.env` 读取。
- 搜索机 sudo/SSH 凭据仅从 `.local_secrets/remote_10.10.132.126.env` 读取。
- `.local_secrets` 目录应保持 0700，文件保持 0600；生产 `.env` 保持 0600。
- `frontend/SESSION_STATE.md` 的旧 Git 历史曾包含明文生产凭据；当前版本已经脱敏，但本轮没有做破坏性的 history rewrite。应把该凭据视为需要轮换；若用户批准清理历史，应单独备份并规划重写，不能在普通修复中擅自 force rewrite。
- 安全调用模板：

```bash
set -a
source .local_secrets/remote_10.10.130.178.env
set +a
export SSHPASS="$CHATDS_REMOTE_PASSWORD"
sshpass -e ssh -o StrictHostKeyChecking=no \
  "$CHATDS_REMOTE_USER@$CHATDS_REMOTE_HOST" 'docker compose ps'
unset SSHPASS
```

- 不使用 `ssh -tt`；不要让命令打印 `.env` 或 secret 文件内容。

## 11. 用户手工 V2.3 E2E 后的接手流程

1. 获取新 conversation/session ID 和大致开始时间。
2. 先读该 session 的 `workspace/debug/agent_runs/*.jsonl`，不要只根据前端错误文案猜测。
3. 对每个 LLM request 统计：provider/model、input/max-output tokens、reasoning/visible/tool fragments、finish reason、elapsed、retry/continuation reason。
4. 对工具统计：Skill selection、capability plan、delegate tasks、worker receipts、search/MCP、write/execute 错误、是否出现 omitted placeholder 或 malformed JSON。
5. 核对 workspace：模块文件集合、强终稿 cohort、总大小/行数、README/merge receipt、证据/source/trace/table/appendix 密度。
6. 与 `GAL3_AD_FULL_REPORT_v2.3_glm52.md` 做语义结构对比，并将差距归因到以下之一：
   - Skill 编译/资源闭包；
   - intent/route；
   - multi-agent 调度或 result fan-in；
   - 工具协议/长参数；
   - provider stream；
   - verifier；
   - 上游搜索/MCP 可用性；
   - 模型自身未遵循已正确提供的指令。
7. 只修复可泛化的根因，并增加跨领域/standard-format regression；不要为 V2.3 的具体标题、文件名、药物或疾病写特判。
8. 修复后先跑相关 targeted tests，再跑完整 Harness suite；是否再跑真实模型 E2E由用户决定。

## 12. 已知剩余风险

- stdio MCP 的 UID/secret 边界不是完整的 filesystem/network sandbox，仍需把配置视为可信代码。
- Browser DNS 校验属于应用层防护，DNS 检查与真正连接间理论上存在 TOCTOU；生产网络边界仍应作为第二层控制。
- 免费搜索引擎健康度会变化；单个 engine timeout/CAPTCHA 不代表 Harness 逻辑回归。
- 复杂报告是否“与 ground truth 一致”最终仍需要一次新的真实模型 E2E 证据；目前基础能力和回归已通过，但不能把单元测试等同于最终业务验收。

## 13. 相关文件入口

- 当前配置：`.env.example`、`docker-compose.yml`、`harness/config.py`。
- 核心 agent runtime：`harness/agent_loop.py`、`harness/skill_capability_plan.py`。
- Search：`harness/tools/web_search.py`、`searxng/settings.yml`、`searxng/limiter.toml`、`SEARXNG_WHITELIST.md`。
- Browser：`harness/tools/browser.py`、`browser_sidecar/server.py`。
- MCP/internal auth：`harness/tools/mcp_client.py`、`harness/runtime/mcp_stdio_sandbox.py`、`harness/main.py` 与 Backend routers/scheduler。
- 历史 V2.3 修复记录：`harness/HARNESS_V23_CONTEXT_2026-07-16.md`。
