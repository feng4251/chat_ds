# ChatDS 当前会话交接（2026-07-31）

> 本文件是本仓库唯一的权威续接入口。新 Codex/Claude Code 会话必须先完整阅读本文件，再查看 Git、测试和生产状态。旧 `_SESSION_*.md`、`_HARNESS_*.md`、`_REMOTE_OPS.md` 只用于历史追溯。

## 1. 当前结论

- 工作目录：`/nfs/yangbb/codes/chat_ds`。
- 分支：`fix/generic-skill-harness-20260717`。
- 2026-07-31 当前功能提交：
  `17e261ef fix: harden generic workflow evidence convergence`。该提交继续保留现有
  AgentLoop、内容寻址编译器、Workflow IR、
  session sandbox 和 exact authority/receipt 主链；没有切换 LangChain/LangGraph/
  Deep Agents 主循环。该提交补齐 handler-owned Knowledge Gate typed receipt、
  exact Skill-resource preload receipt、按 retrieval family 隔离失败、正文与终态
  质量元数据分离、稳定失败 taxonomy 和跨独立步骤 common-mode breaker，并加入
  可复用 `ScriptedProvider` 边界测试夹具。
- 2026-07-30 当前功能提交：
  `82c818fc fix: close generic skill workflow contracts`。
- `82c818fc` 保留现有内容寻址 Skill 编译器、typed Workflow IR、统一沙箱、
  delegation receipt 和 durable run-event 架构，没有把主循环替换为 LangChain
  Deep Agents。Deep Agents 的 middleware、文件式上下文卸载、命名 subagent 和
  durable execution 思路有借鉴价值，但直接引入第二套主循环会分叉当前已经建立的
  authority、egress、workspace CAS、终态和事件落库语义，收益小于迁移风险。
- 本轮闭合的是通用 Harness 契约：
  `delegate_task` 公共 JSON Schema 与内部校验器使用同一组 exact egress 字段；
  preflight 拒绝统一标记为 `actual_dispatch_attempted=false`；
  结构化角色/轮次/fan-in Skill 可零样本识别为声明式多 Agent workflow；
  子 Agent 在 spawn 前失败也会持久化语义化 `agent.spawned` 与权威
  `run.failed`；Python Skill 的 package-data 相对路径通过有界 AST 推导正确 cwd；
  Backend 即时终态投影与重启 reconciliation 都持久化 `finish_reason`。
- `82c818fc` 已从 clean Git archive 构建并部署本机生产，仅替换 Harness 与
  Backend。两者 revision 都是完整提交
  `82c818fc6d7eb135e63d74f3b176c4b56bf4947e`；Frontend、四个统一沙箱、
  egress proxy、Browser、SearXNG/Valkey 和数据库卷均未重建。
- 2026-07-30 最新功能提交：
  - `100f42ba fix: harden bounded skill egress lifecycle`
  - `f1e59c20 test: inspect denied CONNECT requests end to end`
- `100f42ba` 将统一 session sandbox 的签名出网协议升级为强制 policy v3：
  每个 root run 的所有 one-shot、persistent process、delegate 和 retry 共用 Proxy
  预算 scope；调用级 identity、exact authority 与预算都受 HMAC 绑定。Proxy 对请求数、
  client→Proxy wire bytes 和 Proxy→client wire bytes 做跨连接原子累计，GET/HEAD body、
  超限 query/header/body、未授权 method/origin/path 和预算越界均 fail closed。
- Bridge 的调用级 audit 只作为本地遥测，不再冒充 Proxy 跨调用账本的终态证明。
  因此任何 controlled-egress effect receipt 当前都明确为
  `effect_known=false/replay_safe=false`；联网子任务在 wrapper/流异常后不会自动重放。
- one-shot 和 persistent process 的 Bridge seal、expiry、ACK、janitor、shutdown 和
  controller reap 已形成同一隔离闭环。终态 audit 缺失或清理失败时保留 exact
  Bridge/lease/admission 并 quarantine；失败不会丢 handle、占死其他 lease 或杀死
  janitor，只有后续 seal 与 worker containment 都成功才重新入池。
- `f1e59c20` 修正真实网络验收对 policy-v3 CONNECT 时序的理解：本地
  `200 Connection Established` 只建立可检查隧道，不代表目的地已获授权；探针会继续
  完成 Proxy MITM TLS 并要求未授权内层请求得到 403。
- 上述三个 clean-archive 候选镜像已在该轮部署：四个 session-sandbox、
  `skill-egress-proxy` 和当时的 Harness revision 均为完整提交
  `f1e59c20129d9c3ba91b0f80850983e93d24d9dc`，全部 healthy、restart 0。Backend、
  Frontend、legacy Browser 和数据库未重建。
- 2026-07-30 上一轮功能提交：
  `2486f008 fix: harden generic skill execution convergence`。该提交系统性修复了
  mandatory retrieval 调度/收敛、provider 长流 deadline、TLS 1.3 上游兼容、
  intent typed-result 验证、只读 Skill 调用的 effect receipt/retry 判定和静态
  authority 可观测性；没有加入 V2.3、疾病、文件名或 session 特判。
- `2486f008` 已从 clean Git archive 构建并只替换生产 Harness 与
  `skill-egress-proxy`。该轮部署时两者 revision 都是完整提交
  `2486f008b19f760d0fe63111137feb9d103a1a45`，健康且 restart 0；三个 Frontend
  `/api/health` 入口均为 200。Backend、Frontend、四个 session-sandbox 和 legacy
  browser 未重建。
- 当前生产 Harness 功能 revision 为
  `17e261ef61b913e804e9875a8010480edfb5081a`；Backend 保持
  `82c818fc6d7eb135e63d74f3b176c4b56bf4947e`。交接文档可另有 docs-only HEAD。
- 2026-07-30 其他基础功能提交：
  - `b4e8dc18 fix: require durable delete intent for orphan cleanup`
  - `c62a4a69 feat: unify session sandbox and harden session lifecycle`
  - `304781c8 fix: move workspace locks off NFS`
- `b4e8dc18` 修正了 startup/periodic reconciler 的删除授权模型：数据库中没有
  conversation row 只表示“当前无法证明归属”，不再自动生成 tombstone 或删除
  workspace/Skill。只有数据库缺失且已有经过严格复核的 durable deletion tombstone，
  才允许进入清理；pending journal、无 fence 孤儿、损坏 marker 和竞态漂移均保留并
  typed defer/fail closed。
- `b4e8dc18` 已从 clean Git archive 构建并只替换生产 Backend；该轮 Backend image 为
  `sha256:42c62055effbece0a6c3aedb5011baf7f1ed226dc6db9fbd2df3d5794688be2a`，
  revision 为完整提交 `b4e8dc18f315995354798910edb4c77f6da2b252`。Harness 继续运行
  兼容的 `304781c8`，统一沙箱、白名单出网和本地 lock plane 均未改变。
- `c62a4a69` 已把原先会干扰模型决策的 base/browser 双执行环境合并为 4 个完全同质的
  `session-sandbox-v1` 槽。每个槽都预装 Bash、Python、Node、Playwright、Selenium 和
  headed Chromium 能力；Harness 只选择空闲槽，不再让模型判断“该去哪个沙箱”。
  四槽自身均为 `network_mode:none`，所有联网都经过同一个带签名 run policy 的
  `skill-egress-proxy`。出网权限是用户本轮 URL、部署白名单和执行器签名授权的交集，
  不是容器级全局放行。
