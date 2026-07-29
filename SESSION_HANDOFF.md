# ChatDS 当前会话交接（2026-07-29）

> 本文件是本仓库唯一的权威续接入口。新 Codex/Claude Code 会话必须先完整阅读本文件，再查看 Git、测试和生产状态。旧 `_SESSION_*.md`、`_HARNESS_*.md`、`_REMOTE_OPS.md` 只用于历史追溯。

## 1. 当前结论

- 工作目录：`/nfs/yangbb/codes/chat_ds`。
- 分支：`fix/generic-skill-harness-20260717`。
- 2026-07-29 最新功能提交：
  `7bbc0809 fix: harden generic skill workflow execution`。
- `7bbc0809` 已修复 `0147f...` 暴露的通用 Skill 执行问题：run-scoped 冻结包快照、
  optional Knowledge Gate 最小权限、receipt 驱动的完成质量、typed gap ledger、
  delegate 可观测性，以及异常/正常 EOF 缺终态的安全闭合。未加入疾病、文件名、
  session ID 或 V2.3 特判。
- 2026-07-29 已把完整生产从 `10.10.130.178` / `172.30.100.145` 切换到本机
  `10.10.132.126` / `172.30.100.126`；新入口为
  `http://10.10.132.126:5173` 和 `http://172.30.100.126:5173`。旧主机项目容器与
  5173 监听均为 0，旧数据库卷保留为回滚点。
- 上一轮 Knowledge Gate 功能提交：
  `6785e443 feat: compile exact conditional skill knowledge gates`。
- `6785e443` 已把 `knowledge_gate.checks[].tools` 通用编译为签名的条件候选组，
  补齐 plan/digest、两阶段最小权限、TOCTOU、精确 receipt 和 gap ledger；全量回归
  通过并已部署生产。
- 上一版已部署功能提交：
  `da70dc51 feat: make skill runs durable and transactional`；该版补齐断线后后台续跑、
  权威终态/刷新投影、语义化子 Agent、委派硬期限与撤权 fence，以及 Skill
  安装/管理事务。
- 前三轮关键提交：`5a7f21d9 feat: enforce generic skill execution contracts`、`e90415a0 feat: close generic skill workflow recovery gaps`、`b0744a33 feat: add generic profile-aware skill sandboxes`。
- 本轮在既有内容寻址 Workflow IR、exact capability binding、运行生命周期/receipt
  ledger、MCP frozen catalog 和委派 TOCTOU 防护基础上，补齐了 Knowledge Gate 的
  compile → bind → decide → activate → dispatch → audit 闭环。
- `6785e443` 已从 clean Git archive 构建并按 Harness → Backend 的兼容顺序部署；
  Frontend 无代码变化，保持 `da70dc51`。
- 不自动执行模型重型 V2.3 E2E。V2.3 是用户手工业务验收用例，不是 Harness 特判目标。
- Git 只做本地 commit，不向 remote push。

## 2. 用户目标与不可违反的约束

1. Harness 应准确执行任何符合通用格式规范的 Skill。不得加入 GAL3、疾病、文件名、session ID、固定 worker 数量或 V2.3 route 特判。
2. Skill 原文和其内容寻址资源闭包是执行权威。标准 Skill、结构化 workflow、子 Agent 都必须传播同一 package/script/declaring-document authority。
3. 每次 session 诊断必须同时交叉检查：
   - `workspace/debug/agent_runs/*.jsonl`、AgentRun 和 tool events；
   - 持久化对话上下文；
   - 当时实际安装、启用的 Skill 原文、引用资源和脚本。
4. 诊断必须区分 Harness、Skill、provider/model、网络/策略和上游站点问题；不能只根据前端错误文案猜测。
5. 复杂 Skill 应真正执行所声明的 worker DAG/multi-agent、fan-in、artifact contract 和 post-merge checks，不能由主模型假装多 Agent。
6. 不因中间 `complete.md`、单个成功 receipt 或部分文本提前完成；必须检查强终稿 cohort 和终止合同。
7. 不把密码、token、API key 写入 Markdown、Git、日志或命令输出。凭据只从权限受限的 `.local_secrets` 读取。
8. 保留 dirty worktree，不使用 `git add -A`，不 restore/stage 下列两个用户自有 tracked deletion：
   - `XGAL-101_Galectin-3_AD_Comprehensive_Development_Plan_v1.0_claudecode执行参考.md`
   - `xClinicalTrial-Design-V2.2.zip`

## 3. 本轮为何修改

用户要求核对并系统性修复下列 session：

- `5ae1d8a74870416bbdcfedbd18569dc4`：切换模型后没有根据 provider metadata 自适应 context/max output。
- `a78cf0756b254175b10b12a96791d62c`：长流在任务完成前中断。
- `8e48628d4feb43c6bd41dc1d650dfffe`：Skill 实际需要 Bash、Playwright、Selenium 和持久浏览器对象；旧 Harness 没有提供完整 session-wise 执行沙箱，而不只是“路由错了”。
- `dd3dc02c41f7485da229131a57478b37`：一个子 Agent 失败导致整棵任务异常退出，缺少 clean typed-result failure/recovery。
- `0e0feb5a6a6248629a666517644d64c8`、`81ef3f14dd614d409c21f87c08f2265c`：主要是网络白名单或上游可达性，应与 Harness 能力缺失区分。

此前已诊断的通用问题仍适用：

- `ecbc00c03a404e0a97ad892f0adf837a`：compaction placeholder 被当作真实大参数重放、malformed JSON 误分类。
- `a993d814d2bd41a2900b7d5f210c214b`：图片 data URL 被当正文估算，输出预算降至 512，导致 60 次 length continuation。
- `a317a79ea6874b2a84e089f379fe6515`：corrupt streamed tool-call batch 的 bounded repair/replan/synthesis。
- `25af419847c842869a036cddad1a2479`：旧 weak-final 文件误杀真正 strong-final。
- `0f49566048024ff78afee1c13163d115`：GLM-5.2 生产性长思考超过旧 1500 秒绝对流上限。

## 4. 已实现的通用闭环