- `c62a4a69` 同时补齐 Backend/Harness 的 session 生命周期事务、fork/delete/install
  fence、孤儿对账、四槽 admission/reap、工作区原子提交与跨服务锁协议。该提交的
  120 文件变更、全量测试和真实容器验收均未加入 V2.3、疾病、文件名或 session 特判。
- 首次生产启动 `c62a4a69` 时，Backend 卡在 NFSv3 `nlmclnt_lock`：
  即使使用 `LOCK_NB`，NFSv3 hard mount 的 lockd RPC 也可能无限等待。`304781c8`
  因此把 Backend/Harness 的 `flock` 协调面移到只由二者共享的本机 Docker local
  named volume；session 内容仍在 NFS，但任何 mutation lock 都不再落到 NFS。
  生产缺少该卷或挂载策略错误时会立即 fail closed，不会退化成两个容器各自的 overlay
  锁文件。
- `304781c8` 已从 clean Git archive 构建并原子部署 Backend/Harness；Frontend、
  四个 session-sandbox、egress proxy 与 legacy CDP browser 使用兼容的
  `c62a4a69` cohort。当前三个 Frontend 入口、Backend/Harness 健康、SQLite、
  SearXNG、四槽 capability 和共享锁实测均通过。
- 2026-07-29 最新功能提交：
  `7116bb1f fix: separate compiled skill authority from obligations`。
- `7116bb1f` 已把 ordinary/static 能力、conditional Knowledge Gate authority 和
  mandatory receipt obligation 分成三个独立、内容寻址且逐层求交的平面；修复
  `3146526e0e284d50b5f70b7412832b8d` 暴露的静态工具被 KG exact 模式误拒、
  delegated provider 固定 120 秒读超时、条件分支共享 bridge 漂移，以及取消/损坏流
  的错误重放问题。Harness 已从该提交的 clean Git archive 构建并部署本机生产。
- 上一轮功能提交：
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
- Harness 启动时对同质 slot pool 做 capability probe，并 reap/abandon/quarantine
  遗留 lease；同一 process 保持 slot affinity，不会在操作中迁移执行环境。
- workspace write/patch/merge/resource copy/executor artifact apply 使用同一外部
  private `flock` 协议；写入原子化，多文件 apply 在 staging 后再次 CAS。生产 lock
  object 位于 Backend/Harness 共享的本机 named volume，不在 NFS workspace sibling。
- 大报告通道上限支持大于 8 MiB 的单产物，并通过约 12.6 MiB PNG 真实验收。

### 4.4 统一 session-wise 沙箱

- 生产在 Harness 同机部署 4 个同质 `session-sandbox-v1` Docker slot。每个 slot 都是
  superset runtime：Bash/Python/Node、Playwright/Selenium/Chromium/Weston 与常用报告
  依赖均预装；Harness 不再维护 base/browser 两种代码执行 lane。
- “session-wise”表示每个 root run/lease 使用独立 snapshot、HOME、TMP、workspace 和
  进程回收边界；不是每个 chat 独占一个新容器。固定容器池只承载 controller，执行
  内容仍按 session/run 隔离。
- 运行环境是固定、预装、不可变 profile；不允许运行时 `apt`/`pip`/`npm` 安装。
  缺少依赖时在 preflight fail-fast。
- Chromium 使用 headed Wayland/Weston；不转发宿主 `DISPLAY`，不开放 CDP TCP。
- 四槽 worker 均为固定 UID/GID 65529；真实并发硬界由每槽 cgroup/pids limit 和
  pool reservation 约束，而不是有限的 host-UID-global `RLIMIT_NPROC`。
- global `/tmp`、`/dev/shm`、`/workspace` 对 worker 不可写；精确执行树位于
  controller-owned private executable tmpfs。
- startup/admission/teardown 做 worker UID sweep 和 shared-state residue audit；setsid/double-fork/refork 真实测试后残留为零。
- SysV/POSIX IPC 由 seccomp 实测 `EPERM`；统一 profile 只保留 Chromium namespace
  所需的最小 syscall/capability。
- 统一槽不设有限 `RLIMIT_AS`，因为 Chromium/V8 使用大规模 sparse VAS；物理内存仍由
  每槽 3 GiB cgroup 硬限制。

### 4.5 Skill 沙箱网络

- 四个 session-sandbox 均使用 `network_mode:none`，worker 只有 loopback、无默认路由、
  无 Docker DNS。
- 独立 `skill-egress-proxy` 是唯一有 `browser_egress` 网络的 Skill 代码执行组件。
  controller 通过只读挂载的 proxy UDS 建立有界 bridge；worker 不持有
  controller/proxy socket authority。
- 公网 HTTP(S) 需要 frozen Skill/run 签名的 exact origin/method/path-prefix 规则；
  默认端口仅 80/443，未授权目标、loopback 和 metadata 均拒绝。
- 私网访问必须同时满足用户当前 turn 的明确 URL、部署 private origin/CIDR allowlist
  和 executor 签名 run policy；DNS 每个答案均重新校验并固定。私有 CA/key 只存在
  proxy 私有卷，executor 只能读取公开 trust member。
- Chromium wrapper 拒绝代理覆盖、resolver 覆盖、公开 remote-debug、stealth/anti-evasion、QUIC 和非代理 WebRTC 路径。
- legacy CDP browser 仍用于 Harness native browser actions，并保留独立 per-turn
  private-origin policy；它不是第二套 Skill Bash/代码执行环境。

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

### 4.12 2026-07-29 `314652...` 编译回归与三平面闭环

本轮同样交叉核对持久化对话、该 session 实际安装的 Skill 包以及 root/child debug：

- route、intent、7 个 bootstrap 和 worker wave 均正确；失败不是网站不可达。
- Safety worker 的普通 `WebFetch → web_extract` 属于无条件执行能力，却因存在
  Knowledge Gate plan 被旧 exact validator 当成条件候选而拒绝。
- PICO child 已获得约 1768 秒动态 lease，但 HTTPX 仍使用固定 120 秒 read timeout，
  因此 provider 在 Harness lease 之前被截断。
- Termination worker 正确选中 PubMed 条件分支后，模型仍能从决策 prompt 看见未激活
  ClinicalTrials 坐标；两者共用 `skill_http_get`，导致 off-prefix 调用被 handler
  fail-closed。
- worker 阶段未完成，所以没有生成 Markdown；不是 artifact verifier 删除了结果。

通用修复：

- 新增签名 `unconditional_capability_plan`：ordinary worker 的 native/MCP/Skill
  resource/script/command/HTTP 能力使用与 KG 相同的 exact candidate compiler，但它们
  只是可用 authority，不被错误升级为“每个候选都必须调用”。
- Loader 将 ordinary `skills/local_resources` 与
  `knowledge_gate_skill_refs/knowledge_gate_local_resources` 分开保存；gate-only
  候选不再泄漏进初始 preload、required Skill 或静态 bridge。若同一 Skill 被两类声明，
  普通能力仍保持 required，不会被旧兼容过滤器误剥离。
- Delegation schema、digest、forced-policy、batch/single task、parent-authority
  intersection、TOCTOU 和 final receipt audit 全部验证 static plan；KG 仍只在 typed
  decision 后激活。
- 决策阶段不再暴露任一分支的 selector/path/URL；接受决策后只发布当前 activated
  frontier。共享 bridge 调错未激活坐标会得到 exact-frontier 纠偏；正确的分页
  `skill_view` 在 EOF 前不会被误报为分支漂移。
- delegated provider 的 HTTPX read timeout 改为无限，由
  `MaterialProgressLease` 统一执行 idle/progress/hard deadline；provider admission
  release 由 runtime-owned single-flight task 完成，调用者取消也不会泄漏配额。
- 已出现 visible/reasoning/tool-call fragment 的流不再透明整轮重放；避免重复草稿或
  重复副作用。

零模型 exact compile 复核实际当前 Skill：

- 8 个 selected workers、53 个 checks、53 个 KG OR groups 全部通过 strict validation
  和 parent-grant revalidation；所有组都有候选。
- Static 编译 0 unresolved。KG 中 `drugbank-database` 有 5 次不可证明 callable route
  的 unresolved occurrence，但所在 OR group 均有其他 exact alternatives，不构成
  blocker，也没有获得说明文字推导出的权限。
- parent closure 覆盖 18/19 个实际引用包；未引用包未获授权。Loader 为 0 errors，
  只有 3 个非阻断 section-mapping warnings。

### 4.13 2026-07-30 统一 session sandbox、白名单出网与本地锁平面

对 `e81543fe65174ec880ec032f711f2d29` 及同类失败的复核结论是：Skill 对 Bash、
Playwright、Selenium 和远端数据库访问的需求应在一个统一 session-wise 环境中满足；
让 Harness/模型在 base 与 browser 两套执行器之间再做一次运行时路由，会增加不必要的
不确定性。当前闭环如下：

- Compose 固定 4 个同质 `session-sandbox-v1` 槽。每个槽都能运行 Bash、Python、
  Node、CJS/MJS Playwright、Python Playwright/Selenium 和持久 process lease。
  Harness 的 slot pool 只做原子 reservation、abandon/restart/reap，不改变 Skill
  authority，也不按 Skill 名或任务领域挑选环境。
- controller 以 root 身份管理 UDS、进程树和资源边界；真正执行 Skill 的 worker 固定为
  UID/GID 65529。每个 root run 有独立 workspace snapshot、HOME、TMP、process group
  和 lease，且只能接收该 session 的内容寻址文件闭包。其他 session workspace、宿主
  文件和更广泛的内部目录不进入快照或挂载。
- 四个槽均为 `network_mode:none`，没有 Docker DNS、默认路由或直接 socket 出网。
  唯一联网组件是独立 `skill-egress-proxy`。执行请求携带 Harness 签名的 public/private
  origin 规则；proxy 再与部署白名单求交并固定解析结果，拒绝 loopback、metadata、
  未授权私网、端口漂移和 DNS rebinding。
- 当前私网部署 CIDR 仅允许 `10.10.132.126/32` 与 `172.30.100.126/32`，具体 origin
  仍必须出现在用户当前 turn 与签名 run policy 中；CIDR 本身不授予访问权。内部 HTTPS
  证书例外是 origin-scoped SPKI pin，不启用全局 TLS bypass。
- 常用运行时和浏览器依赖在不可变镜像内预装；生产不允许运行时 `apt`/`pip`/`npm`
  任意安装。缺依赖由 capability preflight 在 dispatch 前给出 typed gap，避免半途污染
  环境或让模型反复试错。

`c62a4a69` 还把 conversation delete/fork、session Skill install/promote、MCP、
schedule/hook、workspace API 与 Harness artifact apply 统一到 lifecycle fence 和
mutation lock 协议。首次生产启动揭示旧协议把 lock file 放在 NFS session sibling：

- `/nfs/temp/chat_ds` 是 NFSv3 hard mount；内核栈明确停在
  `nfs3_proc_lock → nlmclnt_lock → rpc_wait_bit_killable → flock`。
- `LOCK_EX|LOCK_NB` 只约束本地争锁语义，不能约束 NLM/RPC 网络等待，因此 Backend
  startup reconcile 可以在应用端 timeout 之外挂死。
- `304781c8` 改为固定
  `WORKSPACE_MUTATION_LOCK_ROOT=/run/chatds-workspace-lock-plane/locks`，并要求父目录
  是真实 mountpoint。Compose 的 `workspace_mutation_locks` 使用 `driver: local`、
  `nocopy:true`，RW consumer 严格只有 Backend/Harness。
- 两端以 NFC user/session、长度前缀和版本域派生相同 SHA-256 lock identity；目录
  精确 `0700`、文件精确 `0600`/`nlink=1`/当前 owner，并使用 dirfd、
  `O_NOFOLLOW` 和 inode/path 复核。锁文件永久保留以避免 unlink inode ABA。
- 缺 mount、非 mountpoint、错误权限、symlink、hardlink、owner 或 identity 漂移均
  typed fail closed；本地锁配置未启用时保留 legacy sibling 仅用于源码兼容测试，
  生产 Compose 强制启用本地锁面。

### 4.14 Durable delete intent 与 reconciler 安全闭环

`c62a4a69/304781c8` 首次生产对账暴露了一个独立于 NFS lock 的危险假设：旧
reconciler 把“DB 中没有 conversation row”直接视为删除授权。若服务连接了错误、空或
暂时不可见的数据库，这会把仍存在的 NFS session tree 批量判成 orphan 并删除。
`b4e8dc18` 将删除授权改为持久、可复核的 intent：

- DB absent 且无 tombstone/pending 的 tree 进入 `unfenced_orphan_retained`，只审计、
  不写 tombstone、不删除；pending 存在时进入
  `unresolved_pending_retained`，有界检查 journal 后仍只保留或 defer。
- 只有 DB absent 与既有 validated deletion tombstone 同时成立才允许 cleanup；
  一个已验证的删除 tombstone 优先于遗留 pending。兼容字段 `fenced` 保留但不再表达
  删除授权，当前始终为 0。
- v2 marker payload 必须逐字匹配
  `chatds-session-deletion-v2\nboot_id=[a-f0-9]{32}\n`，并在 256 bytes 内读到 EOF。
  marker parent 必须为当前 euid 拥有的精确 `0700` 目录；marker 必须为当前 euid
  拥有的精确 `0600` 普通文件、`nlink=1`，并通过 no-follow、inode/path 稳定性复核。
  symlink、hardlink、错误 owner/mode、截断、legacy、trailing 或超长 payload 全部
  fail closed。
- destructive boundary 在删除 Skill/workspace 前再次验证 tombstone，覆盖 marker
  在初检后被移除、替换或损坏的 TOCTOU。pending marker 在 session tree 删除前先
  清理；若该步失败或进程崩溃，tree 仍可在重启后重新发现并最终收敛。
- pending inspection 只捕获预期 I/O/value/workspace/HTTP 异常；编程错误会停止当前
  batch 并传播，且在异常前后都不发生删除。异常 cohort 只输出有界聚合计数和最多
  16 个 SHA-256 user/session 样本，不写原始 ID、异常正文或凭据。
- Backend pytest 通过 autouse 临时 root 隔离 workspace/upload/Skill/lock/reconciler
  状态，避免测试触碰生产 `/nfs/temp/chat_ds`。代表性 strace 用例对此路径为 0 次
  syscall；跨进程 flock、deferred-next-tick、重启收敛、真实 SQLite 快照、marker
  元数据/payload 矩阵和 TOCTOU 均有回归。