### 4.1 Provider、上下文与长流

- provider `/v1/models` metadata 动态发现并缓存 context/max output；模型切换后重新按目标模型容量裁剪。
- `LLM_STREAM_TOTAL_TIMEOUT_SECONDS` 默认 2400 秒；Backend 到 Harness 的流 deadline 默认 3000 秒。
- 已产生 visible/reasoning/tool fragment 后不透明重放整轮，避免重复内容或副作用。
- corrupt tool stream 使用 bounded read-only salvage、exact-one replan、可信证据 synthesis 和确定性终止。
- delegated typed-result 污染/length failure 走 clean restart；子 Agent 失败不再无条件炸掉根任务。

### 4.2 Skill 解析、authority 与 profile preflight

- 对同一 immutable Skill snapshot 同时完成授权、依赖闭包、runtime profile 选择和发送；package/script/declaring-document digest 必须一致。
- 标准 Skill、结构化 workflow 和 delegation child 使用同一 authority，child 只能与 parent 权限求交。
- Python、Node、Shell 的静态依赖、PEP 508、常见 import/distribution alias、literal external commands 进入统一 preflight。
- Python `importlib`/`__import__`/`runpy`/subprocess、Node dynamic import/child process/eval/chdir、Shell source/eval/变量解释器/command substitution/动态 dispatch 均有界分析并 fail-closed。
- Bash heredoc、here-string、quoted/unquoted delimiter、`<<-`、多 heredoc、CRLF、续行、`|&`、FD redirection、stdin-code 与 stdin-data、变量解释器、xargs 和算术递归风险均有回归。
- 复杂、动态或超出 bounded grammar 的代码必须由 machine-readable exact marker/manifest 证明；不能猜测运行。
- bare relative helper 产生 `required_cwd` 并在 capability plan 与 `run_skill_process` 强制；`$SKILL_DIR`/`$CHATDS_SKILL_DIR` 锚定路径不依赖 cwd。
- direct local helper 必须有 suffix-compatible shebang；受支持脚本在 executor snapshot 中为只读可执行 `0550`，普通数据为 `0440`。

### 4.3 Process protocol v2 与 workspace 一致性

- 新增 authenticated persistent Bash/Python/Node process lease：stdin/stdout offset、EOF、signal、class/factory call、artifact sync、close、幂等重试和两阶段 CAS ACK。
- Harness 启动时同时 reap base/browser 遗留 lease。
- workspace write/patch/merge/resource copy/executor artifact apply 使用同一外部 private `flock`；写入原子化，多文件 apply 在 staging 后再次 CAS。
- 大报告通道上限支持大于 8 MiB 的单产物，并通过约 12.6 MiB PNG 真实验收。

### 4.4 Base 与 browser session-wise 沙箱

- 生产设计是在 Harness 同机部署两个独立 Docker executor service，不在搜索机部署：
  - `base-v1`：Bash/Python/Node、无网络、worker UID/GID 65528。
  - `browser-automation-v1`：Playwright/Selenium/Chromium/Weston、worker UID/GID 65529。
- “session-wise”表示每个 root run/lease 使用独立 snapshot、HOME、TMP、workspace 和进程回收边界；不是每个 chat 独占一个新容器。
- 运行环境是固定、预装、不可变 profile；不允许运行时 `apt`/`pip`/`npm` 安装。缺少依赖时在 preflight fail-fast。
- browser 使用 headed Wayland/Weston；不转发宿主 `DISPLAY`，不开放 CDP TCP。
- base/browser 使用不同固定 UID，避免 Linux `RLIMIT_NPROC` 的 host-UID-global 互相影响。
- global `/tmp`、`/dev/shm`、`/workspace` 对 worker 不可写；精确执行树位于 controller-owned private executable tmpfs。
- startup/admission/teardown 做 worker UID sweep 和 shared-state residue audit；setsid/double-fork/refork 真实测试后残留为零。
- SysV/POSIX IPC 由 seccomp 实测 `EPERM`；base 不获得 Chromium namespace capability。
- browser 不设有限 `RLIMIT_AS`，因为 Chromium/V8 使用大规模 sparse VAS；物理内存仍由 3 GiB cgroup 硬限制。base 保留 2 GiB address-space limit。

### 4.5 Browser 网络

- `skill-browser-executor` 使用 `network_mode:none`，worker 只有 loopback、无默认路由、无 Docker DNS。
- 独立 `skill-egress-proxy` 是唯一有 `browser_egress` 网络的 Skill-browser 组件。
- controller 通过固定 loopback bridge 转发到 proxy UDS；worker 不持有 controller/proxy socket authority。
- proxy 只允许公共 HTTP(S) 80/443；private、loopback、metadata 地址返回 403，直连公网/私网均失败。
- Chromium wrapper 拒绝代理覆盖、resolver 覆盖、公开 remote-debug、stealth/anti-evasion、QUIC 和非代理 WebRTC 路径。
- internal/private 浏览继续走已有 legacy browser 的 per-turn policy，不给 Skill browser 全局放权。

### 4.6 2026-07-24 两个 session 的系统性修复

本轮按“持久化对话 + exact Skill + debug/tool/AgentRun”交叉核对：

- `df842a5f2a464e1b924e2794827dd591`
  - `lung-cancer-mdt` 生成了 1705 行报告，但实际没有任何 child Agent；11 个第一轮意见、11 个第二轮意见和投票均由主模型模拟。根因是 instruction-only Skill 没有结构化 workflow，且中文“分别”被 `别` 的否定词规则误判，导致 `delegate_task` 没进入必选能力。
  - 随后的 `visual-browser-operator` 请求只说“使用 Skill”，在多 Skill session 中没有解析为唯一 Skill；因此没有编译 browser profile，模型退化到 broad tools、legacy browser 和无浏览器依赖的 `execute_code`。两个私网目标也未在部署 allowlist 中。
  - 两个 root run 的 debug 终态均是 `task_cancelled`，没有证据证明是 provider timeout；取消来源仍不能从现有日志唯一确定。