### 4.15 Retrieval、deadline、intent 与调用级 effect receipt

`b81829a6d09447989851cbb208bcdbed` 及相邻诊断暴露的不是单一“容器不能联网”，而是
多个通用控制面问题叠加。本轮按 exact Skill、持久化对话和 debug/tool/AgentRun
三方交叉检查后完成以下闭环：

- retrieval completeness 改为 policy-aware、有界且公平的调度器。未完成的 mandatory
  source chain 优先于 clean optional cursor；同优先级按最近推进时间稳定轮转。
  mandatory-only 模式不再退回 optional source，进入 closing 后也不会被 terminal/output
  repair 重新打开联网工具。
- provider 的 planned duration 只用于观测 checkpoint，不再被误当成绝对超时。
  initial/progress lease 均只受调用方配置的 hard cap 约束，并记录
  `planned_budget_crossed`；长思考不会仅因预测偏小而提前终止。
- public-CA 与 SPKI TLS lane 在保持证书校验、SNI、TLS >= 1.2 和 HTTP/1.1 的同时，
  补齐 TLS 1.3 post-handshake-auth client capability，修复部分上游在 TLS 1.3 下
  返回 403/断链而宿主 `curl` 正常的差异。
- runtime compiler 的 `chatds.intent-classifier.v1` typed result 现在精确区分
  required、optional、nullable、default 和 `on_missing`：required WARN/FAIL、
  non-null WARN 均 fail closed；optional/nullable 无默认缺失可 `WARN:null`；
  defaulted 缺失必须以 `PASS` 返回 effective default。legacy 非结构化直接委派保持兼容。
- `run_skill_python`、`run_skill_script` 和 `run_declared_command` 由 handler 生成并绑定
  tool name/call ID 的 invocation-level effect receipt，记录 terminal/teardown、
  artifact mutation 数、实际 HTTP method、rule/operation/binding hash。只有执行器已
  返回、产物精确为零且实际方法全为 GET/HEAD 的调用才可标记 replay-safe；POST、产物、
  缺失/损坏/canceled/wrong-call receipt 均不可自动重放。父层 retry 使用实际 unsafe
  invocation 数，不再只看工具的静态 mutating 标签。
  `100f42ba` 随后发现调用级 Bridge receipt 不能证明 Proxy 跨调用聚合预算，因此已
  进一步安全收紧：只要存在 controlled egress，当前一律 effect unknown 且不可自动
  replay；无网络调用仍保留原有精确证明。
- run start/final/result/debug 都携带 secret-free、内容寻址的初始 child static
  authority snapshot，包括工具与 plan SHA，以及资源、脚本、命令、URL/egress 的
  安全摘要，便于之后从 debug 还原当时真正授予的执行权限。
- GET/HEAD 带 request body 现已在 egress proxy 拒绝。当前网络仍是无直连
  session-sandbox + 签名 exact method/origin/path policy + 独立代理；这显著降低上传
  面，但不能声称“绝对零外传”：任何检索都必须向外发送目标域名、路径/查询词、DNS/TLS
  元数据，且少数 Skill 可显式声明 POST/read-query API。若威胁模型要求严格数据防泄漏，
  仍需独立 read-query lane、查询/header schema、敏感信息检测与速率/字节配额，不能
  只用 HTTP method 名称证明单向数据流。

### 4.16 Policy v3、根任务预算与沙箱清理闭环

本轮没有增加第二套 Bash/浏览器沙箱，也没有让模型选择运行环境。四个同质
`session-sandbox-v1` 槽继续保持 `network_mode:none`；所有 HTTP(S) 仍只能经过
`skill-egress-proxy`。新增的是通用控制面闭环：

- Harness 从 runtime-owned `user/session/root_run/tool_operation` 派生不透明 SHA-256
  scope/call identity。原始身份不跨 Executor 边界；同一 root run 的 child、retry、
  one-shot 和 persistent process 都使用同一预算 scope。
- policy v3 的 scope、call、exact rules、private origins、trust generation 和三项
  limits 全部进入 HMAC。生产 Executor 与 Proxy 都强制 v3；无出网规则的 deny-all
  兼容请求仍可保留 v2，不能借此获得网络 authority。
- Proxy 的线程安全 scope ledger 在每个实际 HTTP 请求进入 DNS/上游连接前累计请求数
  和 outbound bytes，在响应转发前累计 response bytes。默认上限为 2048 requests、
  16 MiB outbound、512 MiB response；scope 容量 65,536，inactive TTL 24 小时。
  inactive scope 使用独立 LRU，长期 active scope 不会阻塞其后过期项回收；容量满时
  不驱逐未过期账本，而是 fail closed。
- 请求仍需 exact method/origin/path-prefix 匹配。GET/HEAD body、chunked request、
  parser 歧义、过大 target/query/header/body、forwarding identity header、未授权
  私网/metadata/loopback 和 DNS rebinding 都在上游连接前拒绝。
- Bridge 在封印前停止接受连接、有界等待 handler、关闭残留 socket，并生成不可变的
  invocation-local receipt。它能证明本地调用已收尾，但不能证明 Proxy 跨调用累计
  账本，因此模型侧只保留安全 counters/effect projection，原始 scope/call receipt
  会在工具结果返回前移除。
- persistent lease 的 open/expiry/close/ACK、janitor、shutdown 和 controller reap
  都按 lease 隔离 seal failure。one-shot 连续 seal 失败会把 Bridge 转移到
  controller-owned orphan registry；registry 非空时 runtime capability/health
  fail closed，统一 admission 保持 quarantined，后续 reap seal 成功后才解封。
- process sync ACK 必须精确匹配 pending operation：close ACK 只能是 `closed`，
  live sync ACK 只能是 `open/running/exited`。expired/closed/quarantined bound error
  携带严格 terminal state，Harness 对结构、authority、scope/call 和 audit digest
  任一漂移都会 quarantine 对应物理槽。

网络安全结论不能写成“只控制方向就不需要白名单”。TCP/TLS/HTTP 检索本身必须向外发送
DNS、握手、域名、path/query/header；状态防火墙无法区分合法查询与把数据编码进 query
的上传。当前实现是“无直连 + exact 目标/方法 + metadata/body 约束 + 根任务字节/次数
预算”的有界受控交换。若要求严格零外传，只能提供固定模板 broker 或 deployment-owned
query/header schema/DLP，不再允许任意浏览器/API 请求。

### 4.17 通用 Skill workflow 契约与成熟 Harness 取舍

- `delegate_task` 的公共 Tool Schema 现在显式接受
  `sandbox_egress_url_prefixes`、`sandbox_egress_rules` 和
  `browser_egress_rules`；rule 严格限定为 `methods + url_prefix`，关闭了“编译器生成
  合法 exact candidate、内部 validator 接受、provider 可见 schema 却拒绝”的
  三方漂移。direct/static/Knowledge Gate、single/batch 都走同一契约。
- 所有 Tool preflight 拒绝由 `ToolPreflightResult` 规范化为 typed reason、
  `dispatch_state=not_dispatched` 和 `actual_dispatch_attempted=false`。自动委派只有在
  真正进入 dispatcher 后才写 delegate receipt，不再把 policy/schema 拒绝伪装为
  malformed child result。
- multi-agent 激活不依赖疾病、文件名、固定 worker 数或 V2.3 词面：编译器有界识别
  角色表/角色定义、多轮、并行独立工作与 coordinator/fan-in 结构；它只决定是否进入
  声明式 workflow，实际节点、依赖、authority、receipt 和完成条件仍以签名
  Workflow IR 为唯一权威。
- delegate 在 provider/schema/pre-spawn 阶段失败时，也会生成稳定
  `child_run_id`、Skill 中的语义化 `agent_name`、零用量 `agent.spawned` 和权威
  `run.failed`。刷新页面后可从 durable event 重建，不再只看到易失的
  `delegate-1/2/3` 或丢失失败原因；同时不会捏造不存在的 model/tool dispatch。
- Python Skill 对 `../data/*.db` 等 package-relative 数据依赖使用静态 AST 有界推导
  `required_cwd`。只有单一、不可变且仍位于 Skill package 内的候选会自动选择
  script/skill cwd；动态或歧义路径继续 fail closed。
- Backend 对实时 `run.failed` 与启动时 reconciliation 使用同一终态投影，并保存
  `finish_reason`。`workspace_context` 对路径安全模块改为惰性导入，消除了独立测试
  顺序触发的循环依赖。
- 架构决策是继续演进当前 Harness，参考 Deep Agents 的上下文卸载、subagent 命名和
  middleware 分层，不引入其第二套 agent loop。未来若采用其组件，应先以 adapter
  接入并通过现有 authority/receipt/terminal contract，不能绕开当前安全与持久化层。

### 4.18 2026-07-31 跨层回执、检索隔离与同源故障熔断

- Knowledge Gate 决策的语义权威从 debug/compacted argument projection 移到
  handler-owned typed receipt。回执只保存 plan digest、check/outcome 和运行时重算
  frontier，不保存模型 reason；outer delegate 必须重新验证 schema、digest 和
  frontier，不能从 `tool.started` 或空 `audit_args` 猜测语义。
- 编译期已完整预加载且 digest 完全一致的 `skill_view` 资源，会生成绑定
  child run 与 aggregate preload digest 的 body-free control-plane receipt。只有
  typed decision 激活后、Skill 名称/相对路径/资源 SHA-256 与 exact candidate 全部
  相等时才计入 gate；`read_file`、HTTP、MCP、脚本、不完整分页和错误摘要都不能借此
  提升 authority 或满足 receipt obligation。
- Retrieval tracker 将 page/cursor/truncation 失败限定在各自 family；一个来源达到
  chain-local 上限不会把其他仍可推进的独立来源一并终止。请求数、累计响应字节和总
  耗时仍是 run-global 硬预算；snapshot 明确区分 global terminal、terminal chain
  和 runnable chain。
- `COMPLETION_QUALITY_JSON` 的整个 ledger 继续受 4096-byte 严格上限和 exact JSON
  schema 约束，但较长或含转义换行的 substantive reason 不再把已经完成的大正文误判
  为失败。审计只持久化 reason 的 SHA-256、字符/字节数和 shape，不复制正文。
- 子任务失败现在带稳定、secret-free 的 `failure_origin`、
  `failure_fingerprint` 和 taxonomy version。只有同一个 Harness/validator
  fingerprint 在至少两个独立声明步骤重复时才停止后续 wave；provider、模型、网络/
  上游失败不会触发 common-mode breaker，已成功步骤和 artifacts 保持不变。
- 新增 tests-only `ScriptedProvider`，在真实 OpenAI-compatible HTTP/SSE 边界按顺序
  驱动请求、工具批次和中断，并可断言实际 request body；它不参与生产执行，也没有
  第二套 agent loop。现有仓储、卫星、博物馆等非临床 holdout Skill 继续验证通用编译
  与 artifact/workflow 契约，V2.3 仍只是一项用户手工 E2E 用例。

## 5. 当前验证证据

2026-07-31 功能提交 `17e261ef` 已通过：

- 受影响面组合回归：
  `246 passed, 95 subtests passed`。
- 非 root 宿主全量：
  `1801 passed, 1 skipped, 19 failed, 751 subtests passed`；19 项全部在测试断言前
  因当前用户无权读取生产 NFS tombstone 而 fail closed，与既有环境噪声同型。
- 只读源码挂载的隔离 root 容器全量：
  `1812 passed, 1 deselected, 760 subtests passed`；唯一 deselect 是生产 Harness
  镜像按设计不带 Node 的 CommonJS round-trip，该项已在宿主 Node 运行时单独
  `1 passed`。warnings 只有既有 multiprocessing/fork deprecation。
- `compileall`/`py_compile`、`git diff --check` 和生产逻辑 genericity scan 通过；
  没有执行模型重型 V2.3 E2E。

2026-07-30 当前生产 cohort 已通过：

- `82c818fc` 最终验证：
  - 首轮 Harness 聚焦为 `244 passed, 115 subtests passed`；独立 import-order
    回归为 `129 passed, 40 subtests passed`；最终补丁后的聚焦组合为
    `127 passed, 46 subtests passed`；
  - Backend 全量为 `223 passed`，仅有既有 deprecation warnings；
  - 宿主 Harness 全量为 `1787 passed, 1 skipped, 19 failed, 747 subtests passed`，
    19 项全部是当前非 root 用户无权检查生产 NFS tombstone 路径；
    在 clean archive 隔离 root 容器中消除该环境噪声后为
    `1792 passed, 5 skipped, 1 deselected, 752 subtests passed`。被 deselect 的
    CommonJS/Node 用例使用宿主 Node 22.23.1 单独运行通过；5 个 skip 是隔离镜像未挂
    reference/runtime assets；
  - 使用真实但只读的 `lung-cancer-mdt` session Skill 做零模型交叉验证，
    `declared_delegated_workflow=true`、
    `clinical_trial_required_cwd=script`、`runtime_profile=base-v1`；
    没有把该 Skill、领域或路径写入生产逻辑；
  - `compileall`、`git diff --check`、显式 staged file/secret/genericity scan 和
    clean-image compile smoke 通过。没有执行模型重型 V2.3 E2E。
- `100f42ba + f1e59c20` 最终验证：
  - Executor/Proxy/Bridge 全组合：
    `210 passed, 1 skipped, 254 subtests passed`；
  - Harness changed-path 聚焦：
    `40 passed, 57 subtests passed`；独立 release audit 另验证
    `80 passed, 81 subtests passed`，无 P0/P1 blocker；
  - 非 root Harness 全量为 `1779 passed, 1 skipped, 725 subtests passed`，19 项均由
    测试进程无权读取生产 NFS tombstone 触发；隔离 root 容器消除该噪声后为
    `1789 passed, 734 subtests passed`，唯一未跑项是 Harness 镜像按设计不含 Node。
    该 CommonJS 用例在宿主完整 runtime 通过，并由下述统一沙箱真实验收覆盖；
  - clean-archive 候选真实启动独立 Proxy/Executor，完整通过 Node CJS/MJS
    Playwright、Python Playwright/Selenium、persistent class/factory、IPC deny、
    UID/capability/route/UDS 隔离、public v3 egress、loopback/private/metadata deny、
    descendant cleanup 和 12,589,062-byte artifact；
  - `py_compile`、Compose effective config、cached diff/secret/scope/genericity scan
    通过；生产代码没有 V2.3、疾病、session ID、文件名或 route 特判。