- `3bbd719a241d4a23aa65d1dd3ca9846c`
  - `healthsim-trialsim` 的结构化 DAG、intent、7 个 bootstrap、8-worker 路由和第一波 fan-out 均正确编译并真实执行。
  - `worker-safety-extraction` 在已成功完成两次只读 HTTP 后，被 Harness 强制执行下一页 GET；该轮只有一个 `skill_http_get` schema、`tool_choice=required`、2048 输出预算，但 provider 输出 2048 token prose 而没有 tool call，触发 `model_hit_max_output_tokens`。旧恢复逻辑只覆盖初始 required capability，没有覆盖强制 retrieval continuation。
  - 7 个 bootstrap 结果中的显式 `DEGRADED/WARN` 未稳定传播到 authoritative `completion_quality`，造成上层错误升级为 complete。

通用修复：

- 泛化的“使用合适 Skill”请求先走 bounded name/description selector，再绑定唯一 immutable Skill，不再把整套 broad tools 交给主模型自行漫游。
- 用户明确要求独立/分别/并行 Agent 时，runtime-owned required capability group 强制 capability plan 把 `delegate_task` 选为 required；修复“分别”被识别为否定词。
- 强制只读 HTTP continuation 被 provider 忽略时，丢弃 prose 并做一次精确 bounded correction；若仍失败，外层依据副作用 receipt ledger 允许 clean child retry。发生任何 mutating dispatch 时仍 fail closed。
- 未声明完成边界的自由 prose 在 `length` 终态不再被当作完整结果；明确的 `DEGRADED/WARN/降级状态` 会传播到 child、batch 和 DAG completion quality。
- 私网浏览仍需“部署 allowlist ∩ 用户明确 URL”双重授权；当前只加入 `https://10.10.132.126:18443` 和 `https://172.30.100.126:18443`。自签/内部证书只通过 SHA-256 SPKI 精确豁免，未启用全局 TLS bypass。短的“继续/使用这个 Skill”可以引用最近一个用户原创 URL turn，不能引用 assistant/tool 或更早的 ambient URL。
- 用户明确标注的 password/token 只保留为进程内 ephemeral taint：禁止进入文件、代码、memory、Skill state、process argv 或 delegated task；直接授权的 `browser_type` 仍可输入，且输入文本不会进入 debug 参数。

仍需后续设计：

- 对无结构化 workflow 的超复杂 prose Skill，目前可以强制“必须发生真实 delegation”，但还不能仅靠 Harness 机器证明 11 个角色 × 两轮 × Round 3 全覆盖。长期方案是通用 prose-to-typed workflow graph + 独立 instruction-coverage verifier；有结构化 YAML/JSON workflow 的 Skill 已具备完整 DAG 强制能力。
- 历史视觉会话工作区曾生成可能包含用户口令的登录脚本；本轮未擅自删除用户 session artifact。应由用户授权后清理，并轮换该测试凭据。

### 4.7 内容寻址 Workflow、能力目录与运行契约

- 新增通用 `WorkflowIR`：把 `SKILL.md` 及其声明的 Markdown 指令单元编译成内容寻址、强类型 DAG，逐项保存路径与 SHA-256，校验 required node、instruction coverage、依赖和 fan-in，再 lowering 为 worker/wave plan。
- capability plan 必须绑定 Workflow IR 和 exact candidate；root direct call、delegation controller、资源、MCP 和工具均使用稳定 candidate ID/coordinate，required candidate 只能消费一次。
- worker/aggregation 的 effective authority 为 parent authority 与 node authority 的最小交集。controller-only 节点只有在 exact backend binding、预加载输入摘要和预期结果路径全部冻结后，才允许零工具执行。
- 委派前和结束时均重新计算 Skill authority、instruction source 和资源 SHA-256；任何 TOCTOU 漂移、目录缺失或 content digest 不一致均 fail closed。
- required candidate 失败不得静默降级；只有携带 exact `CAPABILITY_GAPS_JSON` 和显式 degraded result 才能由上层合同判定。子模型异常仅在父级 dispatch audit 证明零 mutation 时允许 clean retry，mutation 或状态不确定时终止。
- 新增 machine-readable run lifecycle 与 typed receipt ledger：retry revision、终止事件和 projection 一致性都在 authoritative complete 前验证；同一 identity 的精确 replay 幂等，冲突 payload 被拒绝，首个 authoritative terminal 胜出。
- capability catalog compiler、dynamic amendment 和初始编译均有同一安全边界。编译失败使用稳定错误码 `capability_catalog_compilation_failed`，撤销旧 authority 并阻止同批调用，不泄露内部异常字符串。
- MCP catalog 现在冻结完整 input schema、版本和内容摘要，状态明确区分 `resolved`、`not_enabled`、`freeze_failed`；冻结失败后 direct/deferred/child 均不得回退到 live catalog。
- Skill loader/manager 以原始 bytes 计算 digest 后再解码，CRLF 文件也能保持 source content 与 authority SHA 一致。
- Backend 的 `agent_run_events` 启动迁移先按最早 rowid 去重，再创建 `(conversation_id, run_id, event_type, seq)` 唯一索引；`tool_name` 扩至 512，顶层 authoritative 语义与 Harness adapter 一致。

### 4.8 2026-07-27 Skill 路由、浏览器执行与流终止观测

本轮按“对话上下文 + exact Skill 包 + Harness/Backend debug”交叉审计
`17ac2a581e5d4b469ccabbbf9f4f4a55`：

- 第一轮 V2.3 请求的词法相关度只有 6，semantic selector 又发生 `ReadTimeout`，所以历史 Harness 没有选中任何 Skill，而是按普通复杂任务运行；工作区只有一个 90,907-byte Development Plan，没有进入该 Skill 声明的模块 cohort、强终稿和 mandatory merge 合同。
- 同一次 ZIP 上传在数据库中是 1 个根 Skill 加 18 个 supporting Skill；旧 API/UI 把平铺记录都当顶层 Skill，所以再导入 browser Skill 后显示为 19 个。现在新上传持久化 archive SHA-256 `bundle_id`、`primary/supporting` role、root 和 source path；历史数据只在同 scope、同精确创建时间且唯一 primary 时保守投影。Backend、Harness 和 Frontend 共用同一 bundle 身份链。
- 历史 browser turn 已成功 `browser_navigate`，但旧 capability plan 随后只强制 `web_search`；provider 继续尝试另一个浏览器工具名并产生冲突/破损 JSON。现在“访问具体站点并在站内搜索”编译为独立的 `browser_navigate`、`browser_type`、`browser_click` required receipts，不再用 metasearch 替代页面内操作。

通用修复如下：

- 复杂或明确要求 Skill、但 name/description 路由未命中时，在 semantic selector 前增加最多 64 包的 loader-owned declared-route fallback。只有一个可见顶层包的非 default route 明确匹配才允许一次 `skill_view` 检查；它不授予执行、资源或工具 authority。
- 完整包 inventory 仍参与依赖/Workflow 编译，supporting member 只从顶层 relevance/routing 视图隐藏；无 primary、跨 scope、无效 SHA、重复或孤儿 registry row 均 fail open 为独立 Skill，不能吞掉其他包。
- 新增严格 `chatds-runtime.json` schema v1，并支持 `package.json/chatdsRuntime`。固定 runtime profile、依赖、命令、entrypoint、package/script/manifest SHA；未知字段、路径覆写、manifest drift 和未精确声明的动态依赖 fail closed。历史 visual 包没有该 manifest 时仍走已有 native browser action lane，不猜测执行动态 Node loader。
- tool-call stream debug 新增 logical call 数、name fragment 长度以及 exact exposed/prefix/foreign/empty 分类；不保存原始未识别工具名、参数或模型正文。
- Backend 每条 SSE bridge 现在记录 upstream/downstream 状态、chunks/bytes/parse errors、最后事件与根事件、root phase、provider typed failure、未满足合同摘要及终止来源。终止来源区分 Harness complete/failed/cancelled、provider failure、timeout/connect/HTTP error、`generator_closed`、`service_shutdown`、`downstream_send_failed`、有 ASGI `http.disconnect` 证据的 `client_disconnected` 和证据不足的 `asyncio_cancelled_unknown`。
- 安全终止事件同时进入 `agent_run_events` 和 `workspace/debug/backend_streams/<run_id>.jsonl`；不记录请求头、URL、正文、工具参数或凭据。Nginx 生成的 `$request_id` 只作为安全 correlation label 传给 Backend，并在无终态的前端错误中显示。Nginx access log 只记录 `$uri`（不含 query）、status/upstream status/耗时/request ID。
- Nginx SSE read/send timeout 从 1800 秒提高为 3600 秒，保持 `3600 > Backend→Harness 3000 > provider 2400`，避免代理成为第一条长流硬上限。
- Frontend 不再把 `onChunk` 回调异常吞成 malformed SSE；缺少 durable `stream_terminal` 时保留部分草稿并明确失败。
- 断连后的 terminal projection 先进入 conversation barrier。成功 task 可清理；失败、取消或非 durable task 必须由下一 turn 明确消费并拒绝，不能在 callback 中遗忘。只有真实 `http.disconnect` 才归因客户端断连，Starlette 包装的 send failure 保持为 `downstream_send_failed`。
- package-controlled route ID 只允许安全标识字符原样进入 lifecycle/debug；其他内容仅保存 SHA-256 correlation，避免 URL 或敏感文本进入持久化事件。

历史第一轮为何取消仍无法唯一追溯：旧证据只能证明 provider request 已开始、没有
`debug.llm.finish`、admission 随后释放且根事件为 `run.cancelled/task_cancelled`。无法在事后区分浏览器/代理断连、Backend task cancellation 或服务 shutdown。新版能完整覆盖系统实际观测到的边界；进程被强杀、日志丢失或系统外网络故障仍必须保留 `unknown`，不能虚构原因。

### 4.9 2026-07-28 `79c170...` 断线取证与生命周期闭环

本次严格按“持久化会话 + exact Skill + Backend/Harness debug”交叉核对
`79c1701baaba4a6195d740e9a238b3d0`：