- `2486f008` 最终验证：
  - Harness 全量：`1775 passed, 1 skipped, 3 warnings, 717 subtests passed`；
    warnings 仅为既有 multiprocessing/fork deprecation；
  - Executor 全量：`108 tests OK, 1 skipped`；Egress proxy 全量：`60 tests OK`；
  - intent/workflow 聚焦：`46 passed, 9 subtests passed`；关键综合回归：
    `103 passed, 30 subtests passed`；
  - `py_compile`、`git diff --check`、staged secret/genericity scan、clean-image
    `compileall`/import、proxy source compile 和临时 live health 均通过；
  - 独立 release audit 最终无 P0/P1 blocker。
- `b4e8dc18` 最终验证：
  - Backend 全量：`223 passed, 101 warnings`；warnings 仅为既有 `crypt` 与
    `datetime.utcnow()` deprecation；
  - durable-reconciler 聚焦组合：`97 passed`；
  - 独立 release reviewer：`72 passed`，P0/P1 均为 0；
  - 默认与 local-search Compose、`compileall`/import、clean-image 启动、staged
    secret/genericity scan 和 `git diff --check` 均通过。
- 候选容器在隔离 DB/workspace/Skill/lock volumes 中验证：unfenced 和 pending tree
  保留、有效 tombstone 清理、无效 marker defer；deferred 第二 tick 不重新扫描磁盘且
  仍不删除。真实 Uvicorn health/revision、数据库 `quick_check` 和 clean startup log
  通过。
- 生产现有 71 个 marker parent 与 94 个 marker file 均通过 owner/mode/type/nlink
  检查，94/94 payload 匹配严格 v2 格式；部署前后数据库核心计数不变。
- `304781c8` 最终全量：
  - Backend：`196 passed`；
  - Harness：`1760 passed, 1 skipped, 704 subtests passed`；
  - executor topology/profile 聚焦：`35 passed, 25 subtests passed`。
- `c62a4a69` 冻结提交此前全量：
  - Backend：`193 passed`；
  - Harness：`1755 passed, 1 skipped, 701 subtests passed`；
  - Executor：`106 passed, 1 skipped, 115 subtests passed`；
  - Egress proxy：58 项通过；
  - Frontend：19 项与 production build 通过。
- clean Git archive 真实候选验收覆盖 4 个同质槽、Bash/Python/Node、CJS/MJS
  Playwright、Python Playwright/Selenium、persistent process、IPC/escape cleanup、
  约 12.6 MiB artifact、public/private exact egress、直连/loopback/private/metadata
  deny，以及 Harness 高层 adapter 的 4 槽并发 abandon/restart/reap。
- `304781c8` 两镜像在临时 local named volume 上计算同一路径和同 inode；双向持锁时
  对端均约 401 ms 返回 `workspace_lock_timeout`，holder 被强杀后 283 ms 内恢复。
  未挂锁卷且 `REQUIRE_MOUNTPOINT=1` 时两端立即 `workspace_lock_unsafe`。
- 生产同一 lock volume 再次验证双向争锁约 402–403 ms、目录 `0700`、文件
  `0600`/`nlink=1`、Backend/Harness 同 dev/inode、底层 ext 文件系统，且没有创建
  legacy NFS sibling lock。Backend startup 约 16 秒完成，不再进入 NLM wait。
- 四个生产 slot 的 capability probe 均为同一 build，Bash/Python/Python3/Node、
  Playwright 1.61.0、Selenium 4.46.0 可用，direct network 为 disabled。
- staged scope、secret/genericity scan、默认与 local-search Compose effective config、
  `py_compile`、`git diff --check` 和独立 release audit 均通过；无 V2.3、疾病、报告
  文件名或 session-specific 生产逻辑。
- 本轮没有执行模型重型 V2.3 E2E；下一项仍由用户手工发起。

2026-07-29 当前功能提交 `7116bb1f` 已通过：

- Harness 全量（`cd harness && PYTHONPATH=.. python -m pytest -q`）：
  `1602 passed, 1 skipped, 575 subtests passed`，0 failures/errors。
- Knowledge Gate/runtime/AgentLoop/authority 聚焦组合最终为
  `142 passed, 105 subtests passed`；独立 release audit 另跑
  `331 passed, 151 subtests passed`。
- `py_compile`、`git diff --check`、staged secret scan 和生产代码 genericity scan
  通过；新增生产逻辑没有疾病、报告文件名、session ID、固定 worker 数或 V2.3 特判。
- 复杂测试 Skill 的零模型 exact compile 通过 8 workers / 53 checks / 53 groups；
  static/KG 分权、ordinary/KG 重叠、parent exact authority 均通过。
- clean Git archive 镜像通过离线 `compileall`、`import main`、revision label 检查；
  部署后 Harness health/model、三入口、active-run 和日志检查通过。
- 本轮未执行模型重型 V2.3 E2E，下一项仍是用户手工业务验收。

2026-07-29 上一功能提交 `7bbc0809` 已通过：

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
- 2026-07-31 完成 `17e261ef` Harness 收敛更新：
  - 部署前连续两次确认 active AgentRun、active root run、running/enabled schedule
    与 5173 established connection 均为 0；
  - 候选来自 clean Git archive
    `/tmp/chat_ds_deploy_17e261ef.YWE8va`，archive 文件数与 tracked tree 精确一致，
    revision label 为完整 Git SHA；
  - 候选先通过离线 compile/import，再复用真实四槽与 Browser UDS 做隔离
    `/health`、`/v1/models` smoke；随后仅 force-recreate Harness。Backend、
    Frontend、四槽、Proxy、Browser、SearXNG/Valkey 和数据库均未替换；
  - 部署后 Harness image 为
    `sha256:9da0762b742e50d55d8d064b5acb51f49e3043cc65945bff3df9519b0e273139`，
    revision 为完整提交
    `17e261ef61b913e804e9875a8010480edfb5081a`，healthy/restart 0；
    Backend→Harness `/v1/models`、Harness `/health` 与三个 Frontend
    `/api/health` 均为 200，启动严重错误匹配为 0，active/root/scheduled run
    仍为 0。
- 2026-07-30 完成 `82c818fc` 通用 workflow 契约更新：
  - 部署前两次确认 active AgentRun、active root run、running schedule 与 5173
    established connection 都为 0；
  - Harness 与 Backend 候选均来自 clean Git archive
    `/tmp/chat_ds_deploy_82c818fc.xsEyE5`，archive 文件数与 tracked tree 精确一致，
    revision label 为完整 Git SHA；
  - 按 Harness → Backend 顺序逐个 force-recreate，Harness 健康后才切 Backend；
    Frontend、四槽、proxy、browser、SearXNG/Valkey 和数据库卷保持不变；
  - 部署后 `127.0.0.1`、`10.10.132.126`、`172.30.100.126` 的
    `/api/health` 都为 200；Harness `/health` 为 200；两容器 restart 0，
    active/root/scheduled run 都为 0，启动日志严重错误匹配为 0。