- exact ZIP 的 SHA-256 为
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`；
  包内 19 个 `SKILL.md` 是 1 个 primary 与 18 个 supporting member，不是 19 个独立顶层
  Skill。
- root run 从约 08:56 运行至 11:50，route 为
  `composite_full_protocol_design`，execution 为 `artifact_workflow`；7 个 bootstrap
  全部结束为 degraded，8 个 required worker 已真实规划，并非主模型伪造 multi-agent。
- Backend 有正面的 ASGI `http.disconnect` 证据：
  `termination_source=client_disconnected`、`exception_class=CancelledError`、
  `root_terminal_status=missing`、`root_phase=executing`、
  `last_root_event_type=tool.dispatch_started`。上游仍连接，已接收 1372 个数据 chunk，
  parse error 为 0，已向前端发送约 2.33 MiB；没有 provider typed failure。因而黑屏/
  `NetworkError` 是客户端连接中断，不是模型自行停止。
- 断开时 `worker-safety-extraction` 已 completed_degraded，并保留约 17.8 KiB 内部结果；
  PICO 与 termination-analysis 被旧的固定 3600 秒 delegate batch deadline 取消；AE、
  target-biology、competitive-landscape 正在运行并随浏览器连接断开取消；literature、
  I/E、aggregation、11 个模块报告和 final report 尚未到达。因此 workspace 没有业务
  Markdown 是执行阶段事实，不是已生成文件丢失；已完成的内部 child payload 位于该
  session 的 `results/`。
- 页面上 19 个 failed 标签是 tool attempt，不是 19 个 Agent 都失败：4 次是模型猜错
  Python callable，在 dispatch 前被拒绝；1 次是 URL 超出 exact Skill closure，被安全
  策略正确拒绝；其余 14 次是 HTTP 400/404、受限脚本 DNS、ClinicalTrials 脚本退出、
  metasearch 无可接受结果等真实远端/API失败。DrugBank 另需授权数据，PrimeKG 脚本
  依赖缺失的本地 CSV。不能把这些混写为一个“delegate failed”。

通用修复：

- Backend 在请求被接受后以独立 producer 执行；SSE subscriber 只是有界观察者。浏览器
  断开只 detach subscriber，不再取消 root run、provider stream 或 child DAG。relay
  上限为 256 chunks/4 MiB，慢客户端等待 5 秒后脱离，避免断线保护反过来造成无界内存。
- root 在 durable assistant/terminal barrier 后才完成；title generation 移出该 barrier。
  第一个持久化 authoritative terminal 永不翻转；缺失 root 终态会合成 `run.failed`。
  Backend 启动时修复旧 orphan active rows；若已有 durable terminal，只补 projection，
  不制造冲突终态。
- run cards 和 AgentRun DTO 有统一上限、截断元数据、global active truth、orphan child
  projection 与刷新重建；semantic worker identity、batch、completion quality 和失败原因
  持久化，刷新后不再消失，也不再显示无意义的 `delegate-1/2/3`。
- delegate batch 的旧固定一小时墙钟改为 material-progress soft lease；provider admission
  wait 不消耗软租约，另设独立 21600 秒 hard deadline。到期或父级取消时先撤销 child
  execution fence，再有界关闭 HTTP/MCP/browser/process/provider 资源并取消任务。
  fence 在 registry、文件、HTTP、MCP、浏览器、进程和 artifact commit 边界复核，防止
  cancellation-resistant child 在父级结束后继续产生副作用。
- provider pump 改为单槽有界队列并在每次 publish 前验权；同步 resource closer 被拒绝，
  内置 closer 均为 async；child 自己的 `TimeoutError` 不再伪装成 batch timeout。
- Skill ZIP 三类入口统一 per-user/scope 锁、staging/no-clobber、取消安全提交和 exact retry；
  数据库增加 scope/name 唯一约束及旧库去重迁移。bundle runtime 按 `bundle_id` 内容寻址；
  delete 使用 quarantine+journal，promote/fork 使用双锁、文件/DB/MCP 同边界恢复；fork
  保留 source snapshot digest、workspace、Skill、runtime、消息和 bundle metadata。

### 4.10 Knowledge Gate 条件能力编译闭环

- Loader 对每个 worker 的 `knowledge_gate` 做有界符号编译并签名；最多 128 个
  checks。旧式平铺 `tools: [...]` 明确解释为一个 `one_of`，显式
  `tool_groups`/`tools: {all_of: ...}` 表示 AND-of-OR，不从自然语言猜布尔语义。
- 第二阶段只从当前 run 的 native registry、frozen MCP descriptor、Skill
  package/resource/script/command/HTTP grant 中解析 exact candidates。计划和候选权限
  分离，plan 本身不是 authority。
- 决策前模型只看到动态 schema 的 `submit_knowledge_gate_decisions`；check ID 枚举、
  数量和 `yes/no/unknown` 均由签名 plan 精确约束，禁止同 batch 混入候选调用。
- 接受决策后才按选中分支原子激活候选权限；Skill package/main/resource、MCP
  descriptor 和命令/HTTP 坐标在派发前再次校验。未选分支始终不可见。
- 多个 AND 组必须由 distinct dispatch receipts 满足；HTTP receipt 使用
  handler-owned `matched_skill` 与 canonical-prefix SHA，不把原始 URL/query 写入 debug。
  失败组进入结构化 gap ledger，不能靠模型声称完成。
- direct/primary chat 会无条件剥离内部决策工具；只有 delegated run 且 plan digest 与
  exact conditional authority 验证成功后才恢复。
- compiler、runtime 和 delegation 共用 NFC/Unicode/160 字符 canonical ID 合约；
  支持中文和下划线开头，非法、非 NFC 或超长 ID 在编译期 fail-closed。
- Debug 增加 `knowledge_gate.plan.bound/compiled`、`decision.accepted`、
  `group.receipt`、`activation.failed` 和 delegated `final_audit`，只记录安全 ID、
  数量、摘要和终态。

### 4.11 2026-07-29 `0147f...` 失败闭环与通用执行加固

本轮继续按“持久化会话 + exact Skill + Backend/Harness debug/tool/AgentRun”交叉核对
`0147f478e52841fa8ed50ffd0a364506`：

- `pending inspection receipts` 旧文案被前端误读为资源读取失败；它实际表示工作流仍在等待
  检查回执。delegate 的安全参数投影只显示通用占位符，也使正常 dispatch 难以定位。
- FDA/EMA/target/competitive bootstrap 中既有真实远端失败，也有成功但不满足证据合同的
  模型结果。旧逻辑会把仅有 preload、免责声明或模型自报字段当成成功 receipt，DrugBank
  等来源因此可能出现“无真实取证却填充事实”的假阳性。
- ICH 文档中的普通标题被旧 `DEGRADED/WARN` 解析器误判为降级；相反，Markdown 形式的
  显式 YES 有时又被误判为缺失。
- 根 run 完成 bootstrap 后进入 declared-route Knowledge Gate 编译时，
  `loaded_packages` 只在 explicit-skill 分支初始化，触发未捕获 `NameError`。旧 Harness
  producer 只写入 EOF sentinel，没有 authoritative terminal event，所以前端最终显示
  `Harness stream ended without a terminal run event`。它不是单纯网站不可达。

通用修复：

- explicit、declared-route 和 semantic selection 统一创建 run-scoped frozen package
  snapshot；intent/route 重编译、reference amendment 和依赖闭包始终复用同一内容寻址
  身份。运行中 package drift 会撤销全部派生权限并 fail closed。
- optional Knowledge Gate 候选只有在 package/resource/snapshot 审计全部成功后才获得
  HTTP、script、command、MCP 或 tool authority；坏 OR 分支保持 unresolved，不能因同组
  另一个候选成功而泄漏权限。
- 新增严格 `COMPLETION_QUALITY_JSON`、`CAPABILITY_GAPS_JSON` 和
  `KNOWLEDGE_GAPS_JSON` 校验。Harness receipt ledger 覆盖模型自报；仅 preload 不算证据。
  当任务要求 acquisition/bootstrap/retrieval 时，零成功 evidence receipt 却填充 typed
  facts 必须失败，合法 nullable/degraded envelope 则可完成为 degraded。
- delegate 的安全投影现在保留任务数量与语义名称，资源状态改为
  `pending inspection receipts`；既不泄漏大参数，也不再显示无意义的
  `delegate-1/2/3`。
- `run_stream`、Harness wrapper 和 SSE producer 都会为未捕获异常或正常 EOF 缺终态生成
  唯一、安全、可持久化的 `run.failed`，错误码区分
  `missing_terminal_event` / `harness_lifecycle_error`；启动前非法参数仍严格抛出。
- completion-quality legacy parser 修复标题假阳性和 Markdown YES 假阴性；精确 gap
  ledger 对重复块、超限 JSON、非有限数值和歧义结构 fail closed。

## 5. 当前验证证据

2026-07-29 当前功能提交 `7bbc0809` 已通过：

- Harness 全量：
  `1585 tests OK, 1 skipped`，0 failures/errors。
- Backend：compileall 通过，`121 passed`；Frontend：`18 passed`，production build
  通过，仅保留既有约 694.5 KiB chunk warning。
- 候选镜像内两组聚焦回归合计 `262 tests OK`；生命周期/Skill 路由、quality、snapshot、
  optional authority、receipt 和 terminal fallback 定向验证均通过。
- clean Git archive 构建的 Harness、Backend、Frontend、Browser 候选均通过隔离 smoke：
  base executor startup reap、Harness health/model/auth、Backend migration/SQLite、
  Frontend Nginx/反代，以及 legacy Browser UDS/CDP 打开 `https://example.com/`。
- `py_compile`、`git diff --check`、genericity scan、secret scan 通过；独立审阅未发现
  P0/P1 blocker。
- 本轮未执行模型重型 V2.3 E2E。

2026-07-28 功能提交 `6785e443` 此前已通过：

- Harness 全量（`cd harness && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=..:.`）：
  `1565 tests OK, 1 skipped`，0 failures/errors。
- Backend compileall 通过；pytest 为 `121 passed`，只有既有 `crypt`/
  `datetime.utcnow()` deprecation warnings。
- Frontend 无本轮代码变化：`18 passed`，production build 通过；全目录 ESLint 仍只有
  既有 `ModelSelector.jsx`、`SkillLibrary.jsx` 两处
  `react-hooks/set-state-in-effect`，不属于本轮差异。
- Knowledge Gate compiler/runtime/delegation/AgentLoop/能力隔离聚焦组合
  `155 tests OK`；生产容器内 compiler/runtime/delegation/AgentLoop smoke
  `49 tests OK`。
- V2.3 ZIP 的零模型静态编译覆盖 9 个 workers、59 个 checks、58 个条件 OR 组、
  101 个 selector occurrences；0 个空组，0 个 Knowledge Gate error/warning。
- `py_compile`、`git diff --check`、staged secret scan 和生产代码 genericity scan 通过；
  两轮独立 release review 最终无 P0/P1 blocker。
- 本轮未执行模型重型 V2.3 E2E。

2026-07-27 已冻结并部署的 `c21deca0` 此前还通过：

- Nginx 配置 `nginx -t` 通过；Frontend SSE 3600 秒和 `X-Request-ID` 响应头已在生产验证。
- Workflow IR、run contract、MCP、delegation、gate 和 catalog failure 聚焦组合：`276 tests OK`；instruction-source 模块 `24 tests OK`；terminal/run-contract 模块 `170 tests OK`。
- legacy browser sidecar：`8 tests OK`。
- Executor/browser/profile/topology/proxy：`86 passed, 1 skipped, 43 subtests passed`。
- 最终 Shell/profile 定向：`78 passed, 40 subtests passed`；独立 reviewer 的 23-case Bash 矩阵也通过。
- `compileall`、`git diff --check`、genericity scan、staged secret scan、默认 Compose config、`local-search` profile config 均通过。
- 独立最终审计未发现 P0/P1 release blocker。

真实容器验收与生产部署使用同一组 executor/proxy 镜像：

- browser：`sha256:76acea01fdf89f324fef6c48e44d6270841bbb8127887e8cf2e082cd76a84b90`
- base：`sha256:a7afa67c6c2f0ffe08e27cd8b5b5101b08444e71e6008b85efacf9c6784ad14f`
- proxy：`sha256:c5ee4fdc2ee785868f15036706f01d327b05b358f2b7812fcca8bfb7454f9c05`

真实 `run_skill_process` 四路均通过：

- base identity；
- Bash 通过 `${SKILL_DIR}` 直接执行 `0550` shebang helper；
- headed Node Playwright；
- persistent Python `BrowserProbe`。

每条路径的 package/entrypoint digest 与 lease attestation 一致，close 后 manager/worker residue 为零。历史 `8e486.../visual-browser-operator` 的 `ChromeVisualSession` 也完成 observe/open/artifact/close；目标站返回 202→400 属于上游行为，不是缺 Playwright/Selenium/Bash。

## 6. 生产与部署状态

- 当前生产主机：本机 `10.10.132.126` / `172.30.100.126`。
- 原生产主机：`10.10.130.178` / `172.30.100.145`，已下线。
- Compose project：`chat_ds`。
- 生产工作目录：`/nfs/yangbb/codes/chat_ds`。
- 前端：`http://10.10.132.126:5173`、`http://172.30.100.126:5173`。
- Harness 使用同机 SearXNG `http://10.10.132.126:8088`；既有 SearXNG/Valkey 在切换
  中未重建，健康状态和数据卷保持不变。