- 2026-07-30 完成 `c62a4a69 + 304781c8` 生产切换：
  - `c62a4a69` 的首次 Backend 启动因 NFSv3 lockd RPC 卡住，Frontend 始终保持关闭；
    取证后停止 Backend，没有向用户暴露半启动服务。
  - 切换前数据库备份卷为
    `chat_ds_db_backup_pre_c62a4a69_20260730_023437`；备份和源库
    `quick_check=ok`。
  - `304781c8` 候选通过临时跨容器锁验收后，旧 Backend/Harness 被同时移除，
    两者以同一 lock protocol cohort 原子替换。新卷
    `chat_ds_workspace_mutation_locks` 为 `driver: local`、无 driver options，consumer
    严格只有 Backend/Harness。
  - Backend startup reconcile 约 16 秒完成，`/api/health` 200，未再出现
    `nlmclnt_lock`/NFS `flock` 等待；Frontend 最后启动。
  - 当前生产私网 CIDR allowlist 含 `10.10.132.126/32` 与
    `172.30.100.126/32`；具体 URL 仍需本轮用户授权和签名 run policy。
- 2026-07-30 随后完成 `b4e8dc18` Backend 安全更新：
  - clean archive 候选标记为 `chat_ds-backend:deploy-b4e8dc18`，仅 force-recreate
    Backend；Harness、Frontend、四槽、proxy、browser 和搜索服务均保持兼容 cohort；
  - Backend `/api/health` 200、restart 0、startup log 无异常；SQLite
    `quick_check=ok`、foreign-key violation 为 0，核心计数与切换前一致；
  - 切换前使用 SQLite online backup API 创建卷
    `chat_ds_db_backup_pre_b4e8dc18_20260730_041439`，其 SHA-256 为
    `d0055d10b4f6239cceb888efffff3008036e2054d1ba5925df6707d471addb07`，
    且 `quick_check=ok`、foreign-key violation 为 0；
  - 生产 Backend 保持单 Uvicorn process、无 active-active/overlapping rollout，
    满足当前 reconciler 的部署不变量。
- 2026-07-30 随后完成 `2486f008` Harness/egress proxy 收敛更新：
  - 部署前确认 active AgentRun、scheduled running 和 5173 established connection
    均为 0；短暂停止 Frontend 后再次确认，再按 proxy → Harness → Frontend 顺序切换；
  - 两个候选都来自 clean archive `/tmp/chat_ds_build_2486f008.U6nu4S`，revision
    label 为完整 Git SHA；Backend、数据库、四槽、browser 和搜索服务均未替换；
  - 部署后 Harness/proxy healthy、restart 0，Harness `/health` 与 `/v1/models`
    为 200，SQLite `quick_check=ok`、foreign-key violation 为 0，active/scheduled
    run 为 0。
- 2026-07-30 随后完成 `100f42ba + f1e59c20` policy-v3 生产切换：
  - 部署前 active AgentRun、running/enable schedule 与 5173 established connection
    均为 0；SQLite `quick_check=ok`、foreign-key violation 为 0。先停止 Frontend 和
    旧 Harness，再按 Proxy → 四槽 → Harness → Frontend 顺序切换；
  - 三个候选都来自 clean Git archive
    `/tmp/chat_ds_build_f1e59c20.bNq8hp`，revision label 为完整 Git SHA。切换前镜像
    分别保留 `rollback-pre-f1e59c20`；候选、部署和 `latest` tag 均指向已验收镜像；
  - 仅替换 Proxy、四个 session-sandbox 和 Harness。Backend、数据库、Frontend、
    Browser、SearXNG/Valkey 均未替换；
  - 部署后三个 Frontend 入口的 `/` 与 `/api/health` 全部 200；Harness 容器内以及
    Backend→Harness 的 `/health`、`/v1/models` 全部 200。六个新容器 healthy、
    restart 0，active/scheduled run 仍为 0，相关日志严重错误匹配均为 0。
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
| `chat_acits_executor` ～ `_4` | `sha256:08996fb6e1da586de9ee57d1812dda75826145bdf07d86dfa784f24b35ec004a` | 4 个同质槽 / healthy / restart 0 / revision `f1e59c20` |
| `chat_acits_skill_egress_proxy` | `sha256:6f23e97983ace0c4855af3dbf65967678902d2cd8d5c5b33e92eeecb2cec072f` | healthy / restart 0 / revision `f1e59c20` |
| `chat_acits_browser` | `sha256:08bcf8860c10ba8fcd647b6d1a96c2c12e13e46db800c812acea82e17007240c` | healthy / restart 0 / revision `7bbc0809` |
| `chat_acits_harness` | `sha256:9da0762b742e50d55d8d064b5acb51f49e3043cc65945bff3df9519b0e273139` | healthy / restart 0 / revision `17e261ef` |
| `chat_acits_backend` | `sha256:28cc61b9c80eebccbdcb9b0ad4421272ffc63cd3e3a4de320ae7d09934ce2176` | running / restart 0 / revision `82c818fc` / `/api/health` 200 |
| `chat_acits_frontend` | `sha256:907c5abb41a5288c852ae55d2bbc3258196e4fc03fe0305ce072366f9255cb24` | running / restart 0 / revision `c62a4a69` / `/` 200 |

生产 smoke 证据：

- `127.0.0.1`、`10.10.132.126`、`172.30.100.126` 的 Frontend `/` 和
  `/api/health` 均为 200。
- 四个 session-sandbox、browser、skill egress proxy 健康；Harness `/health` 与
  Backend `/api/health` 为 200。四槽 capability probe 的 runtime build 完全一致。
- 生产 SQLite `quick_check=ok`、foreign-key violation 为 0；当前计数为
  conversations/messages/runs/events/tasks =
  `199 / 769 / 403 / 58760 / 358`，nonterminal agent run 为 0，enabled schedule 为 0。
- `task_items` 中有 18 条历史 `running` 投影，但其对应 root AgentRun 均已终态
  （10 succeeded、8 failed），不是当前活跃执行；判断运行态应以 durable AgentRun
  和 terminal event 为准。
- SearXNG 真实 `OpenAI GPT` 查询返回 27 条结果，命中 `360search`、`bing`、`mojeek`；
  SearXNG/Valkey 均 healthy。免费上游仍可能动态出现 unresponsive engine，不属于
  Harness 执行环境缺失。
- Harness revision label 为完整提交
  `17e261ef61b913e804e9875a8010480edfb5081a`；Backend 为
  `82c818fc6d7eb135e63d74f3b176c4b56bf4947e`；proxy/四槽为
  `f1e59c20129d9c3ba91b0f80850983e93d24d9dc`；Frontend 为
  `c62a4a69cfbbfb46404cfa1eb51b5f8e0498dce2`；legacy Browser 保持兼容基线。
  所有长期容器 restart 均为 0。
- executor/proxy/browser/Harness/Backend/Frontend 日志未发现 traceback、
  critical、fatal、unhandled、ProtocolError 或 exception。
- 本轮没有运行模型重型 V2.3 E2E；下一项仍是用户手工业务验收。

回滚点：

- `17e261ef` 切换前 Harness 保留
  `chat_ds-harness:rollback-pre-17e261ef`；候选/部署 tag 为
  `candidate-17e261ef` / `deploy-17e261ef`，clean archive build 目录为
  `/tmp/chat_ds_deploy_17e261ef.YWE8va`。
- `82c818fc` 切换前 Harness/Backend 分别保留
  `chat_ds-harness:rollback-pre-82c818fc` 和
  `chat_ds-backend:rollback-pre-82c818fc`；候选/部署 tag 为
  `candidate-82c818fc` / `deploy-82c818fc`。clean archive build 目录为
  `/tmp/chat_ds_deploy_82c818fc.xsEyE5`。