- 2026-07-29（Asia/Shanghai）完成 `7bbc0809` 完整生产迁移：
  - 先确认旧生产 active run、未结束 run 和 5173 established connection 全为 0；
  - 停止旧 Frontend 后再次确认，再停止旧 Backend/Harness/所有执行器和 Browser；
  - 旧 SQLite `quick_check=ok`，核心表计数为
    `195 / 757 / 373 / 56048 / 327`；
  - 以只读 tar stream 迁移 Docker volume，源/目标文件均为 105,021,440 bytes，
    SHA-256 均为
    `38e788407247862f95e5bc84d8f75674aa9bb66b6366c46affd5810d944de10b`；
  - 本机被替换的旧测试 DB 已备份到
    `/nfs/yangbb/chat_ds_backups/20260729_114726_pre_local_migration/` 并通过 checksum；
  - 新生产验收通过后，旧主机执行 `docker compose down`（未使用 `-v`），项目容器和
    5173 listener 均为 0；旧 `chat_ds_chat_ds_db` 卷仍保留且哈希不变。
- `.env` 已原子生成独立 `EXECUTOR_V2_AUTH_TOKEN`，mode 为 0600；base/browser/Harness 三方值一致且长度合规，值未输出或写入 Git。
- 当前非 root 运维用户不能直接读取 `.env`。不要把 `.env` 通过 `/dev/stdin` 交给
  Compose：Compose 会重复读取并可能渲染为空。需要时在隔离 subshell 中从只读挂载解析
  环境，并配合 `--env-file /dev/null`；不得输出或落盘 secret。
- Harness stream ceiling 为 2400 秒，Backend proxy deadline 为 3000 秒，Frontend Nginx SSE deadline 为 3600 秒。

当前生产镜像：

| 服务 | Image ID | 状态 |
|---|---|---|
| `chat_acits_executor` | `sha256:a7afa67c6c2f0ffe08e27cd8b5b5101b08444e71e6008b85efacf9c6784ad14f` | healthy |
| `chat_acits_skill_egress_proxy` | `sha256:c5ee4fdc2ee785868f15036706f01d327b05b358f2b7812fcca8bfb7454f9c05` | healthy |
| `chat_acits_skill_browser_executor` | `sha256:76acea01fdf89f324fef6c48e44d6270841bbb8127887e8cf2e082cd76a84b90` | healthy |
| `chat_acits_browser` | `sha256:08bcf8860c10ba8fcd647b6d1a96c2c12e13e46db800c812acea82e17007240c` | healthy / restart 0 / revision `7bbc0809` |
| `chat_acits_harness` | `sha256:9cdf01fe2074b2ea5bbc6dd69d11df7a000926e62ad46d261e7afc3d1fe690ca` | healthy / restart 0 / revision `7bbc0809` |
| `chat_acits_backend` | `sha256:cc5a2dad4f18cd4703ece288965629d0caa175177bd13425d433825fe8edbb8c` | running / restart 0 / revision `7bbc0809` / `/api/health` 200 |
| `chat_acits_frontend` | `sha256:48e48710856eaa1b84e975ed4daeedc36f1416d7444979d60cff3907cfc7f91a` | running / restart 0 / revision `7bbc0809` / `/` 200 |

生产 smoke 证据：

- `127.0.0.1`、`10.10.132.126`、`172.30.100.126` 的 Frontend `/` 和
  `/api/health` 均为 200。
- base executor、browser、skill egress proxy、skill-browser-executor 健康；Harness
  `/health`、`/v1/models` 为 200，模型数为 2；未鉴权 `/internal/*` 为 401。
- 生产 SQLite `quick_check=ok`，核心表计数与源端一致，`active_runs=0`，并确认
  `0147f478e52841fa8ed50ffd0a364506` 会话存在。
- SearXNG 真实 `OpenAI` 查询返回 14 条结果；Valkey `PONG`。
- 四个应用容器 revision label 均为完整提交
  `7bbc08097a75c618fc8a7338ff96b6577b8772d4`；所有长期容器 restart 均为 0。
- executor/proxy/skill-browser/Harness/Backend/Frontend 日志未发现 traceback、
  critical、fatal、unhandled、ProtocolError 或 exception。
- 本轮没有运行模型重型 V2.3 E2E；下一项仍是用户手工业务验收。

回滚点：

- 原 executor/browser/Harness/Backend 镜像保留 tag `rollback-20260723-pre-process-v2`。
- 本轮切换前 browser/Harness 镜像另保留 tag `rollback-pre-e90415a0`；新镜像 tag 为 `deploy-e90415a0`。
- `5a7f21d9` 切换前 Backend/Harness 分别保留 `rollback-pre-5a7f21d9`；当时新镜像分别标记为 `chat_ds-backend:deploy-5a7f21d9`、`chat_ds-harness:deploy-5a7f21d9`。
- `c21deca0` 切换前 Backend/Harness/Frontend 均保留 `rollback-pre-c21deca0`；当时新镜像分别标记为 `chat_ds-backend:deploy-c21deca0`、`chat_ds-harness:deploy-c21deca0`、`chat_ds-frontend:deploy-c21deca0`，三者 revision label 均为 `c21deca0`。
- `da70dc51` 切换前 Backend/Harness/Frontend 均保留
  `rollback-pre-da70dc51`；当时候选镜像分别标记为
  `chat_ds-backend:deploy-da70dc51`、
  `chat_ds-harness:deploy-da70dc51`、
  `chat_ds-frontend:deploy-da70dc51`，三者 revision label 均为 `da70dc51`。
- `6785e443` 切换前 Backend/Harness 分别保留
  `chat_ds-backend:rollback-pre-6785e443` 和
  `chat_ds-harness:rollback-pre-6785e443`；候选镜像分别标记为
  `chat_ds-backend:deploy-6785e443`、`chat_ds-harness:deploy-6785e443`。
- 本机 `7bbc0809` 切换前 Harness/Backend/Frontend/Browser 分别保留
  `rollback-pre-7bbc0809-local`；候选镜像均保留 `deploy-7bbc0809` tag。旧主机
  Compose 已 down，但旧数据库卷和旧镜像未删除。
- 可重建的旧 Harness 代码镜像：`chat_ds-harness:rollback-d224db33`，image `sha256:e7d16ee538fc69e638f20bb93035df90d76008721116ebfedb7d07ccb986abef`。
- `c21deca0` 的 Backend/Harness 从只包含三项服务目录的 clean Git archive 构建。Docker Hub metadata 临时连接重置时，Frontend 使用已经本地验证的同一提交 `dist`，在 `rollback-pre-c21deca0` 的既有 Nginx runtime 上清空旧静态文件后封装；配置和资源 marker 均做了生产验证。部署上下文/日志位于生产主机 `/tmp/chat_ds_deploy_c21deca0/`，不属于 Git。
- `da70dc51` 的 Backend/Harness/Frontend 源码均来自 clean Git archive
  `/tmp/chat_ds_deploy_da70dc51.4O3a8d/`。Frontend 遇到 Docker Hub Nginx metadata
  timeout 后，在该归档中用固定 Node 镜像重新构建 `dist`，再基于
  `rollback-pre-da70dc51` 的既有 Nginx runtime 封装；候选镜像先通过同网络
  `nginx -t`、`/` 和 `/api/health`，再切换生产。
- `6785e443` 的 Backend/Harness 源码来自 clean Git archive
  `/tmp/chat_ds_deploy_6785e443.sLSaCZ/`；候选镜像先通过无模型 registry/import
  smoke，切换后再通过容器内 49 项回归和 HTTP/log/restart 检查。

## 7. Git/worktree 边界

提交必须显式列文件，提交前执行：

- `git diff --cached --name-status`
- `git diff --cached --check`
- staged secret scan
- 确认两个 tracked deletion 未 staged

不要提交下列 runtime/reference/upstream 目录：

- `data/skills/**`
- `data/workspace/**`
- `data/runtime_envs/**`
- `harness/data/memories/**`
- `workspace/**`
- `skills_and_refs/**`
- `searxng-master/**`
- `CodeWhale/**`
- `gal3_ad_cdp/**`
- 异常路径 `harness/"`

## 8. 凭据操作

- 生产凭据：`.local_secrets/remote_10.10.130.178.env`。
- 搜索机凭据：`.local_secrets/remote_10.10.132.126.env`。
- `.local_secrets` 保持 0700，文件保持 0600；生产 `.env` 保持 0600。
- 当前执行用户不能直接读取 `.local_secrets`；需要时只允许用临时只读容器把内容送入当前 shell 的 `source`，绝不打印、复制或持久化。
- SSH 使用 `sshpass -e` 和环境变量，命令结束立即 unset；禁止 `set -x`、`echo` secret 或输出容器环境。

## 9. 用户手工 V2.3 E2E 后

测试资产在 `skills_and_refs/`：

- `xClinicalTrial-Design-V2.3.zip`
- `xClinicalTrial_Design_V2.3.html`
- `GAL3_AD_FULL_REPORT_v2.3_glm52.md`
- 旧版对照资产

拿到新 session ID 后：

1. 同时读取 debug/AgentRun/tool events、持久化对话、该 session 的 exact Skill。
2. 统计每次模型调用的 provider/model、input/max-output、reasoning/visible/tool fragments、finish reason、elapsed、retry/continuation。
3. 核对 capability plan、worker DAG/receipts、search/MCP、process leases、workspace artifacts 和 strong-final cohort。
4. 将终稿与 ground truth 做结构、覆盖、证据链、表格、附录、traceability 和可用性对比，不要求逐字节相同。
5. 只修复跨领域可复现的通用根因，并增加非 V2.3 特定回归。

## 10. 已知非 blocker 边界

- V2.3 与 ground truth 的业务级一致性仍需用户手工真实模型 E2E；基础回归不能替代这项验收。
- Legacy `knowledge_gate.checks[].tools` 只能安全解释为单个 OR 组；需要多个独立
  必须条件的 Skill 应显式使用 `tool_groups` 或 `tools: {all_of: ...}`。Harness 不从
  自然语言 action 猜 AND/OR。
- 某个 supporting Skill 若没有可证明的 exact script/command/HTTP/native/MCP route，
  该候选会保持 unresolved；同一 OR 组的其他精确候选仍可执行并形成降级证据，Harness
  不会把说明文字升级为执行权限。
- 同一事件循环里的任意同步阻塞无法被 asyncio hard deadline 抢占；内置长操作已使用
  async/sandbox，真正要强杀任意同步 Python 仍需进程级隔离/watchdog。deadline 后返回还
  可能包含两段有界 resource-close/child-cancel grace。
- cancellation-resistant 第三方协程可能存活到自身返回，但 fence 已撤权、provider 队列
  有界且不能再 dispatch/commit；极小 dispatch-start/fence race 会安全侧误判为
  non-retryable，可能少重试一次，不会重复副作用。
- 兼容 fork API 在客户端未传 `fork_id` 时仍由服务端随机生成；若服务端已完成但响应前
  断网，旧客户端不知道幂等键，自动重试可能产生第二份 fork。后续应由前端预生成
  `fork_id`，并增加启动/周期 orphan journal reconciler。
- Agent event 当前按事件即时持久化，长 run 的事件规模仍可能形成较高写放大；后续可做
  有界批处理。assistant projection 也还没有数据库级 run→message exactly-once 外键。
- session-wise 隔离是固定容器池内的 lease/root-run 隔离，不是每 chat 动态创建容器。
- 依赖 profile 固定且不可运行时安装；复杂动态 Bash/Node/Python 需 exact marker/manifest。
- browser Skill lane只允许公共 HTTP(S)，不支持 CAPTCHA、stealth、反爬绕过或未确认的重要操作。
- stdio MCP 已降权和隔离 ambient secret，但不是完整 mount/network namespace；仍只注册可信配置。
- 免费搜索引擎健康度、CAPTCHA、协议变化和上游站点 4xx/5xx 是动态外部条件，不能误归因为 Harness 回归。
- Workflow IR 当前能机器证明结构、source digest、required-node 与结果路径覆盖，但结构覆盖不等于业务语义质量证明；长期可增加逐 instruction evidence ledger。
- 预加载给 controller-only child 的 instruction source 仍可能同时存在于只读 resource grant；authority 已精确且无安全越权，但后续可进一步禁止冗余读取。
- 数据库唯一索引允许不同 terminal event type 使用同一 seq；当前 projection 以首个 authoritative terminal 为准且不会翻转，未来可增加更强的跨 event-type 存储层终态约束。