- `f1e59c20` 切换前四槽、Proxy、Harness 分别保留
  `chat_ds-session-sandbox:rollback-pre-f1e59c20`、
  `chat_ds-skill-egress-proxy:rollback-pre-f1e59c20` 和
  `chat_ds-harness:rollback-pre-f1e59c20`；当前候选/部署 tag 为
  `candidate-f1e59c20` / `deploy-f1e59c20`。clean archive build 目录为
  `/tmp/chat_ds_build_f1e59c20.bNq8hp`。
- `2486f008` 切换前 Harness/proxy 分别保留
  `chat_ds-harness:rollback-pre-2486f008` 与
  `chat_ds-skill-egress-proxy:rollback-pre-2486f008`；当前候选/部署 tag 分别为
  `candidate-2486f008`、`deploy-2486f008` 和 `latest`。clean archive 为
  `/tmp/chat_ds_build_2486f008.U6nu4S`。
- `c62a4a69` 全套切换前的 7 个应用镜像均保留
  `rollback-pre-c62a4a69` tag；当前候选 tag 为
  `chat_ds-session-sandbox:deploy-c62a4a69`、
  `chat_ds-skill-egress-proxy:deploy-c62a4a69`、
  `chat_ds-frontend:deploy-c62a4a69` 等。其 clean Git archive 构建目录为
  `/tmp/chat_ds_build_c62a4a69.UYIHcV`。
- NFS lock incident 修复前的 c62 Backend/Harness 另保留
  `chat_ds-backend:rollback-pre-304781c8` 与
  `chat_ds-harness:rollback-pre-304781c8`；当前候选分别为
  `chat_ds-backend:deploy-304781c8`、`chat_ds-harness:deploy-304781c8`，
  clean Git archive 为 `/tmp/chat_ds_build_304781c8.7jUf4L`。
- `b4e8dc18` 切换前 Backend 镜像保留为
  `chat_ds-backend:rollback-pre-b4e8dc18`，image ID 为
  `sha256:c178b155ad2ffe55b8ebda9903a45034a2378734dffb9917e4847ec2a31c17e6`；
  当前候选为 `chat_ds-backend:deploy-b4e8dc18`，clean archive build 目录为
  `/tmp/chat_ds_build_b4e8dc18.od5RE2`。
- 当前最近数据库回滚卷为
  `chat_ds_db_backup_pre_b4e8dc18_20260730_041439`；更早的完整切换前卷
  `chat_ds_db_backup_pre_c62a4a69_20260730_023437` 仍保留。不得使用
  `docker compose down -v`。
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
- `7116bb1f` 仅重建 Harness；候选镜像为
  `chat_ds-harness:deploy-7116bb1f`，切换前镜像保留为
  `chat_ds-harness:rollback-pre-7116bb1f-local`。第一次入口探测遇到 Frontend
  刚启动时的瞬时 connection reset，自动回滚成功；加入 bounded readiness retry 后
  第二次切换和三入口复核全部通过。
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

`data/skills/**` 中仍有初始 baseline 意外跟踪的 97 个 runtime 文件，分布在 14 个
历史 session。2026-07-30 旧 startup reconciler 仅凭 DB-absent 错误清理了
`9763f320...` 的 runtime tree，并因此让其中 71 个 tracked fixture 暂时显示为删除；
这正是 `b4e8dc18` 引入 durable delete intent 的直接原因。71 个文件已从当前 HEAD tree
`4105c37f...` 精确恢复到 Git checkout，仅恢复版本库 fixture，未伪造 NFS workspace、
DB row 或 session authority。
不要把未来同类 runtime cleanup 直接 stage；长期应把 runtime `data/` 与 Git
checkout 分离或完成一次审计后的 untrack/migration。

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
- Skill sandbox 的公网与显式白名单私网 HTTP(S) 都必须经过签名 egress policy；
  不支持 CAPTCHA、stealth、反爬绕过或未确认的重要操作。
- “只允许下载、禁止上传”能显著缩小风险，但 HTTP retrieval 本身仍会发送
  域名、路径、查询词和协议元数据；GET 也可把数据编码进 query/header。当前已拒绝
  GET/HEAD body 并精确限制 method/origin/path，严格 DLP 仍需查询/header schema、
  内容检查和出站字节/速率预算，不能把 GET/HEAD 等同于数学意义上的单向通道。
- policy-v3 root-run scope ledger 当前只存在于单个 Proxy 进程内并保留最多 24 小时；
  Proxy 重启会重置累计值。65,536 个未过期 scope 满载时会全局 fail closed，而不会
  LRU 驱逐并重置安全预算。若该累计值未来要成为跨重启安全证明，应迁移到持久 ledger。
- Proxy 尚未提供结构化 aggregate terminal attestation；Bridge receipt 只证明一次
  invocation 的本地连接/字节/封印状态。因此 controlled-egress 工具当前一律
  effect unknown/non-replay，这会少自动重试一次，但不会因证据不足重复外部副作用。
- stdio MCP 已降权和隔离 ambient secret，但不是完整 mount/network namespace；仍只注册可信配置。
- 免费搜索引擎健康度、CAPTCHA、协议变化和上游站点 4xx/5xx 是动态外部条件，不能误归因为 Harness 回归。
- Workflow IR 当前能机器证明结构、source digest、required-node 与结果路径覆盖，但结构覆盖不等于业务语义质量证明；长期可增加逐 instruction evidence ledger。
- 预加载给 controller-only child 的 instruction source 仍可能同时存在于只读 resource grant；authority 已精确且无安全越权，但后续可进一步禁止冗余读取。
- 数据库唯一索引允许不同 terminal event type 使用同一 seq；当前 projection 以首个 authoritative terminal 为准且不会翻转，未来可增加更强的跨 event-type 存储层终态约束。
- `workspace_mutation_locks` 是单 Docker 主机协调面，不支持多主机 active-active。
  NFS lockd 挂死已消除，但 NFS hard mount 的普通 stat/read/write 在存储故障时仍可能
  进入 D-state，需要独立的存储可用性治理。
- 本地 mutation lock 文件为避免 inode ABA 而永久保留；若未来需要回收，只能在
  Backend/Harness 全停且确认无 holder 的离线窗口执行。
- reconciler 的 `fenced` 是兼容字段，当前恒为 0；应使用
  `unresolved_pending_retained`、`unfenced_orphans_retained`、
  `tombstoned_orphans` 和 `deletion_fence_unresolved` 判断状态。
- stable anomaly 每个周期仍会产生有界检查 I/O 和一条聚合 warning；未来可在不削弱
  重试语义的前提下增加内容寻址 cache/backoff。
- tombstone 是 durable delete intent，不是数据库 commit ledger，也没有跨重建
  generation。当前生产依赖单 Backend process 且禁止 overlapping active-active rollout；
  多实例部署前需增加跨进程 lifecycle ledger。
- pending inspection 遇到非预期编程错误会安全地中止该 batch，而不是跳过错误继续处理
  后续候选；这可能降低一次对账吞吐，但不会扩大删除面。
- tombstone 自身的目录/文件边界已严格校验；更高层 NFS root/user 目录仍有历史
  `0777/0755/0775` 权限且不都由当前 euid 拥有。沙箱不挂载 marker plane，现阶段信任
  同主机/NFS writer；若威胁模型包含恶意同 UID/父目录 writer，应离线规范权限或把
  tombstone authority 迁到专用 control plane。
