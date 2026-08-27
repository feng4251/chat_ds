# ChatDS 当前会话交接（2026-08-25）

> 本文件是本仓库唯一的权威续接入口。新 Codex/Claude Code 会话必须先完整阅读本文件，再查看 Git、测试和生产状态。旧 `_SESSION_*.md`、`_HARNESS_*.md`、`_REMOTE_OPS.md` 只用于历史追溯。

## 2026-08-27 原生 Turn 默认取消四小时墙钟上限（候选，未部署）

- 三源诊断已关联 conversation、immutable Skill 和 debug/AgentRun/tool 证据。Claude Session
  `1aede04995a149ef9991489c198993c4` 的 root `c0e06...`、DeepSeek Session
  `2184817c892446f9a195913f51aafb00` 的 root `b5a4...` 都在仍有原生 worker 活动时精确运行约
  14,400 秒后分别以 `run_hard_timeout` / `hard_timeout` 终止；生产两个 Supervisor 环境也都为
  14,400 秒。它们和成功续跑使用同一 `healthsim-trialsim` immutable Skill view、同一 route 和原始用户输入，
  未发现 Skill drift，因此是 ChatDS-owned Supervisor 的绝对墙钟策略，不是 Skill、前端或原生 Harness 的
  planning/tool-loop 行为。ShaIEngine 的可比任务只是没有越过四小时，并不绕过这条限制。
- `218...` 与 Qwen Session `f1044e1682d4477484eed98e643f9268` 首轮同时出现的 `exit_137`
  是另一类宿主/容器 SIGKILL：它发生在约 96--98 分钟，没有 native terminal，且当时的 Supervisor 没有持久化
  Docker kill/OOM provenance。取消四小时上限不会掩盖或宣称修复该独立问题。
- 通用不变量改为：原生 Turn 默认不设 ChatDS 总墙钟 deadline，由原生 Harness 决定何时结束；用户取消、Session
  删除、Supervisor shutdown、provider/read-idle、egress 与资源边界继续有效。部署方仍可用正整数显式设置有限时长，
  `0` 或未设置表示无限；非法、负数和小于 60 秒的正数 fail closed。Supervisor 重启接管运行中容器时保持同一语义。
  实现只修改 `native_security/run_deadline.py`、两个 ChatDS Supervisor/config、Compose 默认值、文档与测试，
  未修改 `claude-code/`、`deepseek-harness/` 或 `deepseek-harness-clean/` 的原生源码。
- 成熟实现对照冻结于本地 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`、tree
  `ef7589945b3767ead85fc52f68d013f88094bd47`。其顶层 native Turn 以显式 cancellation/AbortController 收口，
  timeout 用在具体操作边界而非整轮墙钟。这里选择 **adapt** 其生命周期边界到现有 Supervisor receipt/authority；
  **reject** 复制原生 agent loop 或添加另一个 retry/compaction/terminal 状态机。
- 确定性、跨域验证使用非医疗 `warehouse-planner` holdout：Claude config + lifecycle `47/47`、Backend
  DeepSeek contract `58/58`、DeepSeek lifecycle `2/2`；Compose config、AST、`git diff --check` 均通过。
  新建但未部署的候选镜像为 Claude Supervisor
  `sha256:9a27cadf574c95e558c556503f1e4a1ea7f6387a28ae2d2c345e0eafaca0a597`、DeepSeek Supervisor
  `sha256:1ac5089da6f3431f82d4c5f3672cb3fb97218f517c86402b22311f2e9a8ab89c`，镜像内回归同样通过。
  当前生产容器尚未替换/重启，仍使用 14,400 秒环境值；部署前不得声称生产已取消限制。

## 2026-08-26 单仓库快照整合

- 用户明确要求在不改变当前源码版本的前提下，将项目目录内的嵌套 Git 边界整合到 ChatDS 根仓库。本次采用
  snapshot vendoring，不导入第三方完整历史：`claude-code/` 固定来源 commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`、tree
  `ef7589945b3767ead85fc52f68d013f88094bd47`；`deepseek-harness/` 与
  `deepseek-harness-clean/` 都固定来源 commit
  `47f943859bef60e4160492346772ded9b24f765a`、tree
  `f904efab9ef435201d6ba4da88a34d6366568272`；`hermes-agent/` 固定来源 commit
  `6c73e8ffaa7b8df1e7b2f9d5792b4ee027e41637`、tree
  `29759e962655e2ba1d8bd5b70ac1d356a22070c0`。
- `deepseek-harness-clean/` 不再是 submodule/gitlink；普通 clone 已包含全部固定快照。Claude Code 与 DeepSeek
  Harness 目录继续只读，ChatDS 原生边界约束不变。历史交接中“独立仓库/子模块”的描述是当时事实，本节从当前
  提交起取代其仓库布局含义。
- 整合前根 HEAD 为 `67ecafdc3d13cea86be4ab41aa2e537de4c8f0f1`，本地安全分支为
  `backup/pre-monorepo-20260826`。嵌套 Git 元数据保存在项目目录外、权限 `0700` 的
  `/nfs/yangbb/codes/chat_ds_nested_git_backup_20260826_snapshot/` 可回滚备份中；不得提交该备份。
  两项用户自有 tracked deletion 始终保持 unstaged，既有 runtime/Session/reference 未跟踪数据没有被批量加入。
- 本次只改变 Git 索引布局和对应维护文档，不修改产品/原生源码字节，不部署，也不远端 push。

## 2026-08-25 Qwen xhigh provider 方言、原生角色保持与生产验收

### 三源诊断与两阶段确定性复现

- 历史 conversation `96a9db2a07bf474880f24a79f934da28` 是
  `deepseek_harness + qwen3_5 + workspace_write`，用户输入仅为“你好”。持久化 root
  `97dd37bb0094416e805fe045022d03c2` 在约 3 秒内以 `provider_http_400` 失败、0 token；原始 DSH
  event 明确是供应商拒绝 `reasoningEffort=max`，其可接受值为 `xhigh`、`medium`、`low`。该 Session
  的 immutable Skill view 为
  `7333785203fbd3bb3695c75aaf01399b9e8af5770d251879089fb636ec1dae45`，selected Skill、worker、route、
  artifact contract 与 diagnostic 均为 0。因此不是 Skill、sandbox、工具、模型判断或前端问题，而是 ChatDS
  provider profile 没有把 canonical `max` 绑定到该端点的 wire spelling `xhigh`。
- 首次通用实现 `d7c917b6` 将 per-model wire effort 声明加入 Supervisor profile，并尝试使用 DSH 已有的
  generic pi-ai route。纯 provider 测试可把 `max` 发成 `xhigh`，但生产最小复现 conversation
  `ff9ce9c20c6b43ec8a53945f9d1069f9`、root `29b18a3c808748c9880a28bd1d78b8c5`
  证明完整 turn 随后被端点以 `Unexpected message role` 拒绝。三源再次一致：同一空 Skill view、持久化输入只含
  一条普通 user message、runtime receipt 已显示 `xhigh`；raw request/header event 表明 generic adapter 的
  URL 自动探测使用了供应商不接受的 developer role。这说明“推理别名”不能通过换序列化器实现。
- 通用不变量因此收敛为：deployment 可以把一个 canonical reasoning level 映射成 endpoint wire spelling，
  但必须保留原生 adapter 的 system/user/assistant/tool 角色、工具形状、stream/retry 与 Session 行为；映射还必须
  同时精确绑定 endpoint、model 和字段，未声明的模型完全不变。非医疗 `warehouse-planner` 与
  `museum-curator` 镜像回归分别验证 `max -> xhigh`、`max -> ultra`，并验证非目标模型、非目标 endpoint 仍发
  `max`，原生消息 roles 始终为 `system,user`。

### 最终实现边界与成熟实现对照

- 收敛提交 `31384089` 保留上游 `llm-deepseek` 作为所有模型的唯一 DSH provider route；只有 profile 显式声明
  非 `max` spelling 时，ChatDS-owned runner invocation 才通过 Node `--import` 加载
  `provider_wire_profile.mjs`。该模块只在 exact base URL + `/chat/completions`、exact model、POST JSON、
  `reasoning_effort=max` 同时匹配时改写该字段；它不读取或改写 messages、tools、headers、response、retry、
  planning 或 terminal。没有 Qwen/Session/Skill/业务词进入生产模块；生产 Compose 的唯一非默认 profile 数据是
  `qwen3_5: xhigh`，其他模型继续使用原生 `max` 且命令行不加载此模块。
- 成熟实现对照仍冻结为 clean `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`：其 `src/utils/effort.ts` 先按模型能力解析规范档位，
  `src/services/api/claude.ts` 再序列化 wire value。ChatDS **adapt** 这个 resolve-then-serialize 分层到已有 provider
  profile/runner boundary；**reject** 复制 agent loop、改消息角色或 patch provider body after response。
  `deepseek-harness-clean/` 仍 clean 于
  `47f943859bef60e4160492346772ded9b24f765a`，Claude Code 与 DeepSeek Harness 原生仓库均未修改、重建或 fork。

### 验证、生产切换与当前状态

- 确定性验证：Backend DeepSeek/engine contract `108 passed, 4 warnings, 2 subtests`；相关 DSH Node
  `17/17`；Compose config、Python compile、Node syntax、`git diff --check` 均通过。clean Git archive 构建时的
  candidate-image integration 真实调用原生 `llm-deepseek` serializer 并通过两组 rename holdout；runner OCI
  label 仍为 `upstream-unmodified=true`、revision
  `47f943859bef60e4160492346772ded9b24f765a`。
- 生产仅重建 DeepSeek runner anchor 与 Supervisor。当前 runner image 为
  `sha256:7ae3d760238048b02f9e733f9e3cb256423a9d55206dc40b18376d361fece491`，Supervisor 为
  `sha256:736f47c79b08f857319ea38c6d5daf5d074ec0b1d0e09108e0b84fd5f2308a14`；均 restart 0，
  Supervisor healthy。切换前镜像分别保留为 `rollback-pre-31384089`。Backend、Frontend、Claude Supervisor
  的启动时间未变化，Claude 没有重启或改动。
- 第一个最终候选生产 smoke conversation `10f63438d38745fbb7028e4f051abbf0` 已越过两个 400，但供应商先返回
  HTTP 502，随后原生 retries 收到 TRANSPORT，故真实失败且未伪造成功。同端点、同 system/user、
  `thinking.enabled + xhigh` 立即直连返回 200/stop/usage；再次运行的完整 ChatDS conversation
  `730ce419c1e24a25bab47d0747be4a2c`、root `3c4abfac473446eba7c547d9c945d44e`
  成功，assistant 精确包含 `QWEN_XHIGH_SMOKE_OK`。持久化 AgentRun 为 `succeeded/stop`、8,210 input +
  51 output token；runtime receipt 为 `deepseek-official + xhigh`，provider error 0，artifact receipt
  `not_applicable`，Supervisor 唯一 terminal 为 exit 0/succeeded，派生 root event 唯一 `run.completed`。
  这把前一轮归类为上游瞬态，不制造额外代码修复。
- 功能提交为本地 `d7c917b6` 与 `31384089`；本节文档提交仍按当前 local-only 规则处理，未因本轮自动 push。
  历史失败 Session 保持原终态，新 turn 使用修复后的 profile。两项用户自有 tracked deletion 继续保持 unstaged，
  大量既有 runtime/reference untracked 目录不得加入 Git。

## 2026-08-24 Round 18 原生双引擎 E2E、passed-frontier fan-in 与生产切换

### 新鲜 Session 终态与不可变输入

- 用户新建的同输入 E2E 已全部进入权威终态。DeepSeek Harness conversation
  `681e814526b54e2295c009207508f2b8`、root `0417d05f48b14ef4a68444d2666b601f` 为
  `succeeded/stop`；Claude Code conversation `db051b53fe7b4824a92eb27dc3d52f95`、root
  `db8fd6a7be1e45f7ad7eb1fc695769b5` 为 `failed/workflow_contract_failed`。两者绑定同一 input SHA-256
  `2f042f8dec9eaf2f79994e634a81da9ab11408e53dbd42952a41be049f43787c`、同一 immutable Skill view
  `9aee2a0596eff2e78c48c615332e70ed3d82abc9539b701663ea7076af96d7b8`、同一 manifest 和
  `composite_full_protocol_design` route。route 声明 7 个并行 mandatory worker，再串行 1 个 I/E worker；
  final 硬合同至少 153,600 bytes/2,000 lines。
- DeepSeek 8 个声明 worker 均 attempt 1 succeeded；workflow/artifact receipt 都 passed、0 finding，ledger
  terminal seq 405,163。`GAL301_FULL_REPORT.md` 为 153,967 bytes/2,627 lines、SHA-256
  `237c13179cf98cee354cd84c0797a1b7656f4e315443b3e3c241a85d9b778a53`，与 11 模块顺序 concat 完全相同。
  首稿低于 byte 合同后，模型按 machine `artifact_min_bytes_not_met` 反馈在原生 loop 内扩写并通过；本轮可作为
  DeepSeek V2.3 business acceptance。
- Claude fresh skill invocation、workspace、ledger 与投影都正确，7 个 phase 0 worker 全成功；但模型先启动
  1 个 generic agent，随后把其输出当作 phase 1 草稿，从未调用声明的精确 I/E agent。它在 prose/result 中
  声称所有 phase 成功，机器 receipt 仍准确记录 phase 1 attempt 0/missing。Supervisor seq 127,793 唯一终止为
  failed，artifact audit deferred；8 次 provider retry 均恢复，egress 无 budget rejection，因此不是网络、沙箱、
  Claude binary/core 或 workspace isolation 故障。失败草稿 final 为 147,529 bytes/2,520 lines，虽等于 11 模块
  concat，仍低于硬性 byte 合同，不能作为交付成功。

### 本轮通用缺陷与修复边界

- 旧 ChatDS PreToolUse 只反馈 `frontier=N`，Claude 在本轮产生 69 次 frontier 0 和 8 次 frontier 1 的重复
  artifact 尝试；mandatory frontier 前还允许 optional/generic root agent。模型又用临时文件加多行 Python
  remove/rename 绕过静态 Bash path recognizer。若它之后补齐 worker，旧 turn-start snapshot 可能把 workflow
  前已生成的 final 误判为合法 fan-in。这两个缺陷都在 ChatDS adapter/control plane，不在原生 Claude Code。
- 功能提交 `fbc87906` 只修改 ChatDS Claude adapter/test：mandatory frontier 前拒绝未声明 root Agent；每次
  blocked fan-in 返回当前 phase 精确 `native_agent_type/status` 和可执行动作；workflow 首次 passed 时原子写入
  root-owned synthesis checkpoint，保存 workspace identity 与 declared-final SHA-256。Stop hook 和 terminal
  audit 都要求 final 在该 checkpoint 后真实内容变化；预生成、rename、touch、metadata-only 与损坏 checkpoint
  全部 fail closed。没有新增 control prompt、第二套 loop、provider retry/compaction 或 fixture/model/session 特判。
- 成熟参考仍唯一冻结为 clean `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`：adopt native PreToolUse actionable feedback，adapt durable
  passed-frontier checkpoint 到现有 authority/receipt，reject PostToolUse rollback 与 core patch。DeepSeek 上游
  `deepseek-harness-clean/` 仍 clean 于 `47f943859bef60e4160492346772ded9b24f765a`。
- 非医疗 warehouse/museum failure injection 覆盖 inline Python 静态绕过、pre-frontier final、metadata touch、
  post-frontier rewrite、optional agent、精确反馈和 corrupt checkpoint。最终 Runner contract `56/56`、
  Supervisor lifecycle `38/38`、image self-test、AST、genericity scan、`git diff --check` 全通过。

### 当前生产与下一步

- 从 `fbc87906` clean archive 构建的生产 Claude Turn image 为
  `sha256:c67c9faccf0272617622e21671901b97edafcc30e91b928d6c70e535b6e59602`，OCI revision `fbc87906`；
  旧 `sha256:c2a6d2dd...` 保留为 `rollback-pre-fbc87906`。只重建了 inert Claude runner image anchor；它为
  read-only/network-none、restart 0。Claude Supervisor 未重启且 healthy/restart 0，仍通过 tag
  `chat_ds-claude-runner:2.1.152` 创建后续 Turn。Backend、Frontend、DeepSeek、SearXNG、egress、原生 harness
  与所有 Session workspace 均未重建。
- 三个入口 `/api/health` 均 200、storage identity 一致。不要自动再次运行模型重型 V2.3；DeepSeek 已通过，
  Claude 的修复后 acceptance 等用户新建 Session。下一次必须继续做三源终态闭环，不能把模型 prose 当 receipt。
- 两项用户自有 tracked deletion 仍必须保持 unstaged：
  `XGAL-101_Galectin-3_AD_Comprehensive_Development_Plan_v1.0_claudecode执行参考.md` 与
  `xClinicalTrial-Design-V2.2.zip`；大量既有 untracked runtime/reference 目录也不应加入 Git。

## 2026-08-24 DeepSeek workflow、原生终态恢复与 Turn 时间线修复

### 恢复基线与不可变边界

- 本轮从 `e5657ed0b8d0bdd80b9325fc0688ce297668d968` 继续；该提交已经包含 Claude 会话
  `59903124-d548-48c5-bf38-f069322cd5e3` 的可验证工作链
  `341df61c -> ecbfdfc5 -> a3a2a487 -> 1d72515c`，以及后续原生双引擎/通用 Skill 合同提交。
  本轮没有修改上游原生内核：成熟实现对照 `claude-code/` 固定且 clean 于
  `6f6f12b37f529488b10e53928dd5508bb93535c7`；DeepSeek 上游边界仓库
  `deepseek-harness-clean/` 固定且 clean 于
  `47f943859bef60e4160492346772ded9b24f765a`。所有改动都在 ChatDS adapter、control plane、
  lifecycle、派生活动协议、网络策略与 Web UI 内。
- 生产不再有 Legacy Harness。Claude Code/DeepSeek Harness 继续原生拥有 planning、tool loop、
  sub-agent、compaction/retry 和 Session 行为；ChatDS 没有增加第二套 agent loop、控制 prompt 或
  V2.3/session/model 特判。

### 两个故障 Session 的三源结论

- `a36e8cfb770143fea0c726abf9ccac1b`：持久化 Conversation 是
  `deepseek_harness + shaiengine_glm_5_2 + session_full`，root
  `73cbb06140824813948d14bf7a1a6b68`。冻结的 V2.3 包/Skill view 与 Round 17 相同；模型确实只调用
  一次无参数原生 `execute_skill_workflow`，不是“模型没调用工具”。原生 tool result 明确返回
  `SCRIPT_PARSE / SyntaxError: Invalid or unexpected token`；根因是 ChatDS 编译的通用 workflow
  JavaScript 把模板中的 `\n` 变成单引号字符串内的真实换行。Supervisor 不可变 terminal 位于 ledger
  seq `243577`，为 `failed/workflow_contract_failed`；历史 `error_stage=egress_bridge_seal` 是旧 adapter
  误归因，不能改写该原生账本。
- 同一 run 的 native ledger 为约 117 MiB/243,577 行。旧 Supervisor 用
  `Path.read_text().splitlines()` 扫全文件而 OOM；Backend 又没有 DeepSeek terminal recovery，所以数据库
  root/children 长期显示 running。工具开始事件把 call ID 放在 payload，而 safe TurnActivity 只读取顶层；
  工具结果的 ID 又位于 DSH `message.content[].toolCallId`/`message.source.callId`，旧 projector 没有解析，
  因而前端把同一次调用显示成一张“执行中”和另一张“完成”。不可见 workflow 聚合事件还错误切断
  reasoning/content stream，形成 90 个正文节点和 856 个思考节点。
- `3b92e02e644c41e3a705940c199a7b0f`：持久化 Conversation 是
  `claude_code + shaiengine_glm_5_2 + session_full`，root
  `a4cb1266b41142bf9c117d99383a3537`。Claude 原生请求收到了 ChatDS egress proxy 的
  `403 policy_outbound_budget_exceeded`；64 MiB Session 累计预算在长上下文 provider exchange 中被代理
  开销先行越过。这是 adapter/policy 配额，不是 Claude Code、GLM-5.2 或 V2.3 内容问题。历史 root 保持
  `failed/workflow_contract_failed`，没有伪造成功或重放副作用。

### 通用不变量与实现

- 编译器不变量：Skill 声明生成的通用 workflow program 必须在交给原生引擎前可解析。修正仅转义程序模板
  内部换行；新增 museum/warehouse rename holdout 验证 phase barrier 和 only-failed-member retry，未嵌入
  疾病、报告名、固定 worker 数或 V2.3 route。
- 账本/生命周期不变量：大 JSONL ledger 必须有界流式扫描和有界 replay；Supervisor terminal 由 exact
  user/conversation/run identity 读取，Backend 重启先恢复原生 terminal，再撤销无 terminal 的 run；root
  terminal 后所有 descendant 必须终态化。DeepSeek child 继承 root engine identity，不再落入 ORM 的
  Claude 默认值。恢复出的 AgentRun terminal 还会幂等校正安全的派生 workflow card，避免根失败但 Web
  仍显示取消/child running；原生 ledger 不被修改。
- 活动/UI 不变量：一个 call ID 对应一张 card；start/completed/failed 在原 node 原位 merge，并保留 start
  metadata。DSH live projector 支持顶层、`message.source` 与 `message.content[]` 三种结果身份形态；启动
  migration 只从 immutable raw receipt 重建缺失的派生 call ID/node。不可见 workflow 更新不切断文本；
  前端对历史相邻同类流节点再做安全 compact，真实 tool/reasoning/content boundary 保留。权威 root terminal
  后没有 receipt 的 open tool 显示失败/取消，不再永远“执行中”。
- 网络不变量：保留 exact origin/path/method、response、request-count、deadline 和 Session scope 控制，
  只把 Claude/DeepSeek/provider policy proxy 的默认累计出站预算从 64 MiB 提升到现有绝对上限 1 GiB，
  避免正常长上下文交换被误拒绝；不是去掉网络策略。
- 成熟实现对照仅使用本地 `claude-code/`：其原生 query 在 result 前 drain pending SDK/task events，
  structured I/O 保留稳定 request identity，Session state 与 terminal 由原生 loop 持有。ChatDS **adapt**
  稳定身份、pending/terminal replay 和 one-workspace boundary 到已有 authority/receipt 合同；**reject**
  复制 query loop、从 prose 推断 terminal 或对业务 fixture 特判。

### 验证、部署与当前生产状态

- 确定性回归：Backend 相关套件 `135 passed, 52 warnings, 2 subtests`；Claude config
  `6 passed, 3 subtests`；DeepSeek 通用 workflow Node `4/4`；Frontend `52/52`；Vite production build、
  Python compile、Node syntax、`git diff --check` 均通过。warnings 仍是既有 passlib/
  `datetime.utcnow()` deprecation。全量 ESLint 仍有两个既有、未触碰文件的
  `react-hooks/set-state-in-effect`：`ModelSelector.jsx:70`、`SkillLibrary.jsx:36`；不影响 build，不能记为
  本轮回归。
- 候选 DeepSeek runner image 在 `network=none + read-only + tmpfs` 下执行非医疗
  `native_workflow_plugin.image.mjs` 通过，证明生产镜像内 `execute_skill_workflow` 可注册、解析、执行并发出
  完整 `tool-workflow/*` 事件。按约束没有自动发起新的模型重型 V2.3 E2E；下一次仍应由用户创建全新
  conversation/root。
- 生产当前 Backend `sha256:0054251e...`、Frontend `sha256:498c5326...`、Claude Supervisor
  `sha256:11952220...`、DeepSeek Supervisor `sha256:e69a056b...`、egress proxy
  `sha256:8bfa4a42...`，均 restart 0；有 healthcheck 的组件 healthy。Frontend build entry 是
  `/assets/index-BoKpCOIW.js`。三处运行环境的累计出站预算均回读 `1073741824`。
- SearXNG 曾因从临时 Compose CLI 的 `/repo` 工作目录部署而误挂宿主 `/repo/searxng` 默认配置，表现为
  health 通过但 `/search` 429。已用宿主同路径挂载
  `/nfs/yangbb/codes/chat_ds:/nfs/yangbb/codes/chat_ds` 重新应用 `chat_ds` 项目；当前 config mount 精确为
  项目 `searxng/`，从唯一联网的 egress proxy 查询 `OpenAI` 返回 24 条结果。误建且无容器引用的
  `/repo/searxng` 已精确清理；后续所有 nested Compose 命令必须保持该绝对同路径挂载。
- 启动时从 raw receipts 修复 519 个历史 DeepSeek tool activity，并从 durable AgentRun receipts 校正 70 个
  workflow terminal activity；独立新进程再次运行 terminal migration 返回 0。生产 `a36...` 用与前端相同
  reducer 回放后为：12 个正文块、41 个思考块、201 张工具卡（191 完成、10 失败、0 执行中）、root failed、
  9 children cancelled。`3b92...` 仍为真实 failed，未重写业务结果。

## 2026-08-21 原生双引擎、通用 Skill 执行与 V2.3 新鲜验收闭环

- 已恢复并审计 Claude 会话 `59903124-d548-48c5-bf38-f069322cd5e3` 的工作。恢复基线提交链为
  `341df61c`（统一审批控制环）、`ecbfdfc5`（Claude 原生 stdio permission relay）、
  `a3a2a487`（DeepSeek 原生工具面与 SearXNG）和 `1d72515c`（前端白屏修复）。本轮在该基线上完成
  通用修正；`claude-code/` 与 `deepseek-harness-clean/` 两个独立上游仓库都保持未修改。
- `LegacyAgentEngine` 已从 registry、runtime health、Compose 和生产容器彻底退出；
  `backend/agent_engines/legacy.py` 是本轮有意删除。Claude Code 和 DeepSeek Harness 都由各自原生
  loop 负责 planning、tool loop、multi-agent、compaction/retry 与 session 行为，ChatDS 只保留
  Web/user/Session、精确 workspace、provider/model、Skill/plugin/MCP、权限、SSE/persistence、
  cancellation、artifact audit 和唯一 terminal 的边界适配。不得重新引入 ChatDS-owned agent loop。
- 通用 Skill compiler 现在把包内声明编译为不可变 Skill view、worker/route workflow IR、条件 authority、
  mandatory frontier 和 artifact contract。顺序固定为 compile/bind -> decide authority -> mandatory receipts
  -> optional retrieval -> synthesize/fan-in -> artifact validation -> exactly-one terminal。恢复不得跳过当前
  mandatory frontier，模型 prose 不得替代机器 receipt。所有生产逻辑均不含 V2.3、疾病、Session/Skill ID、
  固定 worker/file 数、route 名或报告名特判；对应回归含重命名的非医疗/跨领域 fixture。
- Claude 权限保持原生 stdio `can_use_tool` 往返，页面提供 `read_only`、`workspace_write`（逐次 Allow/Reject）
  和 `session_full` 三档；三档真实 E2E 均通过。DeepSeek 使用经 `SO_PEERCRED` 校验父 PID 的 Unix event
  socket、原生 Session/event stream、exact tool identity 和原生 permission service；空 tool-name delta、
  并行审批、stale/replay、artifact gate 与唯一 terminal 均有通用 regression。DSH 不再通过粗粒度 plugin
  group 扩大精确工具授权。
- 已恢复本机 SearXNG/Valkey，并恢复精确 limiter；Claude/DSH 只有 Web search 使用 ChatDS 的 SearXNG
  接口，其余执行在各自 session-wise、无 ambient network、单 workspace 的原生沙箱闭环内。生产搜索
  smoke `d0de2026082100000000000000000044` 精确调用一次 `web_search`、返回 8 个来源并唯一成功终止。
- 历史 session 三源归因已经闭合：`ecbc00...` 是旧 Legacy compaction placeholder 泄漏到工具参数并被
  盲目重试；`a993...` 是 provider 连续 60 次 `finish_reason=length` 后旧 Legacy continuation 不收敛，
  不是 agent/chat 分类；`bf7f...` 暴露旧 workflow 编译、审批与搜索超时组合；`9aef...` 暴露 blank
  tool identity 与搜索重试；`e140...` 本身是健康的简单 DSH turn；`5d66...` 是历史 plugin rollout；
  `fc39...` 的 Claude/market 路径健康，天气检索受旧 SearX 429 影响。修复均落在通用 compiler、provider
  bridge、native lifecycle、authority/receipt、SearX policy 上，没有按这些 session 分支。
- 生产已从当前源码重建并整体切换：Backend `sha256:862dd156...`、Frontend `sha256:5f1dd9bd...`、
  Claude Supervisor `sha256:e70054b3...`、DeepSeek Supervisor `sha256:f24a9ab...`、Skill egress
  `sha256:b434765...`；Backend、两个 Supervisor、egress、SearXNG/Valkey 均 healthy/restart 0，Frontend
  实际 Chromium 渲染非空且无应用 ReferenceError。`127.0.0.1`、`10.10.132.126`、`172.30.100.126`
  三入口 `/` 与 `/api/health` 均 200，生产无 Legacy 容器。
- 2026-08-20/21 的 V2.3 新鲜生产 E2E 使用 conversation `00e4881af558441595ab4e0bdba05992`、root
  `2d0a6b1cef46411f87b1c60bed8053b7`、Claude Code + `AgentModel`、`session_full`。输入 ZIP SHA-256
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`；冻结 view
  `9aee2a0596eff2e78c48c615332e70ed3d82abc9539b701663ea7076af96d7b8` 含 19 Skills、9 workers、
  17 routes、0 compiler diagnostics。选中 `composite_full_protocol_design` 后 7 个并行 mandatory worker
  和 1 个顺序 worker 全部 durable succeeded；workflow/artifact contract 都 passed、0 finding，最后只有
  一个 `succeeded/end_turn` terminal。
- 该轮产生完整 14 文件合同：11 个模块、README、checklist 和 canonical full report。最终报告
  168,549 bytes/2,735 lines，SHA-256
  `7f305bd828e47a7ce1cf4ed4569f6d05acf0d389ad813b9f15c4590947557507`，且是 11 个模块的逐字顺序
  concat。12 个原生 Bash error tool-result 均位于可选分支（7 timeout，其余 nonzero/missing/runtime），
  没有 placeholder、malformed JSON、approval deadlock、write retry storm 或 mandatory frontier 越级。
- Ground truth 应作为业务/结构 acceptance oracle，不应要求随机生成文本逐字相等：新鲜结果与
  `GAL3_AD_FULL_REPORT_v2.3_glm52.md` 的 token cosine 为 0.9076、字节量 84.2%；与 Claude modular
  ground truth 的 token cosine 为 0.8884、字节量 87.3%。两份 ground truth 彼此也不逐字相同；三者均覆盖
  同一 11 个核心模块和 14 文件交付合同。若未来要求 byte-identical，必须另定义确定性模板/复制产品，不能
  将其伪装成通用 Harness 指令遵循。
- 成熟参考冻结为 `claude-code/` commit `6f6f12b37f529488b10e53928dd5508bb93535c7` 与
  `deepseek-harness-clean/` commit `47f943859bef60e4160492346772ded9b24f765a`。采用/适配的是 Claude
  typed control、pending background-task result holdback、唯一 result，以及 DSH session-fenced job、
  idempotent terminal 与 workspace boundary；拒绝复制上游 loop 或用模型自述补 controller state。
- 最终回归：Backend `371 passed, 2 subtests`；Claude/DeepSeek Python contracts
  `131 passed, 6 skipped, 19 subtests`；DeepSeek Node `15/15`；Frontend `49/49` 与 production Vite
  build；`compileall`、`git diff --check` 均通过。本轮最终 Git 只做本地 commit，不向远端 push。
- 两项用户自有 tracked deletion 继续只留在 worktree，严禁恢复或 stage：
  `XGAL-101_Galectin-3_AD_Comprehensive_Development_Plan_v1.0_claudecode执行参考.md` 与
  `xClinicalTrial-Design-V2.2.zip`。

## 2026-08-14 DeepSeek Harness 平级引擎最终部署闭环

- DeepSeek Harness 平级引擎已完成、提交并部署生产。功能提交链为
  `1de97739`（平级引擎、Session/Turn 隔离、UI Harness 选择器、SearXNG）、
  `71f4aa93`（模型/provider binding）、`d4f71e33`（Docker daemon volume
  namespace）、`c3a91cd8`（官方 workspace package/plugin graph）、`8fbc5277`
  （官方 permission preset）、`1a2131bb`（官方 headless loader flag）、
  `e5cef02e`（显式 Session worker environment）和 `e61ab07e`（原生 assistant
  event 到 ChatDS DTO 的正文/思考投影）。上游
  `deepseek-harness-clean/` 仍是未修改的独立子模块，精确 commit
  `47f943859bef60e4160492346772ded9b24f765a` / `0.1.0-rc.5`；ChatDS 只在
  `deepseek_runner/` 和 Backend `AgentEngine` adapter 中做多用户 Web 外壳适配。
- 每个 Turn 使用一个 fresh、`network_mode=none` 的非特权容器；只挂当前 user/current
  Session 的 workspace、Session-owned state、immutable Skill view 和 controller receipt，
  不挂其他 user/Session、宿主代码、Docker socket 或 ambient HOME。PID 1 是唯一可信控制面，
  负责签名 exact egress、artifact audit、cancel/cleanup 和唯一 authoritative terminal。
  Turn 完成后容器被删除，workspace/state 通过精确 Session mount 保留。
- Frontend 输入区的 Harness selector 已位于 model selector 左侧。Engine/model compatibility
  是部署数据驱动、fail-closed；首次 durable message/run 后 engine 锁定，切换需 fork。
  Scheduler、Conversation cleanup 和 lifecycle 使用通用 native-engine 路径，同时覆盖 Claude
  Code 与 DeepSeek Harness；Legacy 仍保留为 history/rollback，Claude Code 原生内核未改。
- DeepSeek Harness 的 `web_search` 使用官方 Web provider plugin 接到 ChatDS SearXNG。
  模型/搜索都经 policy-v3 签名 exact egress，Turn 容器本身没有网络；SearXNG 由
  `deepseek-harness` Compose profile 启动，未开放成任意宿主端口。真实生产 E2E 中 DSH
  发出 `web_search`、得到成功 tool result，并返回
  `https://github.com/deepseek-ai/deepseek-harness`；SSE、持久 assistant 和唯一成功终态一致。
- 最后一项真实 E2E 缺陷不是 DSH/model/provider/sandbox/network：官方 ledger 已包含完整
  `assistant/chunk` 的 `text-delta`/`reasoning-delta`，但旧 adapter 把 ChatDS 标准字段误写为
  `delta` 而 `_NormalizedEngineResponse` 只接受 `text`，造成持久 assistant 为空。
  `e61ab07e` 将跨引擎 DTO 统一为 `{"text": ...}`，并用官方事件形状及重命名 child fixture
  回归。官方 DSH `apps/cli/tests/built-bin.e2e.ts` 同样逐个累加 `chunk.text`，
  `packages/bundle/headless/src/index.ts` 从 `assistant/message` 汇总最终文本；采用实时 chunk +
  machine terminal，拒绝从模型 prose 推断控制状态。成熟对照冻结本地 `claude-code/`
  `6f6f12b37f529488b10e53928dd5508bb93535c7`；其 `src/cli/print.ts` 分离流事件与最终
  `result` 的做法被适配在同一个 normalized event/authoritative terminal 边界后，没有复制
  stub 或修改任一原生内核。
- 最终真实文本 E2E 经 `127.0.0.1:5173` 完整通过：stream 文本、持久 assistant 均精确为
  `DSH_PROXY_OK`，reasoning 有 72 字符，且只有一个 `run.completed` 和一个
  `stream_terminal=succeeded`；成功临时 Session 已用正常删除 API 清理。前述真实搜索 E2E
  也通过并清理。没有运行模型重型 V2.3；它仍由用户手工验收。
- 最终回归为 Backend `341 passed`（临时测试路径补入与生产相同的
  `croniter==6.2.4`；缺该环境依赖时先得到 339 pass + 2 个预期
  `schedule_parser_unavailable`）、DeepSeek/Claude engine 契约 `58 passed`、Frontend
  `47 passed` 与 production build。Compose `deepseek-harness` profile、SQLite
  `quick_check=ok`/foreign-key violations 0、三入口
  `127.0.0.1`/`10.10.132.126`/`172.30.100.128` `/api/health` 200、零 nonterminal
  AgentRun/active Engine Session/running schedule/dynamic DSH Turn container 均通过。
- 最终 Backend 从 exact clean archive `/tmp/chat_ds_deploy_e61ab07e.s9aeLB` 构建，image
  `sha256:f20234a48aeb5468e6b7b9a25ea111060b41c79b7279541bc5a4cfc30b0cb742`；
  Frontend 为 `sha256:116685c5a4a9ac779e00305c27421f08935826275c547b88ef9332a4ab3293db`；
  DeepSeek Supervisor 为
  `sha256:505430c29a5a62076c7ab6d0ed0de204b0710c7c2852fb62391327848625b08f`；
  Runner 为 `sha256:7487f260e26f9902ce57df65bb4ebaf248c25502613112df03dca9bdb7ee1030`。
  容器均 running、restart 0，Backend/Supervisor healthy。Backend 切换前镜像保留为
  `chat_ds-backend:rollback-pre-e61ab07e`（image
  `sha256:0eb7fc25a910856ed785adf70d47e11e78fd3fdbe22382cef993b9580fd98cde`）；整个功能前
  Git 回滚锚点为 annotated tag `rollback/pre-deepseek-engine-20260814`，peeled commit
  `ff0e7971b4aa6701a1f439f0df30702ffb4212af`。
- 两项用户自有 tracked deletion 继续只留在 worktree，未 stage/恢复/提交：
  `XGAL-101_Galectin-3_AD_Comprehensive_Development_Plan_v1.0_claudecode执行参考.md`
  与 `xClinicalTrial-Design-V2.2.zip`。用户于 2026-08-15 明确授权本轮远端同步；当前完整
  历史已推送到 `https://github.com/feng4251/chat_ds` 的
  `fix/deepseek-harness-peer-engine-20260815` 分支，回滚标签
  `rollback/pre-deepseek-engine-20260814` 也已推送。远端原有同名历史分支
  `fix/generic-skill-harness-20260717` 与本地没有共同祖先，因此未 force-push、未做
  unrelated-history merge、也未改写该远端分支。

## 2026-08-14 DeepSeek Harness 平级引擎实现（部署前）

- 官方 `deepseek-ai/deepseek-harness` 以独立 Git 子模块固定在
  `deepseek-harness-clean/`，commit 为
  `47f943859bef60e4160492346772ded9b24f765a`（`0.1.0-rc.5`，MIT）。
  ChatDS 不修改该源码树；`deepseek_runner/` 仅实现适配与可信控制面。
- 新增平级 `AgentEngine`：`deepseek_harness`。每个 Turn 启动一个
  `network_mode=none` 容器，只挂载该用户当前 Session 的 workspace、Session
  自有运行状态、不可变 Skill 编译视图和控制器收据。模型进程以非特权用户运行；
  PID 1 独占出网、产物发现和唯一权威终态。
- 模型/Provider 绑定由部署配置精确授权并 fail closed。OpenAI 模型流量与
  SearXNG `/search` 都经过已有签名精确出网代理；不挂载宿主代码、Docker
  socket、其他用户或其他 Session 路径。`deepseek-harness` Compose profile
  会同时启用 SearXNG。
- 输入框内新增 Harness 选择器，位于模型选择器左侧；模型列表依据显式引擎兼容
  关系过滤。首次持久消息/Run 后 Harness 锁定，已有 Session 需 fork 才能切换。
- Scheduler、清理和生命周期事务以通用 native-engine 路径同时覆盖 Claude Code
  与 DeepSeek Harness；Claude Code 仍为薄原生适配器，本轮未修改其运行内核。
- 部署前验证：Backend `337 passed`；Frontend `47 passed`、定向 ESLint 与生产
  build 通过；DeepSeek/Claude contract suites 通过（旧 Claude 测试镜像仅有一项
  与本轮无关的 `/usr/bin/python3` 固定 fixture 差异）；Compose 校验、SearXNG
  实际查询和 DeepSeek 原生 SearX 适配器均通过。未启动模型重型 V2.3 E2E。
- 两项用户自有 tracked deletion 仍不得恢复或提交：
  `XGAL-101_Galectin-3_AD_Comprehensive_Development_Plan_v1.0_claudecode执行参考.md`
  与 `xClinicalTrial-Design-V2.2.zip`。

## 2026-08-14 Turn 时间线、多代理工作流与 Session 权限闭环

- 用户批准将 ChatDS 的固定“工具调用 / 深度思考 / 正文”三段式改为同一 Turn 内按真实发生顺序
  交替展示 reasoning、正文、工具、进度、工作流和权限请求；多代理改为一个可展开的总工作流卡，
  以 Claude 原生 AgentRun 的语义名称展示子任务、状态、工具、产物和失败原因。历史消息仍保留旧
  `reasoning/tool_progress/AgentRunCards` 降级渲染，只有具有完整 commit marker 的新投影才接管刷新后
  的显示，因此没有重写历史消息或把浏览器状态当成控制面事实。
- 新增引擎无关、安全白名单化的 `chatds.turn-activity.v1` 展示协议。Backend 按 root run 建立严格递增
  sequence、稳定 node identity 和 append/merge 语义；同一 DTO 先持久化再经 SSE 发送，页面刷新经
  authenticated Session API 重放。tool input、代码、provider 原始 payload 和凭据不进入展示表；消息
  以新增 `run_id` 精确连接 root projection。缺失原生终态时，顺序固定为先撤销仍可能存活的原生 Run，
  再合成失败展示并封存投影，最后执行既有 Message/AgentRun 权威终态事务。
- ClaudeCodeEngine 仍是薄适配层，原生 Claude Code 2.1.152 二进制和内部 agent loop 没有修改。
  `workspace_write` 使用 Claude 原生 `default` permission mode，并把原生 `control_request/can_use_tool`
  按 exact request id + Runner ledger sequence 持久化为页面权限卡；允许/拒绝经独立 authenticated API、
  Supervisor mailbox 和同一 stdin 返回原生 `control_response`，只有 stdin 实际接受后才发布决定收据。
  重复决定幂等，冲突/stale/跨用户/跨 Session/跨 root/已终态请求均 fail closed。
- 每个 Session 现在提供三个持久权限预设：`read_only`（workspace 只读 mount + Claude plan）、
  `workspace_write`（当前 Session workspace 读写 + 每次原生请求确认）和 `session_full`（仍在当前
  Session mount/出网边界内 bypass confirmation）。新交互 Session 默认为 `workspace_write`；历史行在
  schema migration 时明确保留 `session_full`，避免静默收窄既有行为；无人值守 Scheduler Session
  显式使用 `session_full`。活动 Turn 期间禁止修改权限，fork 精确继承。任何级别都没有其他用户、
  其他 Session、宿主目录或 Docker socket 的挂载。
- 前端设计证据冻结独立 `deepseek-harness-clean/` commit
  `47f943859bef60e4160492346772ded9b24f765a`：采用其
  `ui-conversation/conversation-nodes/assistant.ts`、`chat/ReasoningRow.tsx`、
  `ui-tool/ToolCallTree.tsx` 的事件顺序/可折叠呈现，以及
  `ui-conversation/skeleton/ApprovalPanel.tsx`、`PermissionSelect.tsx` 和
  `interaction/permission-presets` 的“workspace-write + ask / full + never”分离思想；适配在 ChatDS
  已有 durable AgentRun、用户/Session ACL 和 exact workspace mount 后面，没有搬入其单机目录模型或
  Harness 内核。Claude 协议证据冻结独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`：采用
  `src/entrypoints/sdk/controlSchemas.ts` 与 `src/cli/structuredIO.ts` 的 typed request/response、pending
  map、exact request id 和重复响应处理；不推测 stub，也不修改生产 Claude 内核。本轮本地路径完整，
  无需 Web 搜索。
- 回归为跨域/rename fixtures，不含 V2.3、疾病、Session、worker 数量或报告名特判。Backend 在生产依赖
  补齐的隔离测试路径为 331 项全量；Claude Runner/Supervisor 最终为 98 passed、1 skipped、14 subtests；
  Legacy Harness clean mount 为 1984 passed、1 skipped、813 subtests；Frontend 为 44 passed，变更文件
  ESLint 和 production build 通过。宿主 Python 未装 `croniter` 时 Backend/Runner 分别只出现 2/3 个
  `schedule_parser_unavailable`，使用 `/tmp` 临时依赖及生产镜像复验后全部通过；未自动运行模型重型
  V2.3 E2E。
- 主功能提交为 `32a215428dd41af99a4ddee25a68fd6e130c4c23 feat: add durable Turn timeline and
  Session permissions`。首次真实轻量 E2E 在默认 `workspace_write` 下确认原生 Claude 已发布成功
  `result`，同时暴露 `--input-format stream-json` 为接收后续权限决定而保持 stdin 打开时，Claude
  `--print` 会等待 EOF、导致权威 Run 迟迟不提交的通用生命周期缺口。该缺口重述为“收到 ledger 已确认的
  native result 后，交互输入通道必须关闭，使原生进程能够完成退出；stderr 或模型文本不得触发”，并以
  `BytesIO` scripted native-result failure injection 回归修复。修复提交为
  `9dc2b69fdf11ea209ad73edcfaa961a6946d59fd fix: close interactive Claude input after result`；
  没有增加 Provider、Skill、Session、文件名或业务域分支，也没有修改 Claude 二进制/内部 agent loop。
- 首次完整候选来自 exact clean archive `/tmp/chat_ds_deploy_32a21542.2NfI4Z`；最终 Runner 修复候选来自
  `/tmp/chat_ds_deploy_9dc2b69f.eBLMWu`，二者及对应 Git tree 均为 22,524 files，后者为
  251,976,166 bytes。部署前 SQLite online backup volume 为
  `chat_ds_chat_ds_db_backup_pre_32a21542_20260814_154623`（540,258,304 bytes，SHA-256
  `d2d350defaaa5e1a2d35fc0ba08e7eb66cd981aa9a3e0dfc76b3c96efa90f9c9`），备份和生产库均
  `quick_check=ok`、foreign-key violations 0。Git 回滚锚点为
  `rollback/pre-turn-activity-20260814-9dfc334d`；镜像回滚标签为
  `chat_ds-backend:rollback-pre-32a21542`、`chat_ds-frontend:rollback-pre-32a21542`、
  `chat_ds-claude-runner-supervisor:rollback-pre-32a21542`、
  `chat_ds-claude-runner:rollback-pre-32a21542`，中间 Runner 版本另保留
  `chat_ds-claude-runner:deploy-32a21542`，最终版本保留 `chat_ds-claude-runner:deploy-9dc2b69f`。
- 最终生产 Backend、Frontend、Supervisor image 分别为
  `sha256:52921a0687e46ef2c05dd486002492249e9a5dcfa5281e32d9bfcbe1810b8bf4`、
  `sha256:3f9e33d9ea0002e5a5aef8c87f4bf8f806dfc7eaec76a6eb41f18922c10bb39c`、
  `sha256:8b996b0c0066a0397e730418a6188c78760ca01d01bc4ad3a837b609a3d47526`，revision label
  精确为 `32a21542...`；Runner 为
  `sha256:3b172c1b8b0c00a0e9aa037249b5d34b73fe911ed4d53484e205db054370ed1a`，revision 精确为
  `9dc2b69f...`。Runner 内原生 Claude Code 仍为 2.1.152，二进制 SHA-256 与部署前相同：
  `5155bdca27f754aba0d2fe2f80336f5fd4793224561c234a723f0ccef654a8e8`。四容器均 running、
  restart 0，Backend/Supervisor healthy；`127.0.0.1`、`10.10.132.126`、`172.30.100.128`
  三入口 `/api/health` 均 200，部署后严重日志 0。
- 最终真实轻量 E2E 使用全新临时 Session、空 Skill、`deepseek_v4_pro` 和固定纯文本请求；新 Session
  默认权限为 `workspace_write`。SSE 唯一终态为 `succeeded/end_turn`，termination source 为
  `upstream_claude_code_completed`；持久 Run 为 `succeeded`，assistant Message 带精确 `run_id`，
  七个 durable activity events 覆盖 workflow/progress/content/projection，最后以
  `projection.status=committed` 收口。临时 Session 随后通过正常删除 API 清理，数据库残留、动态 Turn
  容器、nonterminal Run、active Engine Session 和 enabled schedule 均为 0。

## 2026-08-13 ClaudeCodeEngine 薄适配边界

- 用户再次明确架构边界：ChatDS 的 ClaudeCodeEngine 只承担浏览器/Backend 与未修改的原生
  Claude Code CLI 之间的接口，包括用户与 Session 身份、单 Workspace 挂载、模型/provider
  绑定、附件协议转换、Skill/plugin/MCP 投影、SSE 事件透传、持久化和取消/清理。Claude 的
  规划、工具循环、子 Agent、上下文压缩和 provider retry 由原生 Claude Code 负责；后续不得
  为某个模型、图片、Session 或 Skill 新建第二套 agent loop、retry/compaction 状态机或控制性
  prompt。安全挂载、签名出网、凭据隔离和 durable terminal 属于多用户 Web 外壳，仍须保留。
- 生产 Claude Code 内核没有被修改。Runner 从固定 npm 平台包安装原生
  `@anthropic-ai/claude-code` 2.1.152 二进制，当前 `/usr/local/bin/claude --version` 仍为
  `2.1.152 (Claude Code)`；本地独立 `claude-code/` 仓库只作源码参考，不进入生产构建。
  `23eea5c5` 只使用官方 CLI 已公开的 `--input-format stream-json`，把浏览器图片降低为 native
  top-level image content block；没有 patch、替换或重编译 Claude 二进制。
- conversation `3984e69bb77a452f8172bc1ca479048a` 的三源证据：持久对话是 Kimi K3 的单张图片
  OCR 请求；exact immutable Skill view 的 `skills`、primary selection 和 artifact contracts 均为空；
  原生 ledger 中 Claude 先执行 2 次 Bash 和 1 次 Read，随后自行发出 10 个 `system/api_retry`，
  最终 result 包含 `ECONNRESET` 并以 exit 1 结束。ChatDS 没有发起这 10 次 retry，也没有重放
  Turn。该现象仍是 Kimi Anthropic 兼容面与 Claude 原生 Read/tool-result image 路径的组合问题，
  不是 Skill、ChatDS retry、compaction、SSE 或 artifact gate。
- 当前薄化改动删除了附件 receipt 在用户 prompt 中的 XML/“不要 Read”控制性说明。receipt 仍在
  control plane 做 path/digest/Session 复验，图片 bytes 仍只在隔离 Turn 内通过官方 stream-json
  顶层 block 传给 Claude；Claude 是否再次使用 Bash/Read、如何重试完全保持原生行为。冻结的本地
  参考 commit 仍为 `6f6f12b37f529488b10e53928dd5508bb93535c7`，其
  `src/main.tsx:getInputPrompt` 和 `src/screens/REPL.tsx` 也把 complex image content 直接交给
  native query，而不追加 controller 工具选择提示。
- 功能提交为 `f43a3740f2a45e1c607ee63664f5b6cd5006a10f refactor: keep Claude engine
  adaptation thin`。Claude Runner/Supervisor 全套为 `93 passed, 1 skipped, 14 subtests passed`；
  图片 lowering 三项聚焦为 `3 passed`，compile/diff/secret/protected-deletion 检查通过。候选来自
  exact clean archive `/tmp/chat_ds_deploy_f43a3740.ciiCi5`，Runner 最终 ENTRYPOINT 的只读、无网、
  drop-all-capabilities 自检通过，并真实握手 4 个 MCP 及 4 个滚动兼容入口。没有运行模型重型 E2E。
- 切换前 nonterminal AgentRun、active Engine Session 和 running schedule 均为 0；只重建 Runner
  anchor 与 Supervisor，Backend、Frontend、Legacy Harness、数据库和其他组件均未重建。生产 Runner
  image 为 `sha256:416d2c1a15a9e71eb514ebfc469b3bf5522b8ac736c8fbdb8cc561fc7bb4a62c`，
  Supervisor 为 `sha256:7e31c4e353cec198c1e2357e5d8e5c0e2e2c8dc2b6faaf1df27ba9951d354a70`，
  revision 均精确为 `f43a3740...`；两者 restart 0，Supervisor healthy，Claude version 仍为
  2.1.152。`127.0.0.1`、`10.10.132.126`、`172.30.100.128` 三入口 `/api/health` 均 200，
  SQLite quick/FK 正常且生产仍空闲，Supervisor 严重日志 0。旧镜像保留
  `rollback-pre-f43a3740`。

## 2026-08-13 Claude 原生多模态输入与 Kimi 兼容性闭环

- 本轮按三源要求诊断 conversation `c6eeb0a8c672495cb8ee084709169ebf`。持久对话是无
  Skill 的普通图片转 Markdown 请求；exact immutable Skill view 中 Skills、primary selection 和
  artifact contract 均为空。Runner 原始时间线显示 Claude `Read` 受 2000×2000 限制后把
  1206×2622 图像切为两段；每次 Read tool-result 携带大块嵌套 base64 图片，上游随后分别
  发生 429 和 `ECONNRESET`，最终被折叠为 `runner_exit_nonzero`。累计 cache/input 超过一百万
  token 来自多次 retry 计费汇总，不是单次超出 Kimi 上下文；签名出网收据没有预算拒绝，
  Backend/SSE 也没有先于上游终止。
- 确定性 Provider 复现证明：直接 user image 请求正常；同类图片放入嵌套 tool-result 时被
  Shaiengine Kimi Anthropic facade 放大为数万 input tokens。因此根因是 Claude `Read`→tool-result
  的间接图片回输与该 Provider 兼容层组合不稳定，不是 Kimi 纯文本接入、图片能力、
  Session workspace、Skill、网络白名单或前端问题。跨 Provider 不变量重述为：已验证的
  Session-scoped 输入图片必须在首次 native user message 中作为顶层 image content block 传入，
  不应强迫模型先 Read 后把同一输入图片回填成 tool-result。
- 成熟实现对照冻结本地独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用其 `src/main.tsx` 支持的
  `--input-format stream-json` 与 SDK user message 顶层 content blocks；继续适配 ChatDS 现有
  Session 单 workspace mount、typed attachment receipt、digest 复验、长期 mutation lease 和 durable
  request redaction。拒绝为 Kimi、Session ID、图片尺寸或任何 Skill 增加分支，也拒绝新建
  prompt compiler、Backend retry 状态机或 Provider 错误分类层。本地相关路径完整，无需 Web 补证。
- 最终生产改动仅是：Runner 用 `stream-json` 发送原 prompt text，并在已有 PID 1 收据/文件
  复验后将 manifest 内图片追加为顶层 base64 image block；Supervisor prompt 不再要求模型
  为加载输入图片单独调用 Read。base64 只在隔离 Turn 进程内短暂存在，不进 Backend、
  Supervisor durable request 或 debug；纯文本也统一走 Claude 原生 stream-json user message。
- 修复提交为 `23eea5c57d98e03b843928cec1c9e59d71c3ec28 fix: pass images through Claude
  native input`，共 5 个文件、新增 128/删除 10 行；生产代码净新增 61 行，其余为自检和
  通用 rename/cross-domain holdout。Runner/Supervisor 专项 `66 passed`，py_compile、diff check、
  fixture/genericity scan 通过。clean archive `/tmp/chat_ds_deploy_23eea5c5.qehU1J` 与 Git tree
  均为 22,518 个文件；候选 Runner 在 read-only/network-none/cap-drop/tmpfs 的真实生产
  ENTRYPOINT 自检通过。
- 切换前 nonterminal AgentRun、active Engine Session、running ScheduledJobRun 均为 0；没有
  数据库 schema/data 修改。仅重建 Runner anchor 和 Supervisor，Backend、Frontend、Scheduler、
  Legacy Harness、Proxy 和数据库均未重建。当前 Runner image 为
  `sha256:2b2810bfb104091f57142f7c1c8ef1b1338ebe65918676d4957ed3caa0be876b`，Supervisor
  为 `sha256:514faa3c109dbeb4b749ae8571fa91278a60438b9260e224c099289a6f76b264`，revision 都精确
  为 `23eea5c5...`，旧镜像保留 `rollback-pre-23eea5c5`。两容器 restart=0，Supervisor
  healthy；`127.0.0.1`、`10.10.132.126`、`172.30.100.128` 三入口 `/api/health` 均
  200，生产 SQLite quick/FK 正常且仍无 active run/session。未自动重跑用户原图片或
  V2.3 E2E；用户可在新 Turn 用 Kimi 重测原类型图片请求。

## 2026-08-13 Claude 多模态输入的 Session 文件投影与执行闭环

- 本轮按三源要求诊断 conversation `3df46ebc307943f498e8724f268d1b0b`。持久化消息中的图片是有效
  JPEG（约 251 KB、1206×2622），前端请求和数据库均完整；该 Turn 的 exact immutable Skill view
  没有 Skills、primary selection 或 artifact contract，因此不是 Skill 路由/指令问题。Runner debug
  显示旧 Backend 只把图片变成文本占位符，Session workspace 中没有对应文件；Claude 随后发出两个
  空参数 `Read` 并 `Glob` 到空目录。故根因是 Backend→Claude Runner 的多模态 lowering 缺失，不是
  图片损坏、模型上下文、网络、Provider 或前端上传失败。
- 通用不变量现为：浏览器/DB 的 data URL 只属于输入传输和历史渲染；Backend 在持有当前 Session
  workspace mutation lease 时，先完整校验数量、总量、格式 magic、MIME、尺寸和像素边界，再把图片
  原子发布为 `.chatds/input-attachments/<sha256>.<ext>`，并把消息降低为 typed
  `chatds.input-attachment.v1` receipt。原始 bytes/base64 不进入 Backend→Supervisor 请求、Runner
  durable request 或 debug。重复输入内容寻址去重；远程 URL、畸形 base64、MIME 伪装、symlink 逃逸、
  digest 冲突和跨 Session 路径均 fail closed。当前新 Turn 携带图片而模型不具备 `is_multimodal`
  能力时，在创建 AgentRun 前返回明确 400；Claude engine/UI capability 现在显式包含 `vision`。
- Supervisor 在 prompt 编译前把 receipt、lowered message 与 exact Session workspace 重验；只渲染一个
  明确 `/workspace/...` 的 `Read` 目标，旧的伪占位符成为稳定的
  `input_attachment_transport_unlowered` 错误。PID 1 在取得整个 Turn 的长期 Session mutation lease 后、
  执行 Claude 前再次核对路径、regular-file、size 和 SHA-256，关闭 preflight/execute TOCTOU 窗口；
  附件目录在可写 workspace 下以只读 child bind 重新挂载。文件为 `0444`，确保 root Backend 发布后
  Claude 的 UID/GID 65529 worker 可读但不可写；真实 Docker worker smoke 已验证读取成功。
- 成熟实现对照冻结独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/utils/attachments.ts` 对图片构造真实 base64 content block，以及
  `src/tools/FileReadTool/FileReadTool.ts` 把图片作为真实 Read tool-result block 返回的原则；ChatDS 将
  二进制投影为 Session-scoped immutable file，以保留既有 one-workspace mount、authority、receipt 和
  debug redaction 合同。拒绝继续输出“图片在 workspace”但没有文件的占位符，也拒绝把 data URL 塞进
  durable control plane。本地参考路径完整，无需 Web 搜索补足 stub。
- 镜像候选验证同时暴露一个既有部署闭包缺口：当前 Supervisor source 会导入共享 schedule parser，
  但旧 `Dockerfile.supervisor` 没复制该模块/安装锁定 parser；生产未暴露仅因为 Supervisor 仍停在
  `e8480032` 老镜像。本轮按“运行时源码的全部 transitive dependency 必须在 immutable image 中闭包”
  的通用不变量，复制同一 `backend/schedule_spec.py` 并锁定 `croniter==6.2.4`，增加源码级闭包回归和
  真实候选 import smoke；没有修改调度语义。
- 最终 Backend 全套 `317 passed`；Claude Runner/Supervisor 全套 `93 passed, 1 skipped`；相关附件/
  engine 合同 `58 passed`。compile、`git diff --check`、secret/fixture scan、三候选 image import/self-test、
  非 root worker 文件读取及 nested read-only mount smoke 全部通过。生产受控视觉 E2E 使用全新临时
  Session、空 Skill view、Qwen 多模态模型和原问题中的有效图片字节；原生事件共 215 个，机器证据同时
  包含精确附件路径的 `Read`、对应 tool result 和唯一 `succeeded` Supervisor terminal。测试 Session、
  Runner state 和 workspace 随后全部清理，没有篡改原 conversation 或自动运行模型重型 V2.3 E2E。
- 功能提交为 `09234289b29a2dfa469468e1bc362ee26113e514 fix: deliver image inputs to
  isolated Claude turns`。clean archive `/tmp/chat_ds_deploy_09234289.DuGDAA` 与 Git tree 均为
  22,518 files。切换前连续确认 nonterminal AgentRun、active Engine Session、running
  ScheduledJobRun 和动态 Claude Turn container 均为 0。SQLite online backup volume
  `chat_ds_chat_ds_db_backup_pre_09234289_20260813_103443` 为 517,136,384 bytes，SHA-256
  `1112fc11d7776fff9d6671bfa264d3119bda5fb89c1a4dadef11384312c6ab73`，quick/FK 正常。
- 当前生产 Backend/Supervisor/Runner image 分别为
  `sha256:77cf058579b7e4bb9e5f89b7cd191b7b5744daafe689df3cd5ff65955789c89c`、
  `sha256:28a14ef77eaeb750705df3f6d11e04c179e3f98612b5911d7f041c1ca185f95c`、
  `sha256:813ec6561b9e674410b548f963ac95bd57ba778d2e64ebd63ba14bde5e7caf27`，revision label 均精确为
  `09234289...`；旧镜像保留 `rollback-pre-09234289`。只重建这三个组件，Frontend、Scheduler、
  Legacy Harness、数据库和其他容器未改。三组件 running/healthy（inert Runner anchor 无 healthcheck）、
  restart 0，`127.0.0.1`、`10.10.132.126`、`172.30.100.128` 三入口 `/api/health` 均 200，
  生产 SQLite quick/FK、active run/session 和严重启动日志均正常/为 0。用户现在可在原 Session 重新发送
  带图请求；历史失败消息不会被重写。

## 2026-08-12 后台消息可见性、前端同步生命周期与部署版本闭环

- 本轮继续按三源要求复核 session `92609477b43645a383b93963df75d28e`。用户最新明确请求为每 2 分钟
  查询一次公开数字资产价格、共 5 次；新 ScheduledJob
  `1e2c9e9a1ed4ecf636651556b977db45` 已 `run_count=5/max_runs=5`、`last_status=succeeded`、
  `enabled=false`，五个 cron root 均为 durable `succeeded/end_turn`，对应五条 assistant 报告已在
  09:58--10:06 UTC 持久化。chat root 的 exact immutable Skill-view digest 为
  `a689f3ba8a8bd62f28ca09c65c22065a5540ac5d3b0b87af27a3eae82aec8e45`，cron root 为
  `39ee440000ef2a0d6535620782a3c6e87cd0d986425003a5983377fd0b51af5f`；两者均无 Skill、primary
  selection 或 artifact contract。因此 Scheduler、ClaudeEngine、Provider、网络、Skill、AgentRun
  终态和消息落库均正常，本轮只涉及浏览器投影可见性。
- Frontend Nginx access log 证明 09:57 Turn 结束后，部署前已打开的旧 SPA 到 10:05 之间没有任何
  run-card/message 请求；10:05 手工刷新才加载当时的新入口 `/assets/index-DpeAHPNp.js`，随后 5 秒
  run-card 与 30 秒全量对账持续运行。第 5 条报告 10:06:27 落库，浏览器 10:06:30 已取到增长后的
  message response。故第一段“不刷新无消息”来自存量页面仍运行旧 bundle；第二段“已取回但看不到”
  来自滚动算法用 append 后的距离判断，后台一次追加 cron trigger+report 超过 300px 后错误地把原本贴底
  的页面当成用户主动上滚。
- 通用前端不变量现为：当前 Session 的 durable projection heartbeat 独立于一次 React render/live SSE，
  skipped/failed read 后必须重武装；SSE ownership 释放后，以及 focus/online/pageshow/visible 时立即强制
  全量对账；隐藏页面不再彻底断掉观察，而以 30 秒低频保持同步。所有读仍经既有 coordinator 串行/coalesce，
  route ownership 与 live draft 防污染不变。滚动使用 append 前的 pinned intent：原本贴底或正在 streaming
  才自动跟随，用户主动上滚则只显示“到底部”按钮。
- 前端部署生命周期也已闭环。Vite 每次构建从真实 entry chunk 生成 `/build-info.json`；运行页面每 60 秒及
  focus/online/pageshow/visible 时以 `no-store` 读取并和当前 script fingerprint 比较。发现新版本后，仅在页面
  visible、无 live/durable/unknown Turn、无未发送文本/图片且焦点不在编辑控件时自动 reload；否则保留旧页并
  等下一次安全检查。Nginx 对 SPA shell/build-info 明确 `Cache-Control: no-store`，fingerprinted assets 仍
  immutable 一年。此次部署前已打开且不含 version guard 的历史页面技术上仍需最后刷新一次；加载本版后，
  后续部署不再要求人工发现并刷新。
- 成熟实现对照冻结独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/remote/SessionsWebSocket.ts` 的显式连接生命周期、wake/reconnect、keepalive 与临时失败不永久停机，
  以及 `src/state/AppStateStore.ts` 的 typed reconnecting/disconnected 状态思想；ChatDS 当前已有 authenticated
  durable REST projection，因此本轮把该模式适配成可重武装 heartbeat+权威对账，没有为单个 Session、cron、
  行情或 Skill 引入 WebSocket/后端特判，也没有改动 Harness。相关本地源码完整，无需 Web 搜索。
- deterministic regression 覆盖跨域 inventory/factory/sensor 投影、临时失败后 rearm、wake 强制全量、route
  revoke、重叠请求 coalesce、pre-append scroll intent、build fingerprint、cache bypass 与 safe reload gate。
  Frontend `36 passed`，本轮全部变更文件 ESLint 通过，production build 生成
  `/assets/index-Bk0M6hUf.js`；全仓 ESLint 仍只有既有 `ModelSelector.jsx`、`SkillLibrary.jsx` 两个
  `set-state-in-effect` 错误，未借本轮扩大修改范围。candidate Nginx smoke 验证 build-info/任意 SPA route
  `no-store`、fingerprinted asset immutable，`git diff --check` 通过。
- 功能提交为 `3810abad9b70b8011088fccf3c538c9f10745f4c fix: keep durable session updates live in
  the frontend`。clean archive `/tmp/chat_ds_deploy_3810abad.hmGWA0` 与 Git tree 均为 22,515 files。
  仅 Frontend 被 force-recreate；Backend、Scheduler、Claude Runner/Supervisor、Legacy Harness、数据库和
  其他容器未重建。当前 Frontend image 为
  `sha256:6bbcf864b9264c0d897dedce2d07f80fcb525dd83c0dbac218d3c10f8ddc94a7`，revision label 精确
  匹配功能提交，旧镜像保留 `chat_ds-frontend:rollback-pre-3810abad`。切换时 nonterminal AgentRun 与
  running ScheduledJobRun 均为 0；当前容器 running/restart=0，`127.0.0.1`、`10.10.132.126`、
  `172.30.100.128` 三入口页面、`build-info.json` 和 `/api/health` 均为 200 且返回相同 entry fingerprint。

## 2026-08-12 调度规格单一权威与工具边界可恢复校验闭环

- 本轮按三源要求诊断 session `92609477b43645a383b93963df75d28e` 的最新失败。持久对话中用户要求
  每 2 分钟查询一次公开数字资产价格、共 5 次；root AgentRun
  `cc5f5c56f01f4483bbfed652f4ab7e40` 的 Claude 原生执行、Bash 行情查询、result、checkpoint 和
  Supervisor terminal 都成功，但 application terminal projection 以
  `schedule_control_no_occurrence_before_expiry` fail closed。exact immutable Skill-view digest 为
  `a689f3ba8a8bd62f28ca09c65c22065a5540ac5d3b0b87af27a3eae82aec8e45`，manifest 中 Skills、
  primary selection 和 artifact contracts 均为空，只装配了
  Harness 自有 `schedule_control`、`web_search`、`market_quote`，故不是 Skill、网络、Provider、行情
  上游、文件沙箱或前端问题。
- Debug/tool 时间线显示模型在 09:14--09:15 UTC 调用 `schedule_create` 时提交
  `*/2 * * * *`、`max_runs=5`，却把 `expires_at` 写成已经过去约 20 分钟的
  `2026-08-12T08:55:00Z`。旧 MCP 只检查 expiry 是非空字符串，错误返回
  `accepted_pending_terminal_commit`；Runner 因而形成 pending control write。Backend 在唯一权威终端
  事务中重新计算首个时点并正确拒绝，导致模型已经输出“已创建”后整轮才失败。历史 root 保留
  `failed/terminal_projection_failed`；失败事务没有创建 ScheduledJob。该 Session 当前
  `active_run_id=NULL`，已有的两个旧 job 都是用户此前明确创建且已达到各自 `max_runs` 的完成记录，
  没有第三个或 enabled job。
- 通用不变量现为：schedule expression、IANA timezone、expiry normalization、首个 eligible occurrence
  与 occurrence-before-expiry 必须由一个纯控制面模块解释；MCP 在模型仍可纠正参数的 tool-call 边界同步
  校验并用 `chatds.schedule.rejected.v1`/稳定错误码返回，只有语义有效的请求才能产生 accepted receipt；
  Runner 只把匹配 accepted hash 的成功结果变成 pending write；Backend HTTP create/update 和 root terminal
  commit 使用同一源码再次校验，后者继续作为 TOCTOU/幂等防线。`max_runs` 已足够表达纯次数边界时，
  工具说明要求不再凭空添加 expiry；用户明确给出时钟边界时才同时使用 expiry。直接 API 更新不再把
  不可能的 schedule 静默改为 disabled，也仍允许显式关闭一个已过期 job。
- 单一实现位于 `backend/schedule_spec.py`，Backend 直接导入；Runner 镜像从同一 Git 文件复制，并在
  immutable build stage 以哈希锁定 `croniter==6.2.4`。运行镜像仍无 pip/npm/apt，不存在动态安装、第二
  沙箱或网络权限变化。跨域 deterministic regression 使用 factory、warehouse、inventory 和 renamed
  sensor 场景，覆盖已经过期、expiry 虽在未来但早于下一 cron、边界相等、零 duration、naive expiry、
  typed MCP rejection、rejected result 不形成 pending write、terminal 二次拒绝、API update fail-fast 与
  关闭 expired job；没有 Session、资产、Skill、疾病、文件名或固定 workflow 分支。
- 成熟实现对照冻结本地独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/utils/cronTasks.ts` 的“写入前 validate、读取时 revalidate”、`src/utils/cron.ts` 的单一
  parse/next-run 语义，以及 `src/utils/cronScheduler.ts` 的 typed next-fire/in-flight ownership；ChatDS
  将其置于既有 Session authority、receipt hash、terminal transaction 与 durable Scheduler 之后。拒绝
  只在 Backend terminal 加 Session 特判、静默删掉错误 expiry、接受模型 prose 为控制状态或以 native
  process cron 取代持久 Scheduler。本地参考代码完整，无需 Web 搜索补足 stub。
- 回归结果：Backend 全套 `304 passed`；Claude Runner/Supervisor 全套
  `87 passed, 1 skipped, 14 subtests passed`；候选与生产 Runner 的真实 `python -I` image self-test、
  module/compatibility MCP entrypoint、跨时区有效请求和 stale-expiry typed rejection 均通过；Backend
  候选镜像使用精确 parser 版本并通过 semantic smoke。compileall、diff check、secret/genericity scan、
  clean archive 文件计数均通过，没有启动模型重型 Skill E2E。
- 功能提交为 `3cf5d06ffee90aee9e3a73c54d2ebac35eede0fc fix: validate schedule effects
  before acceptance`。clean archive `/tmp/chat_ds_deploy_3cf5d06f.jxdQsJ` 与 Git tree 均为 22,513
  files。切换前 nonterminal AgentRun、active Engine Session、running ScheduledJobRun 和动态 Claude Turn
  container 均为 0。SQLite online backup volume
  `chat_ds_db_backup_pre_3cf5d06f_20260812_174747` 为 464,461,824 bytes，SHA-256
  `4412207210b37248a66181068c07f585090350f558271ef99bf4145236504781`，quick/FK 正常。
- 当前生产 Backend/Runner image 分别为
  `sha256:29b188a33cb38337490764170082bdd9faf109203ee2fdd1f88f30b03b953f66`、
  `sha256:56ec4f1befab84ba7af333e0468ad3840443e94f2cef0892ebec8bb8e13958c2`，revision label 精确
  为 `3cf5d06f...`；旧镜像保留 `rollback-pre-3cf5d06f`。只重建了 Backend 与 inert Runner anchor；
  Frontend、Claude Supervisor、Legacy Harness、Scheduler、数据库及其他服务未改。Backend healthy、
  restart=0，Supervisor healthy、restart=0，`127.0.0.1`、`10.10.132.126`、
  `172.30.100.128` 三入口 `/api/health` 均为 200，生产 SQLite quick/FK 正常。历史失败不重写、不自动
  改变原请求时间；用户现在可在原 Session 重新发送该调度请求，新 Turn 会在工具调用现场纠正任何不一致
  的时钟边界，而不会等到终端才失败。

## 2026-08-12 后台会话消息前端重同步与成功终态去噪闭环

- 本轮按三源要求诊断 session `92609477b43645a383b93963df75d28e`。持久对话中用户先后两次要求每
  5 分钟报告数字资产价格、各 10 次；两个 ScheduledJob
  `7e2490162b674775975e9d051738fa9d`、`ac5adca2ca9bb66fc6d07ff3f41aef79` 均已
  `run_count=10/max_runs=10`、`last_status=succeeded`、`enabled=false`。生产消息共 52 条，其中
  20 条为 `source=cron` 的 assistant 报告；AgentRun/run-card 无活动 root。exact immutable Skill-view
  digest 仍为 `1bd74217f15a79587cd16cfee5f021b6a5094d5c46a4455d489781984c1e7521`，Skill、primary
  selection 与 artifact contract 均为空。因此后台执行、ClaudeEngine、Provider、网络、Skill、Scheduler
  和消息持久化都没有失败；两倍报告来自用户第二次明确创建同一有界任务，不是单个 job 越界。
- 用户观察到的第一项问题发生在 UI 生命周期：原前端只在路由进入、当前交互 SSE 结束或已知 active
  AgentRun 时读取消息。后台 job 在页面空闲后创建的 durable message 不会启动该 poll，故 DB 已有结果但
  浏览器要等刷新或下一次交互。按用户要求保持 Harness/Backend/Runner/Scheduler/数据库不变，前端现在用
  一个通用、可见页面限定的 Session 投影同步器读取既有 authenticated read API：空闲时每 5 秒先读取
  bounded run-card；仅在新 root、active/terminal/assistant mapping 边界变化时读取完整消息；另有 30 秒
  全量兜底。窗口 focus、网络 online、页面重新 visible 会立即强制全量校准。所有触发共用不可重叠的
  coordinator；在途请求只合并为一次 pending refresh，并保留 force-full 意图；会话切换、组件卸载或
  live SSE draft 会撤销投影写权限，旧响应不能污染新 Session。DB/API 投影仍是唯一权威，轮询不是新的
  workflow state。
- 第二项是纯前端投影噪声：无 Backend-proven assistant mapping 的每个普通 succeeded root 曾生成一个
  空 assistant placeholder，唯一正文即“任务已完成；以下为持久化的执行记录。”；多个 cron root 因而
  堆积相同卡片。普通成功 root 现在只保留在 Tasks/run audit 视图，不再制造聊天 turn；active、failed、
  cancelled、degraded、recovered 等需要用户关注的状态仍保留生命周期占位。生产真实目标 Session 用新
  hydration 零模型验收为 `persisted_messages=52`、`projected_messages=52`、`roots=26`、
  `assistant_cron_messages=20`、`lifecycle_notices=0`、`ordinary_success_notices=0`。
- 成熟实现对照冻结独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/hooks/useTasksV2.ts` 的单一共享 watcher、进程内通知与有界 fallback poll、不可见/完成后去噪思想；
  ChatDS 浏览器没有文件 watcher，故用现有 authenticated DB projection API 作为跨进程/重启兜底。拒绝
  把 ephemeral browser notification 或模型 prose 变成状态权威，也拒绝为 cron、行情、Session ID 或
  Skill 增加特判。本地源码完整，本轮无需 Web 搜索。
- 跨域 deterministic regression 使用 factory line、inventory 与 sensor 场景，覆盖消息边界 revision、
  tool progress 不重复下载 transcript、重叠请求 coalesce、在途 force-full 保留、route ownership 撤销、
  普通成功去噪及异常终态可见。Frontend 全套 `29 passed`，目标 ESLint 零错误，production build 通过；
  全仓 ESLint 仍有 `ModelSelector.jsx` 与 `SkillLibrary.jsx` 两个既有 `set-state-in-effect` 错误，本轮没有
  扩 scope 修改。`git diff --check` 通过。
- 功能提交为 `18961e04e9a34d2dee6284dc975618c9e40d07e8 fix: reconcile durable session
  updates in chat`。clean archive `/tmp/chat_ds_deploy_18961e04.My2zXb7W` 与 Git tree 均为 22,509
  files；Frontend image 为
  `sha256:7b87ef91942fbfb6ec79076d33b7f79c02168d679a66855a97b7c7dc51bdb798`，revision label 精确
  匹配提交，旧镜像保留 `chat_ds-frontend:rollback-pre-18961e04`。仅 Frontend 被 force-recreate；
  Backend、Legacy Harness、Claude Supervisor/Runner、Scheduler、数据库和其他容器未重建。当前 Frontend
  running/restart=0，`127.0.0.1`、`10.10.132.126`、`172.30.100.128` 三入口页面与 `/api/health`
  均为 200，三入口均加载 `/assets/index-DpeAHPNp.js`。

## 2026-08-12 Claude 控制写入能力编译与终态投影失败收敛闭环

- 本轮按约定完整关联 session `92609477b43645a383b93963df75d28e` 的三类证据。持久对话最后一条
  用户请求是通过公开行情接口每 5 分钟报告一次所选数字资产价格、共 10 次，最后没有 assistant 行；
  root AgentRun `fdf16e79b12c4510bc12c492b28c28f2` 长期停在 `committing /
  terminal_projection_pending`，Engine Session 仍错误保留 `active_run_id`，且该 Conversation 没有任何
  ScheduledJob。Runner/Supervisor 原始事件却有唯一成功 native result、checkpoint、零 pending native
  task、成功 authoritative `run.completed` 和一条 pending schedule control write。Backend 精确异常是
  `stage_schedule_control_writes -> schedule_control_tools_unknown`，随后 `_persist_after_stream` 只返回 false，
  没有把 `committing` 收敛为失败终态。故障不是 Provider、网络、行情上游或模型执行失败。
- exact immutable Skill-view digest 为
  `1bd74217f15a79587cd16cfee5f021b6a5094d5c46a4455d489781984c1e7521`；manifest 的
  `skills`、`selected_primary_skill_names`、`artifact_contracts` 都为空，只编译了 Harness 自有的
  `schedule_control`、`web_search`、`market_quote` 能力。因此这不是 Skill 路由/指令遵循问题。触发器是
  模型在 `enabled_tools` 返回 Claude 可见的 `Bash` 与 MCP 全限定名，而 Backend 持久控制面只接受稳定
  ChatDS capability ID；之前 Skill-view、MCP schema、Runner receipt 和 Backend 各自猜测名字，没有一份
  内容寻址的共享编译产物。
- 通用不变量现为：持久任务只保存跨引擎稳定 capability ID，Skill-view 编译并固化 exact
  model-visible→canonical map；Schedule MCP schema、MCP receipt、Runner ledger 和 Backend commit 使用
  同一映射；只允许平台拥有的 exact alias，伪造/外部 MCP 前缀不得按后缀获得能力；当前 Conversation
  authority 是上限；`NULL`（controller omitted）和显式 `[]`（无额外能力）严格区分，空集合不得按
  truthiness 扩张为 unattended defaults。Claude 的 `Bash/Read/Write` 等内建工具是每个 native Turn 的
  ambient runtime surface，不作为 ChatDS capability grant 持久化。
- root engine terminal 现在只是 application commit candidate。assistant row、usage、controller effect 与
  AgentRun lifecycle 的最终事务若失败，会用独立、幂等、无原始异常泄露的事务落
  `run.projection_failed` 诊断，把 root/task/Engine Session 收敛为
  `failed/terminal_projection_failed`、释放 `active_run_id` 并补一条明确失败 assistant 消息；公开 SSE
  terminal 会重新读取 durable AgentRun，不能继续显示过期的 native success。重启遇到
  `committing + durable engine terminal` 也不再冒充成功或重放不确定副作用，而是
  `terminal_projection_interrupted` fail-closed、保留唯一原始 terminal 并解锁 Session。该失败终态不会
  发布 native checkpoint；普通执行成功和 transcript-only 既有语义不变。
- 成熟实现对照冻结独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/utils/cronTasks.ts` 的写边界 validate/normalize、`src/Task.ts` 的显式 typed terminal，以及
  `src/utils/cronScheduler.ts` 对 scheduler ownership/in-flight 与持久状态的区分；把这些模式置于 ChatDS
  现有 Session authority、DB pending-write receipt 和 exactly-one terminal 合同后。拒绝把 Claude
  display/MCP transport 名直接变成长期 DB 身份，也拒绝用进程内 native cron 取代 Backend durable
  scheduler。本地相关源码完整，无需 Web 搜索补足 stub。
- 确定性回归包含 factory sensor/renamed public metric 等非业务 holdout，覆盖 alias rename、MCP receipt
  与 Runner hash 同源、外部前缀伪造、Conversation 权限扩张、显式空权限、投影异常释放 Session、重启
  中断不误报成功和 schedule 运行期不默认扩权。Backend 最终全套 `297 passed`；Claude
  Runner/Supervisor `85 passed, 1 skipped, 14 subtests passed`；相关组合另为 `83 passed`。compile、
  diff check、secret/genericity scan、clean archive 文件计数与三个候选真实镜像 smoke 均通过。没有运行
  V2.3 或其他模型重型 E2E。
- 功能提交为 `e8480032094bfcf1de669e1b27836e84a0155e8c fix: make terminal control writes
  converge`。clean archive `/tmp/chat_ds_deploy_.v8DBvaRF` 与 Git tree 均为 22,507 files。切换前唯一
  active root 就是上述已完成 native execution、但 application commit 卡住的目标 run；没有 running
  ScheduledJobRun 或动态 Turn container。SQLite online backup volume 为
  `chat_ds_db_backup_pre_e8480032_20260812_153826`，460,627,968 bytes，SHA-256
  `f3ae2ba66230530bd34084d1a5a16dcd4f20dd2a29d84530d4da2a6ccbffc1e7`，quick/FK 正常。
  当前 Backend/Runner/Supervisor image 分别为
  `sha256:8449388a3fc621bea321f7703a39a5f146c0a77112f517149b2ad0cd059c4b85`、
  `sha256:654d75226a19c1974cd64b2dab8aa59368ae8a16931d3404cfbdd69afa0d98f5`、
  `sha256:4a330744555f723f6ba35e238fc61c3b0c41aadac7c88a4d5bd478ae6619c2c3`，revision 均精确为
  `e8480032...`；旧镜像保留 `rollback-pre-e8480032`。三容器 restart 0、Backend/Supervisor healthy，
  `127.0.0.1`、`10.10.132.126`、`172.30.100.128` 的 5173 `/api/health` 均为 200。
- 启动恢复已将历史 root 明确终态化为 `failed/terminal_projection_interrupted`，补 assistant 并清除
  active Engine Session。原请求的模型生成 expiry 已因排障只剩不足 10 个触发点；没有伪造或快速补跑
  错过的历史价格，而是通过正式 internal schedule API 从当前时刻重建同一 cadence/max-runs 意图：job
  `7e2490162b674775975e9d051738fa9d`，`*/5 * * * *`、`max_runs=10`、绑定原 Conversation/模型，
  exact capability subset 为 `web_search + market_quote`。15:45 CST 首个自然 tick 已完成：
  ScheduledJobRun `600443d10755447b876c3e51052d7804=succeeded`，cron AgentRun
  `6951d26aa2204dbda0018adcc679cd89=succeeded/end_turn`，assistant cron message
  `c2bcbab352da497c9257b850594e1a5a` 已持久化。15:50 第二个自然 tick 也独立完成：
  ScheduledJobRun `23e9f0d962c74532a138a6351996e5d8=succeeded`、cron AgentRun
  `18bcfa407e0a4698be16477a8a83bdbf=succeeded/end_turn`、assistant message
  `2ab71661fb5447478dd7310cf06dd4de`。job 当前 `run_count=2`、`last_status=succeeded`、
  `consecutive_errors=0`、下一时点 15:55，证明 recurring reschedule 也成立；后续由持久 scheduler 继续
  直到达到 10 次或恢复窗口结束。

## 2026-08-12 Claude 定时 Turn 单锁、终态刷新与能力网关网络闭环

- 本轮继续诊断 session `63a312df7a1d462fac00ac0926381de3` 的“已声明 13:03–14:53
  每 10 分钟报告但没有后续消息”。三类证据完整关联：持久对话中的最终请求是 600183 生益科技与
  002636 金安国纪当日 13:00–15:00 监控；首个正式 `ScheduledJobRun`
  `c11e5f12c1414c5bbca162946bb19780` 自 13:00 起一直为 running，`last_status` 为空且
  `next_run_at` 停在 13:10，同时没有 cron AgentRun；exact immutable Skill-view 的 primary 仍是只适用
  临床试验/CDISC/监管任务的 `healthsim-trialsim` 加 18 个生物医学 supporting Skills，本轮没有 Skill
  receipt/artifact contract，证券监控与这些 Skill 无关。故障发生在模型、Skill、工具和网络之前。
- 确定性根因是 Scheduler 在 `registered_conversation_execution` 中先持有 Conversation maintenance
  lock，再调用 Claude `_chat_stream`；普通 Turn ingestion 又获取同一把非重入 `asyncio.Lock`，形成
  自锁。通用修复把 lifecycle producer 注册与 Turn lease 分开：Claude scheduled Turn 只注册 producer，
  由普通 `_chat_stream` 唯一持锁；不经过该入口的 Legacy 路径仍显式持一个 maintenance lease。跨域
  回归用任意 `session/job/model` 注入真正的 Conversation lock，修复前稳定 timeout，修复后通过；不存在
  Session、股票、Skill、V2.3、文件名或固定数量分支。
- 部署后首个恢复 run 真正产生 Claude cron AgentRun 并落 assistant 消息，但外层 Schedule 被错误记为
  failed。第二个确定性复现证明 `_chat_stream` 由 Scheduler 的 DB Session 创建 `AgentRun=running`，
  terminal projector 则在独立 Session 提交 succeeded；Scheduler 的主键 get 命中了旧 identity-map
  对象。现在外层只能在 `refresh(agent_run)` 后从 `succeeded/failed/cancelled` durable terminal 派生状态，
  其他值明确 fail closed，不能再把 stale running 映射成无错误信息的 failed。
- 同两次恢复 Turn 的 `market_quote` 在 0.2 秒内返回 HTTPError，而网关自身及代理容器内并行探针均为
  HTTP 200。机器配置证据显示能力网关同时挂载 `browser_egress` 与 `search_net`，代理 DNS 只解析到
  `172.31.0.4`，但 private DNS pin 仅授权应用内 `172.29.250.0/24`，所以代理按设计在连接上游前拒绝。
  修复没有扩大 CIDR/公网权限，而是使 typed capability broker 只在 `search_net` 暴露一个 Harness
  可见坐标；该网络本身仍向固定公共行情上游出网。静态 topology regression 防止受控私有 broker 再次
  被多网卡 DNS 坐标破坏。
- 生产中还发现同一请求存在两条 enabled job：`47d0448...` 是按用户边界建立的 typed
  `market_quote` job，`41987971...` 是此前维护操作与模型创建路径重叠后留下的 13:03 job。已通过正式
  internal schedule API 停用后者，保留前者，防止恢复后双重播报。已错过的历史时点没有伪造或补写为
  实时行情。
- 成熟实现对照继续冻结独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/utils/cronScheduler.ts` 的独立 scheduler ownership/`inFlight`、从当前时刻重排而不快速补跑，及
  `src/Task.ts` 的 typed terminal/terminal predicate；这些模式被放入 ChatDS 的 DB schedule、
  Conversation lifecycle、durable receipt 与 exactly-one terminal 合同中。拒绝把原生进程内 cron
  作为 disposable print Turn 的持久 owner，也拒绝用 timeout/retry 掩盖锁重入或 stale projection。
  本地源码路径完整，本轮无需 Web 搜索。
- 提交依次为 `82a511ceaecd34ecb8926bd6e6c5113144846c69 fix: serialize scheduled Claude
  turns once`、`e5712b9f37d3d5b03fdb9e5c75cde535cbb9bdc1 fix: refresh scheduled child
  terminal projection`、`1194cc12 fix: pin typed gateways to one network plane`。Backend 全套最终
  `290 passed`；锁/调度专项 `24 passed`；部署拓扑、market MCP 与完整 proxy 组合
  `86 passed, 122 subtests passed`；diff/compile、候选 AST/import、Compose config 均通过。
- 生产 Backend 当前 image 为
  `sha256:b7010df5eb2e9b13e363fadd1ef635a67dede3099ee40c12076ac059720a65ce`，revision
  `e5712b9f...`，running/healthy；旧镜像保留 `rollback-pre-e5712b9f`。切换前 SQLite online backup
  volume `chat_ds_db_backup_pre_82a511ce_20260812_140739` 为 457,011,200 bytes，SHA-256
  `07356dc4142a9db10828d32c76f23d6095ad17057bb0ae5f19ebf1824ee30191`，quick/FK 正常。生产
  `market-data-gateway` 现只有 `search_net` 地址 `172.29.250.6`，代理侧真实 quote probe 为 200。
- 14:30 CST 自然 tick 已完成业务验收：ScheduledJobRun
  `fc0bc2430fa945e4a243fcfc7f1c15ef=succeeded`，cron AgentRun
  `67b2e61b90e544f08270bdcf4780d623=succeeded/end_turn`，assistant cron message
  `62eab17e78f84fa0aefb24e3f5f1beb` 持久化；两次 typed quote 均成功，报告了 600183/002636 的现价、
  涨跌幅、昨收和 14:30 数据时间。保留 job 的 `run_count=4`、`next_run_at=2026-08-12 14:40 CST`、
  `last_status=succeeded`；13:00 自锁 run 已由启动恢复器明确 cancelled，14:11/14:20 消息如实保留当时
  网络拒绝结果，不改写历史。

## 2026-08-12 Claude 持久调度、计划权威与失败续接闭环

- 本轮诊断 session `63a312df7a1d462fac00ac0926381de3` 的最后两轮时完整关联了三类证据。
  持久对话中用户明确要求“生益科技和金安国纪、当日 13:00–15:00、每 10 分钟、共 12 次”；首个
  AgentRun `90240be03bbb44d291c7cbc148f6e626` 实际已写出正确脚本、成功查询 600183/002636，并调用
  native `CronCreate` 建立 13:03–14:53 的 12 个 session-only 时点和 15:03 收尾，但模型留下两项
  TaskCreate 状态未完成，Runner 因 `native_plan_tasks_pending` 把 native result/exit/checkpoint 均成功的
  Turn 判失败。随后“继续”的 AgentRun `b7aa4ea1e5d948fd8434eb975350bcd0` 没有 resume 这个失败
  checkpoint，而是回退到更早的成功 checkpoint `c1e4...`，所以恢复成了德明利旧主题并写入错误
  durable native cron。两轮均无 provider、网络、egress、工具上游或 Skill 失败。
- 两轮 exact immutable Skill-view digest 均为
  `cd77e110ddd6f405edcc4d8bc8abf1f5b0a3d8a4caf7312947d7123027b31bd1`，primary 为
  `healthsim-trialsim`，另有 18 个生物医学 supporting Skills；primary `SKILL.md` 仅适用于临床试验/
  CDISC/监管数据任务。本轮没有 Skill tool receipt，artifact contract `activated_contract_count=0`，证券
  watchdog 明确不属于该 Skill。因此修复没有修改 V2.3/疾病/证券路由，也没有添加 Skill/session/股票/
  worker/文件名或固定数量特判。
- 根因被重述为五个跨领域控制面不变量：model-owned TaskCreate/TaskUpdate 只是计划/UI 诊断，不能取代
  native task、artifact、egress 等机器收据；外层合约失败但 native result 与 checkpoint 完整时，应提交
  transcript checkpoint 而继续保留失败 outcome；一次性 `claude --print` 进程不能拥有后台定时器；
  定时控制写必须在 root terminal 事务中按 run/tool receipt 幂等提交；定时 Turn 必须复用 Conversation
  选择的 ClaudeEngine、Skill view、workspace、checkpoint 与终态投影，不能漂移到已禁用的 Legacy
  Harness。周期任务另增加 `max_runs/run_count/expires_at`，有界请求不会按年无限复发。
- Runner 不再用 pending plan item 阻断成功，但仍把计数保留在 debug terminal；完整失败 Turn 的 raw
  `result_succeeded/checkpoint_observed` 可发布 transcript-only checkpoint，Backend 启动恢复也遵循同一
  语义。Claude 原生 CronCreate/Delete/List 在 per-Turn 模式下统一禁用，历史
  `.claude/scheduled_tasks.json` 会在当前 Session 内移出 active load path 并归档。`cronjob` 能力现在编译
  为 `chatds-schedule` receipt-only MCP；成功 tool result 形成 controller-owned pending write，Backend 在
  authoritative `run.completed` 的同一事务中用 `SHA256(root_run_id,tool_call_id)` 派生 job ID，绑定当前
  user/Session 并幂等落库。失败/取消 Turn 不提交无人值守副作用。
- 成熟实现对照冻结独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/hooks/useTasksV2.ts:120-170` 中 task list 仅驱动 UI/poll 的语义；采用
  `src/utils/cronTasks.ts:180-205` 对 session-only/durable state 的区分；同时依据
  `src/cli/print.ts:2685-2720` 与 `src/utils/cronScheduler.ts:450-500` 明确的 print-mode timer `unref`/
  process-exit 行为，拒绝照搬需要常驻 REPL 的 native cron ownership。ChatDS 改用已有 DB scheduler 与
  pending-write/terminal receipt 边界承接持久权威。本地路径完整，无需 Web 搜索补足 stub。
- 确定性回归使用 factory sensor/equipment/renamed-model 等跨域 holdout，覆盖 plan 非权威、失败外层合约
  checkpoint 续接、native cron 隔离归档、12 次有界请求、无 owner/session 伪造、root terminal 写入
  幂等、max-run 即使 force 也不能越界、Claude schedule 不触达 Legacy Harness，以及四个 MCP 的真实
  镜像握手。Runner/Supervisor 全套为 `81 passed, 1 skipped, 14 subtests passed`；Backend 全套
  `289 passed`；Legacy cron control 专项 `3 passed`。compileall、diff check、Compose config、生产代码
  fixture/genericity scan、clean archive 文件计数与四镜像候选自检均通过；未启动 V2.3/模型重型 E2E。
- 代码提交为 `7603475df48ca7d4ddc51210b018d02696967438 fix: make Claude schedules durable
  and bounded`。clean archive `/tmp/chat_ds_deploy_7603475d.gvsBpT` 与 Git tree 均为 22,506 files。
  切换前 nonterminal AgentRun、active engine session、running/enabled schedule 均为 0。SQLite online backup
  volume 为 `chat_ds_db_backup_pre_7603475d_20260812_115848`，455,733,248 bytes，SHA-256
  `4e8138c7d46250e1b311e12b7ad54b100fe9fb1e4f4c482af9ef782413927e84`，quick/FK 正常。
  当前 Backend/Harness/Supervisor/Runner image 分别为
  `sha256:6cd7d4afca40c23664271dde415d81c29219db1ea07d7f383f38e88e480fd701`、
  `sha256:6def3454ae0e6830694fc657ece15485acb6b829bb36cf58de648278db25d2db`、
  `sha256:e997880492b3519be12a646878eb0700d57696dd0f5d12c2e6495ceafceb0db2`、
  `sha256:6f3f9a0355f24b62fb451a37cd45779ae941f1d55491b1f60add584779a6c63c`，revision 均精确为
  `7603475d...`；旧镜像保留 `rollback-pre-7603475d`。四容器 running/healthy（anchor 无 healthcheck）、
  restart 0，LAN 与 172.30 映射 health 200，部署后 DB quick/FK、active run/session 和严重日志均正常。
- 为修复该历史 session 已发生的 transcript 分叉，在确认无 active run 后仅清空了它的旧 native resume
  指针（对话/消息/审计记录均未删除或改写）。正式内部 schedule API 已创建 job
  `47d0448b8dfb4c5db7612e2ca396270c`：`*/10 13-14 12 8 *`、`Asia/Shanghai`、
  `max_runs=12`、`expires_at=2026-08-12T15:00:00+08:00`、仅启用 typed `market_quote`，绑定原
  Claude Conversation 与原模型；首个 next run 为 2026-08-12 13:00 CST，最后一个计划时点为 14:50。
  prompt 明确同时查询 600183/002636、禁止替换证券或递归创建 schedule。该真实时间任务尚未到首个
  firing；后续可检查 12 个 `ScheduledJobRun`/cron 消息及终态收据，不应把“已排程”表述为“12 次均已执行”。

## 2026-08-12 Claude Runner 隔离启动、镜像准入与可诊断终态闭环

- 本轮继续诊断 session `63a312df7a1d462fac00ac0926381de3` 的最新
  `runner_exited_without_terminal`，并关联了三类证据：AgentRun/debug 中 run
  `602d62...`、`a471e0...` 只有 Supervisor 合成终态，退出码为 1，之前没有
  `runtime.config`、模型、工具或网络事件；持久对话是普通行情问答；该 Session 的 exact immutable
  Skill-view 仍以 `healthsim-trialsim` 为 primary，Skill 没有在本轮激活，也没有 artifact/工具
  receipt。因此故障不属于 Skill、provider、模型、网络策略或上游，而发生在所有 Claude Turn 共用的
  Runner bootstrap 边界。
- 对生产镜像用其真实默认 ENTRYPOINT 复现：
  `python -I /app/claude-runner/runner_entrypoint.py` 在导入
  `claude_runner`/`runtime_capabilities` 时即 `ModuleNotFoundError`。根因是镜像只复制了相邻 Python
  文件，但 `-I` 按设计移除脚本目录；此前候选 smoke 覆盖了 ENTRYPOINT 并手工插入 `sys.path`，所以
  测试的是另一条启动路径。跨域不变量重述为：生产镜像必须包含可由 isolated Python 导入的自包含
  runtime；构建、Supervisor 启动和部署验收都必须执行未改写的真实 ENTRYPOINT；bootstrap 在主包
  完全无法导入时仍必须写出一个有界、机器拥有的 authoritative terminal。
- Runner 现将 `claude_runner` 安装为只读 isolated package，最终 ENTRYPOINT 为 stdlib-only
  `/opt/chatds-claude-runner/bootstrap.py`。bootstrap 仅接受精确
  `/state/control/runs/<32hex>/events.jsonl` ledger，导入/语法故障固定写
  `runner_runtime_import_failed/bootstrap_import/exit 70`，其他早期故障收敛为
  `runner_bootstrap_failed`，不泄露异常路径或原始配置。主 controller 在解析大 request 前先建立
  EventLedger；Supervisor 保留 bootstrap 已写终态并把 error/stage 投影到 status，不再用模糊的
  `runner_exited_without_terminal` 覆盖它。无终态的真实进程退出则明确区分
  `runner_process_exited_before_terminal/bootstrap_or_controller` 与容器消失。
- 新增镜像 conformance receipt `chatds.claude-runner-image-self-test.v1`。镜像构建和 Supervisor
  lifespan 都通过最终 ENTRYPOINT 在只读、`network=none`、drop-all-capabilities、NNP、有限
  CPU/内存/PID 条件下运行它；它导入完整 controller graph，校验 Claude 2.1.152，并对 process、
  web-search、market-data 三个 MCP 做真实 JSON-RPC initialize/tools-list 握手。新 Skill-view 编译为
  `python -I -m claude_runner.*`；旧已冻结 view 的三个绝对路径作为滚动兼容入口也同时握手。Supervisor
  还要求 image runtime label，receipt 缺项、额外输出或入口损坏都会在服务准入阶段 fail closed。
- 泛化回归没有 V2.3/疾病/行情/Session/Skill ID、固定报告名或 worker 数分支。确定性缺包镜像注入使用
  真实 ENTRYPOINT，验证只产生一个 stable bootstrap terminal；mutation/rename holdout 使用
  `renamed-model-holdout`、`renamed_holdout_lookup`，Backend 另验证任意 Skill 编译的三个 canonical MCP
  module entry。Claude Runner/Supervisor 全套为 `78 tests OK, 1 skipped`，Backend 引擎契约为
  `42 tests OK`；本轮 Backend 全套此前为 `283 passed`，最终兼容补充未改变 Backend 逻辑。
  compileall、diff check、Compose config、干净 Git archive 三镜像构建、生产/候选 exact ENTRYPOINT
  自检及 Supervisor Docker-API 准入均通过。未自动启动 V2.3 或其他模型重型 E2E。
- 成熟实现对照冻结独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/entrypoints/cli.tsx:28-47,292-298` 的薄 bootstrap/快速诊断/延迟主模块模式，
  `src/utils/gracefulShutdown.ts:299-429` 的顶层诊断与有界 shutdown/failsafe，以及
  `src/cli/print.ts:729-733` 的启动失败不继续执行语义；它们被置于 ChatDS 的 Session mount、verified
  Skill-view、durable ledger、authority 和 exactly-one-terminal 合同内。拒绝用 smoke 专用入口、
  `sys.path` 注入、模型 prose 或 fixture 特判掩盖真实镜像路径；相关本地源码完整，本轮无需 Web 搜索。
- 代码提交为 `1ed177f791fc495210285f4cbdc16bc24038eebe fix: make Claude runner startup
  self-verifying`。生产 Runner/Supervisor/Backend 镜像分别为
  `sha256:0b3076675c74543f4644baff95c4787490df069b06a2e745ce9cedaa4aa91d00`、
  `sha256:905e535d574fa5f8388ac7ce7c597d11648bf1f468295c992338ba2365507134`、
  `sha256:825f9f9bb7832abcfec15797845b257cb457f72a805815d50d9ec7ffb83960a3`，revision
  均为该提交；旧镜像保留 `rollback-pre-1ed177f7`。切换前 active AgentRun/Engine Session/schedule/
  dynamic Turn container 均为 0。SQLite 在线备份卷为
  `chat_ds_chat_ds_db_backup_20260812_1ed177f7`，449,863,680 bytes，SHA-256
  `22489bc7e8c725c4fbd6534eb01c6f54d745266e63996abb52da296947d51409`，quick/FK 正常。
  部署后三容器 restart 0 且 healthy/running；Supervisor 内部 health 200，`127.0.0.1`、
  `10.10.132.126`、`172.30.100.128` 的 `/api/health` 均为 200；生产 DB 仍 quick/FK 正常且 active
  run/session 为 0。用户可在原 Session 发起新 Turn 验证；历史失败记录不会被改写。

## 2026-08-12 Claude 当前能力收据与公网读取语义闭环

- 本轮诊断 session `63a312df7a1d462fac00ac0926381de3` 时按约定关联了三类证据。持久
  对话的最后三轮均在询问德明利（001309）最新价/昨收；最新 AgentRun
  `65ef0218f13e4873afc7325c6fd90664` 为 ClaudeCodeEngine succeeded/end_turn，原生 init
  明确显示 `chatds-market-data`、`chatds-web-search` 均 connected，effective tools 中存在
  `mcp__chatds-market-data__market_quote`，但模型只调用了两次 web_search，并在终答中沿用旧
  会话的“没有行情 MCP/沙箱禁外联”说法。terminal receipt 为成功、无 budget rejection；因此这
  不是 provider、代理、行情上游或 terminal lifecycle 失败，而是 durable session 中历史能力判断
  压过当前工具清单的 capability selection/grounding 缺陷。
- 该 Session 当前 immutable Skill-view digest 为
  `52beb03b17aab27a8aae1723689604624008c8fd1ce18f64f325aeae30847259`，primary Skill 为
  `healthsim-trialsim`，另有 18 个生物医学 supporting Skills。完整读取的 primary `SKILL.md`
  明确只在临床试验/CDISC/SDTM/ADaM/监管数据请求中启用；本轮没有 Skill tool receipt、没有
  artifact contract 激活，证券行情问题不属于这些 Skill。故本次也不是 Skill 错路由，更不能通过
  修改 TrialSim/V2.3 指令修复。
- 生产 `market-data-gateway` 对同一证券的真实探针成功返回
  `source=tencent_public_quote`、`last`、`previous_close`、`as_of`，两个固定上游本次均成功；现有
  `CLAUDE_PUBLIC_READ_EGRESS_ENABLED=true` 与
  `SESSION_SANDBOX_PUBLIC_READ_EGRESS_ENABLED=true` 也已启用统一公网 HTTP(S)
  `GET/HEAD`、80/443 profile。旧回答中的 `request_url_not_allowed` 来自更早 Turn 的 Bash 尝试；
  最新 Turn 没有重试 Bash，反而忽略了已经装配的 typed quote MCP。因此用户原始“通过接口读取
  价格/昨收”不需要任意 TCP 或直接 Docker 网络。
- 文件系统隔离不等于出站保密：完全开放网络仍可外传当前 Session 上传文件/产物、把数据编码进
  URL/DNS/TCP，或泄露 Claude 子进程继承的 provider credential，也会放开内网扫描/C2。生产继续
  使用唯一 session-wise sandbox、`network_mode=none` 和 signed proxy；保留已经通用开放的公网
  HTTP(S) 只读 profile，不开放任意双向 TCP、私网、未列端口或写方法。这个取舍没有域名白名单，
  可覆盖公开行情、天气、文献、新闻和任意规范 HTTP API；确需非 HTTP 协议时应新增部署拥有、可审计
  的协议 broker/显式 authority，而不是让模型选择第二个沙箱或直连网卡。
- 新增 `claude_runner/runtime_capabilities.py`：Supervisor 从已验证 Skill-view 的
  `harness_egress_rules` 与当轮签名 `egress_policy.public_read` 编译有界 typed contract，保存进
  durable `request.json`。Runner 再验证该 contract，通过 Claude Code 原生
  `--append-system-prompt` 每个 fresh/resumed Turn 注入；它明确当前 structured capability 名称、
  公网读取方法/端口、直接 NIC 仍隔离，并规定当前机器收据覆盖历史助手说法、优先最具体结构化工具、
  未尝试不得声称不可用。相同 contract 同时写入 `chatds.runtime.config` 原生 debug 事件，后续可从
  debug 直接审计当轮能力。行情 MCP/Legacy tool description 同步补充 `previous_close` 语义。这些
  逻辑没有证券名/代码、Session、Skill、V2.3、疾病、route/worker 或固定文件分支；mutation holdout
  使用 `renamed_catalog_lookup`/`renamed_evidence_lookup` 验证跨领域泛化。
- 成熟实现对照冻结独立仓库 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/QueryEngine.ts` 中 controller/SDK 用 `appendSystemPrompt` 叠加当前运行策略、工具权限继续由
  structured tool/MCP state 控制的模式；ChatDS 将它放在自身 verified Skill-view、签名 egress、
  session mount 和 durable request/receipt 之后。拒绝仅靠会话 prose、强制关键词路由或直接放开
  NIC；相关本地代码路径完整，无需 Web 搜索补足未知 stub。
- 回归结果：Claude Runner/Supervisor 全套 `71 passed, 1 skipped, 10 subtests passed`；行情、
  public-read、isolated executor 组合 `63 passed, 64 subtests passed`；egress proxy 全套
  `82 passed`；Backend 全套 `283 passed`；compileall、diff check、Compose config、候选镜像 import/
  revision smoke 均通过。未启动 V2.3 或其他模型重型 E2E。
- 代码提交为 `768dd745267daed3ed79b6661e606c28aaa6e627 fix: attest current Claude runtime
  capabilities`；clean Git archive 为 `/tmp/chat_ds_build_768dd745.OTIEej`（22,500 files）。切换前
  nonterminal AgentRun、active engine session、running/enabled schedule、动态 Turn container 均为
  0。SQLite online backup volume 为
  `chat_ds_db_backup_pre_768dd745_20260812_092943`，449,777,664 bytes，SHA-256
  `5b5c50b90fc25675a06aed46cda47574a57594124e64675dd3d6f103cadd65ef`，quick/FK 正常。
- 当前生产 Runner/Supervisor/保留的 Legacy Harness image 分别为
  `sha256:c461e34adbc6739a76d45173cd581b2f454600eb1235b64f3ffb29ced0887297`、
  `sha256:ad10cdcace6924d12c3e2d8261c616f8717833d83de76a80cd7274b9a5eadb48`、
  `sha256:97c7b4f2e1feefae2954ebcb68fdead55b9abbee8a05abd58d2c41a91a7cd10f`，revision
  均精确为 `768dd745...`；切换前镜像保留 `rollback-pre-768dd745`。容器 healthy/running、restart
  0，Claude 2.1.152；`127.0.0.1`、`10.10.132.126`、`172.30.100.128` 的 `/api/health`
  均为 200；生产 SQLite quick/FK 正常，切换后 active run/session 仍为 0，相关新容器日志无严重错误。
  用户可以在原 Session 直接重试同一行情问题；只有部署后的新 Turn 会收到当前能力 contract，历史
  错误回答本身不会被改写。

## 2026-08-11 Claude Skill 合约、持久进程与唯一执行引擎闭环

- 本轮把近期 ClaudeCodeEngine 会话暴露的问题统一重述为五个跨领域不变量：Skill 声明的最终
  artifact/模块/体量/行数/章节必须成为内容寻址的机器合约，不能由模型正文自证；持久 stdin
  进程必须有 controller-owned typed lifecycle，不能依赖后台 Bash 通知时序；本地 Bash task 与
  delegated agent 必须分型投影；每个失败终态必须持久化稳定 `error_stage/error_code`；部署声明
  Claude-only 时，Legacy 只能读历史，任何新 Conversation/Turn/settings/fork/底层 dispatch 都不能
  旁路。生产修复没有疾病、Skill/package/session、worker、固定报告名或 V2.3 分支。
- 新增 `backend/agent_engines/skill_contracts.py`。它只在有界、惯例化 orchestration/workflow YAML
  中编译显式 `output_contract` 或 `final_report_template.auto_merge`，拒绝不安全路径、冲突声明、
  越界 YAML/指令与倒置范围；可从声明的 inert `cat` merge 中提取 Markdown 模块 glob。合约随
  immutable Skill-view digest 写入 manifest，经 Supervisor verified receipt 一次性传给 Turn。
  Runner 只有观察到当轮原生 `Skill` tool receipt 才激活对应合约，因此已安装但与当前问题无关的
  Skill 不会强制运行或强制产物。最终文件必须是当轮唯一新建/修改匹配项；历史产物可保留；模块可由
  同 Session 的失败续跑复用。字节、行数、H1/H2 章节和模块存在性均由 controller 审计，失败以
  `artifact_contract_failed` 形成唯一 authoritative terminal。
- 对生产真实但重命名为 holdout 的复杂 Skill 零模型编译得到：最终 artifact placeholder、11 个
  module glob、20 个章节、153,600--256,000 bytes、至少 2,000 行；这证明此前 50/114KB 单文件或
  1,623 行结果不会再被模型 checklist 文案误报完成。该检查只是通用编译/基础验收，不替代用户手工
  V2.3 业务 E2E 或 ground-truth 语义对比。
- 新增按需 `chatds-process` stdio MCP：只有 Skill 指令同时声明 persistent/long-running 与 stdin
  才装配 `process_open/write/read/close`。它不接受 shell 字符串，限定 argv/进程数/读写量、可执行
  根与当前 `/workspace`/只读 Skill-view cwd；每个子进程有独立 session、合并输出 offset receipt，
  MCP EOF 时精确 TERM/KILL 全部 owned process，并从子进程环境剥离 provider/API credential。
  Turn 仍为 `network=none`，只挂当前 Session；子进程继承的 HTTP proxy 仍受既有 signed egress
  policy，不获得第二套 Bash、mount 或网络 authority。编译出的通用 runtime prompt 明确要求使用
  typed process MCP，解决 visual-browser 类 JSONL stdin driver 无法交互的问题。
- native task ledger 现在分型保存 `local_bash/local_agent/...`。`TaskOutput` 的 exact XML
  `retrieval_status/task_id/status` 可在通知缺失时结算；Claude 退出并由 PID 1 确认整个 disposable
  process group 已回收后，只把仍 running 的 `local_bash` 结算为 killed，delegated agent 继续
  fail closed。Backend 不再把 `local_bash` 伪装成 delegate AgentRun；真正 agent 仍用 Claude 描述
  命名并持久化。逻辑失败与异常失败均带阶段、稳定代码、native task summary、artifact receipt 和
  egress receipt。Web-search MCP 以及 TLS Proxy 另补齐 HTTP/DNS/timeout/certificate/handshake/reset/
  transport 的安全分类，不再把所有网络故障压成类名或同一个 TLS 错误。
- 部署配置新增 `LEGACY_ENGINE_NEW_RUNS_ENABLED`，生产为 `false`，默认 engine 仍为
  `claude_code`。Legacy 源码和历史 Conversation 保留，但 registry、模型兼容列表、新会话、已有
  Legacy 会话新 Turn、settings、fork 与最终 stream dispatch 均 fail closed；当前生产 registry
  实测只有 `claude_code`。未停止 Legacy 容器，因为 Backend 的共享存储健康证明和显式回滚路径仍
  使用它；“Claude-only”在这里指执行 authority，而不是删除 rollback 组件。
- 成熟实现对照冻结独立仓库 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配 `src/Task.ts` 的 typed TaskType/TaskStatus、
  `LocalShellTask` 先提交 completed 再通知、`TaskOutputTool` 的 exact structured terminal receipt；
  这些模式被放在 ChatDS 既有 Session mount、authority、ledger、artifact 与 exactly-one terminal
  合同之后。拒绝把参考仓库 private/stub 当实现证据，也拒绝用模型 prose、无限 retry 或后台 shell
  存活代替 durable state；相关本地路径无语义疑点，本轮未用 Web 搜索替代源码证据。
- 确定性回归覆盖跨域 inventory/warehouse/satellite rename、历史 artifact mutation、字段误编译、
  冲突声明、未调用 Skill 不激活、真实复杂 Skill 零样本编译、persistent stdin echo/cleanup、
  credential scrub、TaskOutput 无通知、local-bash/agent 分型、terminal stage/code、Legacy history-only、
  Web/TLS failure injection。Backend 全套为 `283 passed`，最终改动后的相关 Backend 为
  `45 passed`；Claude Runner/Supervisor 全套 `68 passed, 1 skipped, 7 subtests passed`；Egress
  Proxy `82 passed, 122 subtests passed`。Compose config、compileall、diff/genericity scan 与候选
  image JSONL smoke 均通过；未自动启动模型重型 V2.3 E2E。
- 代码提交为 `50b44861dae7a65845385bf94b41fc2ab8ec78b3 fix: enforce compiled Claude skill
  contracts`。clean archive `/tmp/chat_ds_deploy_50b44861.vMY5YZ1d` 与 Git tree 均 22,499 files。
  切换前连续两次确认 nonterminal AgentRun、active engine session、running schedule 与动态 Turn
  container 均为 0；SQLite online backup volume
  `chat_ds_db_backup_pre_50b44861_20260811_184725` 为 446,984,192 bytes，SHA-256
  `c094a6d276793ea1d1d6fe1b3a503fa2b3eedffd8cfe26fb73c74768f9d2af6d`，quick/FK 正常。
- 当前生产 Backend/Runner/Supervisor/Proxy image 分别为
  `sha256:ceb6af9bd003a06d1f2289199effa77b1821c042c17da7234cfb226d7fa5b0e9`、
  `sha256:83981279d0a4a30b67eb4b65c9bb28744cddd6d5d13e15f0777b138a92f298f8`、
  `sha256:4c5a210ae7f9046388cab57bb0a04c5f09709f428cbe6bd042e693a5101e3514`、
  `sha256:d8aa0f06d536acd2eb7ad062b51233ce1a6698f11501845cf2cb90230131c543`，revision label 均精确为
  `50b44861...`；旧镜像保留 `rollback-pre-50b44861`。相关容器 running/healthy、restart 0，Claude
  2.1.152 与 signed public-read policy attestation 正常；SQLite quick/FK、active run/session 与
  严重启动日志均正常/为 0。`127.0.0.1`、`10.10.132.126`、`172.30.100.128` 的
  `/api/health` 均为 200。下一项应由用户手工发起新 V2.3/其他 Skill E2E；收到 session ID 后仍须按
  debug/对话/exact Skill 三源流程验收，不能把本轮 deterministic closure 宣称为业务 E2E 已通过。

## 2026-08-11 统一沙箱公网只读出站闭环

- 用户指出对每个新网址增加 typed broker/hostname allowlist 是补丁式治理，并要求统一
  session-wise Bash/代码沙箱在不破坏文件系统隔离的前提下具备通用公网读取能力。本轮将问题重述为
  跨 Skill 不变量：网络 authority 必须由 deployment 拥有、与模型/Skill 参数分离、经过签名和总预算
  约束；任意规范 Skill 可以使用同一只读 profile，但不能自行扩大 method、port、私网或 header/body
  权限。提交 `133616f6740dea1643e4d5e2a4e1e42eceb4502b feat: add signed public-read
  sandbox egress` 完成该闭环，没有加入疾病、Skill/package/session、route、worker、文件名、固定来源
  或 V2.3 特判。
- 四个 Legacy session sandbox 和每 Turn 独立 Claude 容器仍保持 `network_mode=none`，没有 Docker DNS、
  默认路由或第二套 Bash 环境。新增的是签名 policy-v3 中固定的
  `public_read={methods:[GET,HEAD],ports:[80,443]}` profile；生产仅能由
  `SESSION_SANDBOX_PUBLIC_READ_EGRESS_ENABLED=true` 与
  `CLAUDE_PUBLIC_READ_EGRESS_ENABLED=true` 启用，ToolContext、Skill、MCP 和模型参数都不能铸造或
  修改它。既有 provider/MCP/Skill exact rules 优先匹配并保留所需协议 header；只有 exact rule 未命中
  时才进入公网只读 fallback。
- `skill-egress-proxy` 在 fallback 中删除调用方全部 header、credential 和 request body，只重建固定
  Host、User-Agent、Accept、identity encoding 与 close envelope；非 GET/HEAD、GET/HEAD body、
  非 80/443 端口、loopback、private、link-local、reserved、multicast、metadata、transition/NAT64
  地址以及 mixed public/private DNS 全部在上游连接前 fail closed。Proxy 自行解析、分类并 pin 公网 IP；
  policy/profile/root-run budget/call identity 全部受 HMAC 绑定，redirect 必须重新经过同一授权。
  Legacy declared command、Skill script/Python/persistent process、模型生成 `execute_code` 与 Claude Turn
  都复用同一协议；runtime cohort 标签统一为 `signed-public-read-v1`，Supervisor health 明确报告
  `network-none+signed-exact-and-public-read-egress-v3`。
- 该设计没有也不能宣称数学意义的“禁止上传”：域名、path、query、DNS/TLS 元数据本身就是出站信息，
  恶意代码仍可把少量数据编码进允许的 URL。当前固定 header、无 body、仅标准端口、全局次数/字节预算
  和无私网访问显著收窄泄露面；真正零任意外传仍必须使用固定参数 schema 的 typed broker。需要 POST、
  WebSocket、raw TCP、QUIC、非标准端口或认证 header 的合法应用，仍应由 Skill/MCP exact declaration 或
  新 typed capability 显式获得权限，不能借 public-read profile 放宽。
- 成熟实现对照冻结本地独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用并适配
  `src/upstreamproxy/upstreamproxy.ts` 对所有 subprocess 集中注入代理/证书环境、
  `src/cli/structuredIO.ts` 的结构化 sandbox permission callback，以及 `src/tools.ts` 的中央工具装配；
  这些模式被放在 ChatDS 既有 signed authority、receipt、budget 与 session mount 合同之后。拒绝
  `src/main.tsx` 中只适用于真正无网外层 sandbox 的 permission bypass，也拒绝给 Turn 容器直接加入
  Docker network。相关本地路径完整且无 stub/语义疑点，因此本轮没有用 Web 搜索替代源码证据。
- 回归证据：Proxy/Bridge/Topology 最终组合为 `119 passed, 206 subtests passed`；Claude 全套此前为
  `57 passed, 1 skipped, 7 subtests passed`，Executor/Proxy 完整相关组合为
  `216 passed, 1 skipped, 257 subtests passed`。Harness 宿主全量的可执行逻辑为
  `1972 passed, 1 skipped, 804 subtests`，其 19 个既有 failure 来自当前 `cc` 无权读取生产 NFS
  tombstone；隔离 root/tmpfs 全量为 1,983 项通过，唯一 CommonJS holdout 因测试镜像不带 Node 且用例
  mock 掉 worker-tree 准备而失败，同一 isolated-executor 路径在完整宿主 runtime 已通过。最终发布前
  rerun 的 Claude/policy/isolated 组合为 `113 passed, 1 skipped` 加同一个 Node 环境 holdout。
  `compileall`、Compose config、`git diff --check`、secret 与 genericity scan 均通过；未自动运行模型
  重型 V2.3 E2E。
- 生产从提交的精确 clean archive `/tmp/chat_ds_deploy_133616f6` 构建，archive/tracked tree 均为
  22,496 个文件。切换前两次确认 nonterminal AgentRun 和动态 Claude Turn container 均为 0；SQLite
  online backup volume 为 `chat_ds_db_backup_pre_133616f6_20260811_124338`，365,400,064 bytes，
  SHA-256 `96ec6fb3554058c39b687fa1537c9d01e929d46aa69ef211171cabbe56f9b5b8`，quick/FK 正常。
  切换前镜像保留 `rollback-pre-133616f6` 标签；Backend、Frontend、数据库、Browser、SearXNG/Valkey
  和其他生产容器没有重建。
- 当前生产 Proxy image 为
  `sha256:7f396ab6c8aae1692e2f3800179bbf876be7a2d9cb546bc7ec1875e805ae1da3`，四个 sandbox 为
  `sha256:33c723834ffd5843af0c154a3df5b453b1913f65bb852d6def185a0cff32427d`，Claude Runner 为
  `sha256:92e6ddcf0d4bd66f5c95034e4c46492cea99717b5fd1e0d402958d7fdacdbbfd`，Supervisor 为
  `sha256:9b1b15265a2a1170541ac42e57c4fdb9a11a61b8b810de1b4ab4771d8659672f`，Harness 为
  `sha256:2be8da8e348f1b48b7dbcced24ac99a7a082e934d0f30056973dc74d20e12281`；revision 均精确匹配
  `133616f6...`。所有长期目标容器 running/healthy、restart 0（inert Runner anchor 无 healthcheck）；
  Backend→Harness `/health`/`/v1/models`、localhost 和 `10.10.132.126:5173/api/health` 均为 200，
  SQLite quick/FK、nonterminal run 和严重启动日志均正常/为 0。
- 生产零模型真实网络 smoke 经 Harness→统一 executor→loopback bridge→签名 Proxy 完成：
  `https://example.com/` 与 `https://www.iana.org/domains/example` 均返回 200；
  `http://127.0.0.1/` 在上游连接前被拒，公网 POST 返回 403；receipt 为 policy v3、
  `controlled_egress_proxy`。该 smoke 证明是通用公网 profile 而非域名补丁，并且没有创建持久 Session、
  workspace artifact 或模型调用。

## 2026-08-11 `request_url_not_allowed` 与类型化实时行情闭环

- conversation `63a312df7a1d462fac00ac0926381de3` 已按三源证据闭环。持久化对话中的最后一轮要求查询
  德明利实时股价；四个 Claude primary run 均为 `succeeded/end_turn`，最后一轮实际通过 Bash 请求
  新浪、腾讯与东方财富公网接口时被 Turn bridge 返回 `request_url_not_allowed`，随后 Web 搜索虽成功但
  只能给出陈旧摘要。对应 immutable Skill view digest 为
  `4eee910b17659a55da8509d3ebb500ed502d609586ae41758e88220f0819cb12`，包含 TrialSim 主 Skill 与
  18 个生物医学 supporting Skills，没有行情 provider 声明；Skill router 也没有错误强制调用 TrialSim。
  出站 receipt 同期为 8 次 accepted、无预算拒绝或耗尽。因此根因不是容器断网、模型中断、Skill 编译
  或 SearXNG 故障，而是 Harness 缺少受控的实时行情 capability；任意模型生成 URL 没有签名 authority
  时被 fail-closed 拒绝是正确安全边界。
- 通用不变量为：模型生成的任意 Bash URL 永远不能自行成为出站权限；需要时效性和结构化语义的外部数据，
  必须经 deployment-owned、typed、固定上游、只读、限时限量的 broker 获取。提交
  `3143bda7 feat: broker typed market quotes for isolated turns` 新增独立、非 root、read-only 的
  `market-data-gateway`，只接受严格的 market/symbol/exchange 字段，不接受 URL、任意 query、额外字段或
  非 GET 请求；固定使用腾讯主源与新浪交叉核验并返回来源时间和 freshness。ClaudeCodeEngine 通过
  `chatds-market-data` stdio MCP 暴露唯一 `market_quote` 工具，Legacy Harness 通过同名 typed tool
  使用同一网关。Turn 容器继续 `network=none`，只把内部 `/v1/quote` 精确 GET 权限编入当轮签名 policy；
  新浪、腾讯、东方财富公网 origin 不会进入模型可见 authority。
- 成熟实现对照冻结本地独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。采用其 strict MCP configuration、typed tool schema、
  显式工具/权限装配模式，并适配到 ChatDS 既有 authority/receipt 合约；拒绝其仅适用于无网 sandbox 的
  bypass 思路。这里没有行情代码、证券代码、conversation/Skill ID 或 V2.3 特判；同类新数据域应新增
  typed capability 或由 Skill/MCP 明确声明 authority，不能扩大通用 Bash 权限。
- 回归结果：Gateway/MCP 目标套件分别 7 项及 4 个 subtests 通过，Legacy 行情工具 2 项通过；Backend
  `276 passed`；Claude Runner/Supervisor `56 passed, 1 skipped, 7 subtests`；Egress Proxy 与 Gateway
  `81 passed`；Frontend `24 passed`、production build 与 targeted ESLint 通过；Compose、py_compile、
  `git diff --check` 通过。Harness 全套约 1,939 项中 10 项因当前本地 `cc` 测试进程无权读取
  `/nfs/temp/chat_ds/*/.chatds-session-tombstones/*.deleted` 而失败，其中一个 delegation timeout 是同一
  前置权限问题的下游现象；没有为使测试通过而削弱跨 Session 文件边界。
- 生产由 commit `3143bda7` 的 exact clean archive 构建并部署。部署前确认无 active run/session，完成
  SQLite online backup，并保留 `rollback-12c30348` 镜像标签。当前 Gateway、Egress Proxy、Runner
  Supervisor、Harness、Backend 均 healthy，Frontend 与 Runner image anchor 正常运行，restart=0；
  `/api/health` 和目标 Session 页面均为 200。生产 MCP JSONL smoke 只列出 `market_quote`，并对 CN
  `001309` 返回腾讯主源与新浪核验一致的实时报价。该既有 Session 的 `enabled_tools` 为空，下一 Turn
  会自动继承包含 `market_quote` 的新默认能力，无需重新上传 Skill 或新建 Session。公网行情端点仍可能
  变更；网关会显式报告 source failure 并 fail closed，而不会退化为任意网络访问。

## 2026-08-11 `172.30.100.128:5173` 前端转发入口

- 用户报告 conversation `7ba267744b3a4b1d9bb322831793a776` 的运行配置显示
  `Request failed (502)`。按三源证据冻结的原始配置阶段（01:35–01:37 UTC）中：持久化
  Conversation 尚无消息，DB/AgentRun/engine session/raw event 均为 0；Session Skill 列表为空，
  因而没有可参与该错误的 Skill 指令；Frontend 与 Backend 记录的 settings GET 以及 01:37:20
  settings PATCH 全部为 200。PATCH 已正确持久化 `engine_id=claude_code`、
  `model_id=local_deepseek_v4_flash`。因此该 502 不是模型、Claude Runner、Harness、Skill 编译或
  配置落库失败。用户稍后在 01:47 上传 TrialSim bundle 并启动了一个独立 Turn；其后续运行、Skill
  view 与一次 debug mirror workspace lock timeout 不得倒灌成早先 502 的根因。
- 基础设施复现发现两个方向的 128↔126 新 TCP 流约有 1–3% 建连黑洞：128 上
  `systemd-socket-proxyd` 明确记录 `Failed to connect to remote host: Connection timed out`，从维护机
  对 128 的 5×100 raw 并发读取也有 11 个请求在到达代理前 `time_connect=0`。128 的两个 i40e
  接口都有累计 `rx_dropped`，而 conntrack、listen backlog、CRC/error 不是瓶颈。这是主机/NIC/交换
  路径问题，应用代理不能修复未到达主机的 SYN。另发现 128 系统时钟停在 2026-07-06、NTP inactive，
  与当前权威时间相差约 36 天；为避免扰动现有 vLLM/TEI/病理/Cloudreve 工作负载，本轮没有擅自跳变
  主机时钟，后续应由基础设施维护窗口修复 NIC/交换路径与 NTP。
- 原先单次上游尝试的 `systemd-socket-proxyd` 已由发行版 HAProxy 2.4.30 接管 128 的
  `0.0.0.0:5173`。HAProxy unit enabled/active；主上游为 `10.10.132.126:5173`，备用为
  `172.30.100.126:5173`，仅在上游尚未连接时做 4 次 connect retry，带双路径健康检查、HTTP keepalive
  和 6 小时 client/server/tunnel timeout，不重放已经发送的 HTTP mutation。权威配置已跟踪为
  `ops/chatds-forward-5173/haproxy.cfg`。旧 `chatds-forward-5173.socket/service` 保留但 disabled/inactive，
  仅用于显式回滚；128 上既有容器均未重启或改动。HAProxy 解决出站一次尝试和长流代理问题，但不能
  宣称修复上述入站链路丢流。
- 跨 Session 的 UI 不变量由本地提交
  `06e9b40acee603ac348ecbf9a57f77eab877d71b fix: isolate workspace reads from transient gateway loss`
  修复：API client 只对 GET/HEAD 的 transport error 或 502/503/504 做两次短退避重试；PATCH/POST/DELETE
  与 abort 永不自动重放。Session Workspace 九类资源改为 `Promise.allSettled` 独立加载，单个可选面板
  失败不会清空运行配置；每个失败显示精确资源和 API path；切换 Conversation 时用 exact
  `loadedConvId` 阻断上一 Session 状态；保存成功清除旧错误。脚本化测试覆盖 502→200、网络错误→成功、
  budget exhaustion 边界、400 不重试、abort 及 PATCH 不重放。Frontend 共 24 项通过，涉及文件的
  targeted ESLint 通过，production build 通过；全量 lint 仍只有两个修改前既有的
  `ModelSelector.jsx`/`SkillLibrary.jsx` React effect 规则错误。
- 生产 Frontend 已从 commit 的 22,489-file exact clean archive
  `/tmp/chat_ds_deploy_06e9b40a` 构建并单独替换。当前 image
  `sha256:a8b48f30fb027a6e29cb615c3238849027734a4687a2733407b46068cdc1eae3`、revision
  `06e9b40a...`、running/restart=0，旧镜像保留 tag `chat_ds-frontend:rollback-pre-06e9b40a`；
  Backend/Harness/Supervisor 未重启。localhost、`10.10.132.126:5173` 和
  `172.30.100.128:5173` 的 `/`、`/api/health` 均为 200，生产 bundle marker 正确。4×50 个带相同
  两次有界重试的逻辑读取全部成功，期间 raw TCP 仍捕获 4 次首次建连超时，证明 UI 缓解闭环成立且
  底层链路问题仍需独立处理。凭据未写入仓库、代理配置、日志或本文件。

## 2026-08-10 Claude Skill 相关性、受控搜索与部署默认值闭环

- 生产 conversation `28f32a430935405e92cc0ea53700cba8` 的最后两轮已按三源证据闭环。
  持久化对话中两次天气提问都被错误引向 TrialSim 后拒答；对应 primary runs
  `a4ccadc543f74de788b3909fe59f3426`、`8092a780b8754b0bb2f04d7c9a25af9d`
  均是唯一 `succeeded/end_turn`，但 init 中 `mcp_servers=[]`、`webSearchRequests=0`，没有
  provider stream 或 Claude core 错误；exact immutable Skill view digest 为
  `241bcf331e12478a0a6b997496a4d4c7d5660173ed95f6d4ca0e675b77c42127`，其中
  `healthsim-trialsim/SKILL.md` 明确只描述临床试验任务。旧 ChatDS 编译器却生成
  `chatds-harness-session-entry` 并在每个 Turn 强制 slash invocation，同时没有给 Claude 装配搜索
  MCP。SearXNG 同期手工查询有结果。因此两项缺陷都在 ChatDS Skill/capability adapter，不是 Claude
  Code 后台不会判断，也不是该时刻 SearXNG 无数据。
- 通用提交 `8b0f61eda471e96445366a33f495e25349685e48 fix: route Claude skills and search by
  capability` 移除了每轮强制合成 entry Skill。已安装/选中的 Skills 仍以 immutable manifest 和原生
  plugin 暴露，但由 Claude 按 `SKILL.md` description 与当前问题相关性决定是否调用；旧内容寻址 view
  仍兼容恢复。新增 Harness-owned `chatds-web-search` stdio MCP，仅在 capability 开启时编译，并把
  authority 限定为 `GET http://searxng:8080/search`、当前 Turn HMAC policy、固定私网 CIDR 和总出站
  budget 的交集。Turn 容器继续 `network=none`，只有受信 Egress Proxy 同时连接出公网与固定
  `search_net`，没有给模型开放任意网络。
- 首次生产搜索 smoke 证明 Skill 路由和 MCP 装配已正确：一个仅描述博物馆来源核验的合成 Skill
  没有被天气问题调用，Claude init 中 `chatds-web-search=connected`，并实际调用搜索工具。但工具三次
  收到 `RemoteDisconnected`。签名规则、DNS pin、私网 allowlist、SearXNG 直连和 budget receipt 都
  正常；对真实 SearXNG 的 UDS probe 与进程内逐阶段 probe 都定位到 cleartext HTTP 代理在请求发送后、
  响应返回前执行 `upstream.shutdown(SHUT_WR)`，上游把提前 FIN 当作客户端中止并返回空流。
- 该跨域不变量以 `858f0d25f19bd61be3c491107a1677aad2f7f961 fix: preserve HTTP request lifetime
  through proxy` 修复：单请求限制、Content-Length 校验、`Connection: close`、deadline 与 response-only
  relay 继续负责边界，但代理不再用提前 TCP 半关闭替代 HTTP framing。新增的通用、非业务
  `FinSensitiveOrigin` 失败注入在旧实现稳定得到空响应，在新实现通过；两个 cleartext HTTP lane 都
  采用同一修正，没有台风、SearXNG、Skill/session ID 或 V2.3 特判。
- 成熟实现对照仍冻结本地独立 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`。`src/commands.ts` 的 description-driven Skill
  暴露、`src/main.tsx`/`src/tools.ts` 的首轮 MCP 工具装配被采用；原生 MCP/HTTP 客户端的标准请求—
  响应生命周期被适配到 ChatDS 既有签名 authority/receipt 边界。拒绝“安装即每轮强制执行”和绕过
  policy 的原生任意 Web 能力。确定性代理复现已无语义疑点，因此本轮没有额外 Web 搜索框架调研，
  也没有臆测本地参考仓库中不存在的 private/stub 行为。
- 同一 `8b0f61ed` 提交完成部署模型/上下文闭环：selector 现在有
  `shaiengine_glm_5_2`（1,000,000）、`shaiengine_deepseek_v4_pro`（200,000）、本地
  `deepseek_v4_pro`→`10.10.132.2:1025/AgentModel`（918,528）、
  `local_deepseek_v4_flash`→`10.10.132.126:1025/AgentModel`（1,048,576）和
  `qwen3_5`→`10.10.132.128:1025/qwen3_5`（262,144）。共享 wire model 名的两个本地端点由
  route/profile 保持独立；Claude 启动使用 exact capacity 设置
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`，大上下文模型附加 native `[1m]` client marker，实际 wire
  model 仍是 provider 声明名。新 Conversation 的生产默认 engine 为 `claude_code`；已存在
  Conversation 继续使用自身持久化 engine。Workspace 每个常规文件及编辑器工具栏均有带认证的下载
  按钮，后端 raw endpoint 继续先鉴权、拒绝 traversal/symlink/special file。
- 回归结果：Backend `275 passed`；Claude Runner/Supervisor 主套件 `51 passed, 1 skipped,
  7 subtests`，本次代理相邻套件另为 `45 passed, 4 subtests`；Egress Proxy `78 passed,
  117 subtests`；Frontend `19 passed` 且 production build 通过；Harness 可执行逻辑为 1,975 项通过。
  三个未计入的旧 fixture 测试绑定历史 TrialSim snapshot 阈值，不是本轮通用逻辑失败；没有自动发起
  模型重型 V2.3 E2E。模型 `/v1/models` 容量、Compose、py_compile、diff/secret/genericity scan、
  direct MCP JSONL 和真实 native Claude MCP init 均验证通过。
- `8b0f61ed` 上线前 DB backup volume 为
  `chat_ds_db_backup_pre_8b0f61ed_20260810_174223`，345,542,656 bytes，SHA-256
  `a08782c1b99ba7ae9a82024029338f28b5e13c2855a461e8a12bec3f06fe2305`，quick/FK 正常。
  当前 Backend `sha256:c3e05f5720ee44aa038f40d79dc98f6c043b1fd432e99c42fab3d4f9712a9510`、
  Harness `sha256:81a5d21f913d8921758f215627bf9b3c1068874f30b5ac7ba5870781ec655146`、
  Supervisor `sha256:5bec97658a4d6c61aa90ae0b0b9843d70b507fafd6f429645ed1126e9329dda9`、
  Runner `sha256:5174f037b00605bf671f4ec2c5aa9075b122d30e22feccb63a751c4b9451e1df`、
  Frontend `sha256:bf27848afa91b4cc1ac7d602c0bed5bc8356549bc684acf24884e6e7fcf66795`
  的 revision 都是 `8b0f61ed...`；Proxy 已进一步更新为
  `sha256:c1395f6c1fad2ba52b844c62e00a7901824b3b1c95a6c0f06cfc037eb12282cc`、revision
  `858f0d25...`。所有目标容器 running/healthy、restart=0，localhost 与
  `10.10.132.126:5173/api/health` 均 200。
- 最终真实生产 smoke 使用 `shaiengine_deepseek_v4_pro` 和无关的合成
  `museum-provenance` Skill：Claude 没有调用该 Skill，自动调用
  `mcp__chatds-web-search__web_search("白海豚 台风 现在 位置")`，工具成功返回中央气象台、新闻和
  上海台风路径共 3 条结果，assistant 为 `SEARCH_SMOKE_OK: 台风路径`；native result 与唯一
  Supervisor terminal 都是 succeeded，egress receipt 为 3 accepted/3 clean close、0 budget
  rejection、not exhausted。该合成 Session 的 control state、run index 和 workspace 已清理。

## 2026-08-10 ClaudeCodeEngine 启动事务闭环

- 用户报告的生产 conversation `9cd170e6ac064be4a03b978022153f6d` 已按三源闭环查验。持久化对话确认
  engine=`claude_code`、model=`shaiengine_glm_5_2`，只有一轮用户请求和误导性的
  `The Claude Runner event stream timed out`；exact immutable Skill view 是一项 primary
  `healthsim-trialsim` 加 18 项 supporting Skill（177 files、2,549,525 bytes），声明 7 个并行
  worker、后续聚合与大报告合同；AgentRun/debug 则显示 root 从未启动，generation=0、0 token、
  0 tool、0 native event、0 artifact、container_id=null。Backend 在约 330 秒后先超时，Supervisor
  的 `/v1/runs` POST 到约 15 分钟才返回。故障发生在真实 Claude Code 2.1.152、模型和工具启动
  之前，不是本地 `claude-code/` 参考仓库或 Claude 内部 Harness 错误。
- 通用根因是 Supervisor 在 HTTP event loop 上同步执行 NFS Session 路径解析、完整 Skill view
  hash/manifest 验证，并在 egress compiler 再次读取认证资源；同一阻塞还让 cancel/health/events
  失去活性。另有两项部署漂移：动态 Runner tag 被清理，以及旧 `session-sandbox:latest` 缺少
  `signed-exact-query-v1` 运行时；缺镜像健康检查又因 `JSONResponse` 参数顺序错误返回 500。
- 跨 Skill 不变量已提交为 `a3d6ef88caaf78af41676b5f37c7de3342b8cb00 fix: make Claude runner
  startup transactional`：`POST /v1/runs` 只做部署拥有的 provider binding，然后在 Supervisor
  本地 volume 原子持久化 identity-bound admission/request/status/locator 并立即返回；慢 NFS/Skill
  preflight 在独立有界 executor 中运行。cancel/session cleanup 先写本地 authority fence，late 或
  timeout preflight 在每个后续边界都失去 Docker authority；Supervisor restart 会重排 durable
  preflight/queued work。event stream 在 preflight 阶段只读本地 ledger，每个后端 ledger 至多一个
  异步读取，不再因 NFS read 卡死 heartbeat/cancel。Skill view 产生 typed
  `VerifiedSkillView` receipt，policy compiler 复用已认证的 `SKILL.md`/`.mcp.json` bytes，不再二次
  hash/read。Backend 现在把 start acceptance timeout 与 event inactivity 分成
  `claude_runner_start_timeout`（retryable）和 `claude_runner_event_stream_timeout`。preflight
  默认 1800 秒、部署可调且上限 3600 秒；模型 Turn 的四小时 hard timeout 未缩短。
- 动态 Runner 镜像现在由一个 `network=none`、read-only、cap-drop、64 MiB/0.05 CPU 的 inert
  Compose anchor 持有，防止常规 stopped/dangling cleanup 再删除其唯一 tag；Supervisor 依赖
  anchor started。Runner 仍是官方 npm/package native binary Claude Code 2.1.152，不使用本地
  `claude-code/` 仓库作运行内核。为避免旧基础镜像漂移，本轮从同一 clean source 重建
  `chat_ds-session-sandbox:runner-base-a3d6ef88`，但没有替换四个 Legacy executor 容器。
- 成熟实现对照仍只使用本地独立参考仓库 commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`：`src/Task.ts` 与
  `src/tasks/LocalMainSessionTask.ts` 的 typed pending/running/terminal 和 work 前注册状态被采用；
  `src/cli/structuredIO.ts` 的本地立即 cancel/忽略迟到结果被适配为 durable authority fence；
  `src/cli/transports/SSETransport.ts` 的 transport liveness 与 workflow terminal 分离，以及
  `SerialBatchEventUploader.ts` 的单写者有界 pending/backpressure 仅用于校验现有 ledger 方向。
  没有复制 private/stub 行为，也没有把参考仓库打包进生产。
- clean source 为 `/tmp/chat_ds_claude_start_final.VZmBpO`（HEAD archive 加且仅加本轮 7 个精确
  修改文件）。最终回归：Claude Runner/Supervisor `40 passed`，Backend `268 passed`，
  `py_compile`、`git diff --check`、Compose config、production genericity/credential scan 均通过；
  新增 synthetic generic missing/slow Skill view、late preflight、timeout、cancel、restart、single
  attestation、missing image 503 和非业务 rename/identity holdout。未自动运行模型重型 V2.3 E2E。
- 生产已切到 commit `a3d6ef88` 对应代码：Backend image
  `sha256:a72a1758504534d921a8a99b55e0ff85f14e17e512e6245a4217382d96e2721a`，Supervisor
  `sha256:a44e8f7ca51508ae8971bc3d86184a20908b0ec8a69dab83059e681819bb941d`，Runner/anchor
  `sha256:3c9536c30a3cfdc49ebeaf20fa6c2c592257bcd7731564daf7eb68093dc81ceb`。三者 running，
  Backend/Supervisor healthy、restart=0，Supervisor health 精确报告 Claude 2.1.152；
  `127.0.0.1` 与 `10.10.132.126` 的 `/api/health` 均 200。上线前 nonterminal AgentRun 和 active
  engine session 均为 0；SQLite quick/FK 正常。online backup volume 为
  `chat_ds_db_backup_pre_claude_starttxn_20260810_153122`，270,667,776 bytes，SHA-256
  `db1b5f413ba6d0f655f920de5e49219801ad7894102e28935393dd438f1dad58`。
- 生产零模型 failure-injection smoke 验证完整新路径：start admission 120 ms 返回 preflight，
  不存在的 generic Session view 后台形成唯一 `preflight_HTTPException` failed terminal，动态 Turn
  容器从未创建，Session cleanup 成功。该 smoke 的预期 traceback 是注入故障证据，不是生产残留。
  历史 `chat_acits_claude_runner_supervisor_e2e` 与 `chat_acits_backend_e2e_161b4e43` 容器未参与
  当前生产路由且本轮未擅自删除；模型/vLLM/其他生产容器均未触碰。

## 1. 当前结论

- 2026-08-05 最新 ClaudeCodeEngine 验收与候选修复：生产会话
  `25c72ca95b8544b58f3f57f6d8a9dc66` 在用户选择 `claude_code` 后有 0 Message、0
  AgentRun、0 engine session、0 raw native event，失败发生在 durable run 建立之前。
  根因是首轮启动路径缺少 `AsyncExitStack` 导入，并对不存在的上一代 native session 做了
  `generation=None > 0` 比较；两项已提交为
  `161b4e43285333d1a20a09a7a0279edf16ceb343 fix: start first Claude Code turn reliably`。
  后续真实 E2E 又形成一组尚待本节所述最终提交/部署的通用候选：显式选中的根 Skill 由不可变
  Harness-owned slash entrypoint 强制完整读取，supporting bundle Skill 只保留为依赖；移除会把
  Claude 2.1.152 原生 Task/Agent 工具静默裁掉的 `--bare`，同时继续使用空 setting sources、
  per-Session HOME、严格 MCP、单 Session mount、network-none 与签名 exact egress；第三方
  Messages provider 默认禁用不属于兼容协议保证的 WebSearch/WebFetch；Provider exact route、
  Turn/Proxy 出站预算、NFSv3 `ENOTEMPTY` 内容寻址发布竞态、native child/task-list terminal audit、
  tool name 与 stream message 去重均已闭合。Claude 直接写入 workspace 的新/修改常规文件现在
  还会在同一 Session mutation lock 内生成有界、内容寻址的 `artifact.created` 账本，不再只在
  文件系统可见而 `artifacts` 为 0；该实现按 ctime/identity 选择变更并拒绝 symlink/special file、
  文件/数量/总字节越界和审计期漂移，没有报告名或业务特判。
- 最新真实 V2.3 ClaudeCodeEngine case 为 conversation
  `c0d54695d6ac42aa9c9449d09a084f26`。首次 root
  `204ba0dbd83f4b0bb35262d69f8445e3` 使用
  `shaiengine_deepseek_v4_pro`，19 分 22 秒 durable succeeded，实际生成 11 个模块、README、
  checklist 和 full report；3 个数据库检索 child failed、1 个修正 child succeeded，另一个后台
  merge child 在 parent terminal 时被取消，但 parent 自身完成了 merge。首次 full report 为
  112,883 bytes/`wc -l` 1989，暴露“接近 >2000 行阈值却被 checklist 误记为通过”和 parent 可在
  native background child 未结算时完成两项通用缺陷。验证 continuation root
  `adb85e43f9ae44e1a1ac8ebfe5109763` 通过两个均 succeeded 的 verifier child 修正为
  112,943 bytes/`wc -l` 2019，满足 exact Skill 的 >100 KB、>2000 行、11 模块顺序 merge 和
  checklist 合同。ground truth 为 200,094 bytes/3383 行，因此当前结果达到 Skill 硬性最低合同，
  但只有 oracle 约 56% 字节量，不能宣称逐字或完整业务等价。上述 root 使用的是 artifact-ledger
  修复前候选，所以其 DB 中 artifact row 为 0；workspace 文件本身完整存在。
- 本轮肺癌 MDT 新 conversation `03b90a3ff581421682feafc6d4f58031` 尚未建立 AgentRun：Backend
  在读取 User Skill `lung-cancer-mdt` 的 immutable view 时进入宿主 NFSv3 hard-mount
  `folio_wait_bit_common`。独立只读 soft mount 能枚举全部 36 个文件，但读取第 8 个文件开始返回
  `EIO`/随后同样进入 D-state；本地 volume 只恢复 7/36 文件，残缺 ZIP 校验为 `BadZipFile`，不得
  用来冒充 E2E。NFS server ICMP、TCP 2049 与 NFSv3 RPC 当前都可达，故障位于导出文件数据页/
  存储数据面而不是 Claude provider、Skill 路由或网络白名单。该 case 保持 0 Message、0 AgentRun、
  0 artifact；NFS 恢复后必须用全新 conversation 重跑，避免与当前迟到请求竞态。
- 最新候选从本地 clean archive（避免宿主 NFS 读取卡顿）完成 Backend `267 passed`；
  Runner/Supervisor/Proxy 为 `109 passed, 1 deselected, 115 subtests passed`。唯一 deselected 是
  未改动的 TLS 1.3 post-handshake-auth 环境用例：临时 Backend 测试镜像的 OpenSSL 组合在该
  用例发生 BrokenPipe；同套完整运行除此以外全部通过。`py_compile`、diff、secret 与 genericity
  scan 通过。宿主默认 Python 自身位于 NFS，直接 pytest 与 `git archive` 当前会进入 D-state，
  因此候选测试使用 `/tmp/chat_ds_deploy_161b4e43.lFIsij` 的 clean tree 加当前精确 diff，并在
  隔离 Docker runtime 中运行；不得把 NFS D-state 误报为测试逻辑失败。
- 上述候选已经本地提交并部署为
  `c148a0b440cfce19621340524fb942861248878f fix: execute selected Claude skills transactionally`。
  精确 clean tree 为 22,482 个 tracked 文件；上线前 SQLite online backup volume 为
  `chat_ds_db_backup_pre_c148a0b4_20260805_221507`，大小 270,528,512 bytes，SHA-256
  `8d5772717b59e295ccae4b53248a195fa113d5cbb931d7f49424deed0f4d4f75`，quick/FK 正常。
  当前生产 Backend image 为
  `sha256:d89d7dd72edf9a66d2082a93569b47424ccc6f897ea783c013f0ce22d3ba9e11`，Supervisor 为
  `sha256:60928a56aed7b5de542cc1a36ef8529fd748265f4e752cff38cb92eb548a8161`，Egress Proxy 为
  `sha256:9c56e5509e327322c5bbf708ac91c5ecaf0913054fb7d2305b5009f135234f1f`，Claude Turn
  Runner 为 `sha256:63a91c566a4ca8bdbb11ff6ab971d1970948e7aa331834abb4b0f91e2f7d9894`；四者 revision
  均精确为 `c148a0b4...`，Claude Code 仍为 2.1.152。旧镜像分别保留
  `rollback-pre-c148a0b4`。Proxy、Supervisor、Backend 均 healthy/restart 0；三个 Frontend
  入口 `/` 与 `/api/health` 均为 200，SQLite quick/FK、nonterminal run、active engine session、
  残留 Turn container 与严重日志均为 0。Proxy 实际 ceiling 为 8,192 requests、64 MiB outbound、
  2 GiB response 和 14,400 秒 tunnel；发布 tmpfs 环境副本已删除。
- 部署后的真实首轮 smoke conversation 为 `c378cb839ce0402e93a51dc8f3861e05`，root
  `ccaf68376f7849aa9f867f3b1c3e7ecc`。它使用 ClaudeCodeEngine +
  `shaiengine_deepseek_v4_pro`，durable `succeeded/end_turn`，generation=1、native checkpoint
  已 committed，assistant 持久化正文精确命中 `SMOKE_OK`；Bash 写入并回读的
  `claude_engine_smoke.txt` 内容与 29-byte 合同一致，Artifact row 的 source 为
  `ClaudeCodeWorkspace`、SHA-256 为
  `d1fc42fe96dad010730b35d229f430d6af088d7dfe25518ca391de3db2d7d808`。这直接证明原生产
  session `25c72...` 的首轮闪退路径和新的 workspace artifact 投影在正式容器中均已闭合。
- 本次 Supervisor 首次重启还揭示两个旧 local run locator 指向 NFS 状态并在启动对账时进入
  D-state；生产 DB 中精确确认两个 run ID 都不存在。它们没有被删除，而是从 local state volume
  的 `run-index/` 原子移动到 `quarantine-nfs-20260805/` 后保留；随后 Supervisor 正常启动。当前
  quarantine=2，run-index=1（上面的有效 committed smoke session）。这不修复 NFS 数据面；有效
  native checkpoint 仍需 NFS 恢复才能保证未来重启/续接读取稳定。
- 工作目录：`/nfs/yangbb/codes/chat_ds`。
- 分支：`fix/generic-skill-harness-20260717`。
- 2026-08-05 新增可选 `ClaudeCodeEngine`，Legacy Harness 保留且 Conversation 级固定
  engine；切换 engine 必须 fork，不能拼接不同原生 transcript。Backend 只保存稳定归一化事件，
  同时把 Claude 原始 stream-json 持久化到独立 lossless ledger。每 Turn 使用新的候选 native
  session；只有唯一成功 Supervisor terminal、原始事件和 assistant 投影均 durable 后才提升为
  committed checkpoint，失败/取消不会污染上一个可恢复会话。Backend/Supervisor 重启、取消和
  Conversation 删除都有 fail-closed reconcile/cleanup。
- Claude Turn 由受信 Supervisor 动态创建独立容器：只挂载当前 user/session workspace、当前
  session state、内容寻址只读 Skill plugin、单请求和本机 mutation-lock volume；无 Docker socket，
  `network_mode=none`、只读根、cap-drop、资源上限和紧凑 seccomp。Skill view 仅包含 DB 已授权的
  user/session Skills，显式编译受限 stdio/http/sse MCP，`--bare --strict-mcp-config` 禁止 ambient
  project/user MCP。官方 npm 包和平台二进制固定为 Claude Code `2.1.152`，构建时校验版本和真实
  native binary，不复制宿主不透明二进制。
- Claude 网络制度见 `claude_runner/NETWORK_EGRESS_POLICY.md`：Turn 默认无网，只能经回环桥接到
  Skill Egress Proxy；Provider 仅精确 `POST /v1/messages`，当前用户 URL 仅精确 query 的
  `GET/HEAD`，Skill/MCP 只获得其静态声明的方法/路径前缀，私网还要求部署白名单、Skill grant、
  当前 Turn URL 三方交集。不存在通配域名或临时全网兜底。策略集合、root-run budget 和 call ID
  经 HMAC 绑定，重定向重新鉴权，终端保存出站计数/预算/摘要 receipt。Sandbox、Runner、Proxy
  共同声明 `signed-exact-query-v1`；镜像构建实际导入解析器校验，Supervisor 再校验 Runner label，
  防止编译器与旧执行镜像漂移。
- 当前宿主 runc/kernel 把 `no-new-privileges + seccomp` 组合拒绝为 `errno 524`。Claude Turn
  使用 `seccomp_stripped_setid`：构建移除全部 setuid/setgid/file capability 并由 Supervisor 校验，
  运行仍保留 seccomp、network-none、cap/mount 边界。Egress Proxy 同样构建期剥离 setid/cap，
  以非 root、cap-drop、只读根和 Docker 默认 seccomp 运行；固定、无网的 socket initializer 只保留
  窄 capability。宿主修复后可切回 `seccomp_no_new_privileges`。
- 本轮确定性验证：Backend `258 passed`；Claude Runner/Supervisor `20 passed`；Egress Proxy
  `76 passed`；共享网络策略 `45 passed, 43 subtests`；Executor/Browser/Topology
  `135 passed, 1 skipped, 140 subtests`；Frontend production build 通过；默认及
  `claude-code` Compose config 均通过。真实、零模型 token 的固定 CLI 容器 E2E 已通过完整
  Supervisor → 动态 Docker Turn → 回环代理/零出站 → native stream → checkpoint → 唯一 terminal
  链，结果为 `succeeded`、4 events、1 native result、checkpoint present、0 egress connections。
  本轮未自动运行模型重型 V2.3 E2E。
- 2026-08-05 四模型 Claude provider 闭环已完成，本地代码提交为
  `59fe8d0c54a95a4e2df28ca5a3c5de03f6ab3e6d feat: expose all deployment models to Claude engine`。
  模型目录以显式 `claude_provider_profile` 绑定部署 profile，不再把宽泛 provider family 当作
  Claude 兼容证明；当前 Claude selector 包含 `shaiengine_glm_5_2`、
  `shaiengine_deepseek_v4_pro`、本地 `deepseek_v4_pro`（API model `AgentModel`）和
  `qwen3_5`。`backend_protocol=openai` 只校验既有 ChatDS/Legacy catalog route；Claude Turn
  始终设置 `ANTHROPIC_BASE_URL` 并只获准精确 `POST /v1/messages`。两个本地服务分别由真实
  Claude Code `2.1.152` 做了无工具、无会话持久化、单 Turn Messages smoke，均返回唯一
  `result/success`；所以本地 OpenAI-compatible Legacy 路由和 Anthropic Messages Claude 路由可并存。
  provider credential 仍只存在于 Supervisor/Turn deployment profile，Backend 请求不会转发用户或
  catalog secret；两个私网 origin 还必须同时通过部署白名单和签名 Turn exact-path policy。
- `59fe8d0c` 回归结果为 Backend `261 passed`、Claude Runner/Supervisor `24 passed`、Egress
  Proxy `76 passed, 117 subtests passed`、Frontend `19 passed` 且 production build 通过；Compose
  三个 profile 和四组 model allowlist 解析通过，`py_compile`、`git diff --check`、secret scan、
  genericity scan 均通过。本轮没有运行模型重型 V2.3 E2E。
- `59fe8d0c` 已从精确 clean archive `/tmp/chat_ds_deploy_59fe8d0c.Vzvr4R` 部署生产，archive 与
  Git tree 均为 22,482 files。上线前备份 volume 为
  `chat_ds_db_backup_pre_59fe8d0c_20260805_165630`，大小 `263806976` bytes，SHA-256
  `6922b18369ace298894882e7d3b939ced12635323ee67c6015ab1e2a59acfcbf`，quick/FK 正常。
  当前 Backend image 为
  `sha256:56a06ef70bae823c99b231b16875c6fc3407c57bcbf8f8c50dd140b83d2ce8ab`，Supervisor 为
  `sha256:bcd0e11a640883a83554bf136c5c4e344e280ce74b6d783b0c3e53ce18f797d1`，revision 均精确
  匹配 `59fe8d0c...`；旧镜像分别保留 `rollback-pre-59fe8d0c`。Proxy 代码镜像未变，容器已重建
  以加载本地 provider exact-origin allowlist，image 仍为
  `sha256:8b79b29d59605ebe54b3d12200a071b54ce2eb294a027f24fe692898827ef2f4`。
  三服务均 healthy/restart 0；Backend 实测 `legacy`/`claude_code` 均 available，四模型 Claude
  compatibility 全为 true，Proxy 容器到两个本地 `/v1/models` 均为 200。`127.0.0.1`、
  `10.10.132.126`、`172.30.100.126` 的 Frontend `/` 与 `/api/health` 均为 200；SQLite quick/FK、
  nonterminal run、active engine session、running schedule、残留 Claude Turn 和严重日志均正常/为零。
  生产 `.env` 未读取到日志或复制进仓库，发布用 `/dev/shm` 0600 临时副本已删除。
- `ClaudeCodeEngine` 实现已本地提交为
  `6ad54bd1 feat: add isolated Claude Code agent engine`，并于 2026-08-05 从 clean archive
  `/tmp/chat_ds_deploy_6ad54bd1.bEDuDE` 部署到本机生产。上线前 SQLite 在线备份为 Docker volume
  `chat_ds_db_backup_pre_6ad54bd1_20260805_155037`，大小 `263704576` bytes，SHA-256
  `0051a84ddd7d7f854fdb1953351fb28a2f3dcc55fa76c8134ba636cce7e03d2b`，quick/FK 均通过。
  当前生产镜像为 Egress Proxy
  `sha256:8b79b29d59605ebe54b3d12200a071b54ce2eb294a027f24fe692898827ef2f4`、Claude Runner
  `sha256:80ca75f505c8a05c455aa0149216a6e04fb305819b22d53d2061c6eeb9d262ed`、Supervisor
  `sha256:9313a7b8744ccdab722baeb662a31212591883b3fb23d1358b69c20fb2433f1d`、Backend
  `sha256:2e00fd967f83053c221c50c784b661b888ab009572f5c45a03e2e4efbb28ca71`、Frontend
  `sha256:bcff7403fcbeaad8e5b1bf4f42258ddbeb662a9787b3078a75f9152d712c4ef2`。Proxy、Supervisor、Backend
  均 healthy/restart 0，Frontend running/restart 0；后端实际发现 `legacy` 与 `claude_code` 都
  available，Claude 版本为 `2.1.152`。`127.0.0.1`、`10.10.132.126`、`172.30.100.126` 的
  `:5173/` 与 `/api/health` 均返回 200；新表/列、SQLite quick/FK、零 active run、零残留 Claude
  Turn container 均通过。生产 `.env` 保持 root:root/0600，只持久加入非秘密开关
  `CLAUDE_CODE_ENGINE_ENABLED=true`；发布时通过 `/dev/shm` 的 0600 短生命周期副本传给 Compose，
  没有打印或持久复制凭据。旧 Legacy Harness 与四个既有 Executor 未在本轮重建。
- BuildKit 首次构建 `executor/Dockerfile.browser-runtime` 时仅因远端
  `docker/dockerfile:1.7` frontend 元数据连接重置而失败；该 Dockerfile 没有使用 1.7 专属语法，
  因此已删除不必要的 `# syntax=` 远端依赖。正常 BuildKit `node-deps` target 随后从正确
  `executor/` context 构建通过，日志不再请求 Dockerfile frontend；digest-pinned 基础镜像约束不变。
- 2026-08-04 用户更新了双 Skill 迭代的成熟实现对照规则：以 ChatDS 为实现基础继续迭代，
  本地独立仓库 `claude-code/`（当前冻结 commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`）是从现在起唯一的成熟 Harness 实现参考。
  每轮原有三源诊断、逐 delegate 归因、通用不变量、确定性复现、跨领域 holdout、修复、回归、
  local commit、部署与观察步骤不变；只把“Web 搜索/多框架调研”替换成“读取该冻结仓库中与故障
  对应的实际代码路径并给出 problem -> code path/pattern -> adopt/adapt/reject 映射”。该源码是
  默认和主要设计证据；只有相关路径是 stub、调用链断裂或确有语义疑点时，才允许围绕该疑点做
  最小化 Web 补证，并必须分别记录源码证据、Web 补证和最终取舍。不恢复 OpenClaw/Hermes 或其他
  框架的常规轮询。ChatDS 不依赖该仓库构建或运行，有用机制仍须在 ChatDS 内独立实现并通过跨
  Skill 测试。
- ChatDS 原创贡献现采用根目录 `LICENSE` 中未经修改的 PolyForm Noncommercial 1.0.0；
  `THIRD_PARTY_NOTICES.md` 明确排除了第三方目录、独立参考仓库、运行时数据、上传 Skill 和生成产物。
  该许可证没有、也不会重新授权 `claude-code/` 等第三方内容。
- 2026-08-04 最新权威状态：Round 16 已完成两个全新顺序 E2E、exact Skill/对话/debug/tool/provider/
  artifact 三源诊断、冻结 `claude-code/` 对照、通用修复、完整回归、本地代码 commit 和生产切换。
  V2.3 conversation `8bdd202c6b854c07b21e61100723a977` / root
  `3fef4aeefbd74600866712c02ecb3853` 的 Competitive Landscape 首次与精确 retry 都返回填充过的
  typed DrugBank 字段，但没有任何真实 evidence receipt；旧 Harness 只在 child 返回外层拒绝，导致昂贵
  的整个 child 重跑。肺癌 MDT conversation `7f8382b53003479b9c38d5f7d43d1c15` / root
  `129194592ba943b4842d7cc610902fe5` 已进入 semantic capability-plan transaction，前四次收到 duplicate、
  schema 与 unselected-capability feedback；第五次只余一个 internal `coverage.iu-*`，但 model-facing
  feedback 没有 exact document/ordinal 坐标，模型无法据此修正。逐 child、provider、tool、artifact 与
  exact Skill 证据见 `E2E_ITERATION_LOG.md` Round 16。
- Round 16 通用代码提交为
  `8097db3ca14d9341cffcf5d4253c5c8c51133728 fix: keep skill validation corrections transactional`：
  parent-compiled evidence obligation 现在进入 child 的同一 bounded structured-output transaction；只有
  runtime-owned 成功 receipt 才能支撑非空 evidence claims，零 receipt 时模型可在同一 child 内改为
  `null/degraded` 或补正，工具、authority 与副作用均不重开。capability-plan validator 把内部
  content-addressed `coverage.iu-*` 安全投影为 exact `document_id + ordinal + source lines`，内部 hash 仍
  保留在 debug，coverage/authority 严格度没有降低。生产逻辑没有 V2.3、疾病、Skill/session/worker、
  固定角色数、route、数据库名或报告名特判。
- Round 16 直接受影响组合 `268 passed`，扩展高风险组合 `543 passed`。生产 Harness image 内 full
  discovery 为 `1937 tests, 2 environment-assembly errors, 5 skipped`；两个 error 分别是只挂载 Harness
  时缺 `/executor` 和缺 Backend workspace-lock parity 文件。按真实服务布局挂载后 workspace-lock 项通过，
  isolated executor 44 项中 43 项直接通过，唯一 CommonJS 项只因 Harness image 不预装 Node；精确挂载
  生产宿主 Node 后该项通过。因此没有逻辑回归。clean archive 与 Git tree 均为 22,456 files。生产
  Harness image `sha256:75aa609858a9c8d24dd447b1d8565dbdccaf05378cb3123c8c377aa3ba655b9b`，
  revision 精确为 `8097db3c...`，healthy/restart 0，旧镜像为 `rollback-pre-8097db3c`；三入口、
  Harness 内部、Backend→Harness、storage identity、SQLite quick/FK、idle AgentRun 与严重日志 smoke
  全通过。
- Round 17 是下一项，继续使用 `shaiengine_deepseek_v4_pro`，必须先全新 V2.3、再全新肺癌 MDT，
  两个 root 顺序运行且各自达到唯一 durable terminal 后，按同一诊断/复现/`claude-code/` 对照/
  通用修复/回归/commit/clean-deploy 闭环推进。当前用户授权上限仍是 Round 18。
- Round 14 历史状态：已完成两个全新顺序 E2E、exact Skill/对话/debug/tool/provider/
  artifact 三源诊断、通用修复、全量回归、本地代码 commit 与生产切换。V2.3 conversation
  `ad60a1cd11fc448e844c8198080d2ccc` / root `9f4747b4fbe348ef8d5b61d0a923e589` 的唯一失败 child
  `c42014306d01498b9f3e299eaef98910` 已有 6 个成功 HTTP receipt，却在 tools-closed final synthesis
  turn 遇到 provider foreign tool 幻觉；坏批次派发 0，但旧 Harness 没有转入已有 post-dispatch
  synthesis。肺癌 MDT conversation `2ad4efc9047748558006dd1026832d28` / root
  `80ab4ffa71a34f008c9932c4bd0f319a` 在前三次 typed plan submission 依次纠正 duplicate selection、
  `round=0` schema error 和仅一个 instruction unit 未映射后耗尽旧三次上限，执行 grant 从未安装。
  逐 child、请求体、stream fragment、compiler path 和 artifact 证据见 `E2E_ITERATION_LOG.md` Round 14。
- Round 14 通用代码提交为
  `cfc0e09d62ff98c2d831dbf0895c9b358fd01a60 fix: recover typed workflows across provider faults`：
  未暴露/非法 tool call 仍整批丢弃且绝不执行；delegated run 仅在已有提交 receipt、无 pending mandatory
  frontier、仍有预算时允许一次 tools-closed synthesis，不重开 schema、不保存坏正文/reasoning/fragment。
  capability-plan schema 与 semantic compiler feedback 现共用五次有限 transaction，成功才原子安装
  grant，连续五次错误仍 fail closed；validator、coverage 和 authority 均未放松。生产逻辑没有 V2.3、
  疾病、Skill/session/worker、固定角色数、route 或报告名特判。
- Round 14 聚焦为 `5 passed`，受影响联合为 `556 passed, 155 subtests passed`。完整隔离主体为
  `1971 passed, 810 subtests passed`；bubblewrap 用户命名空间的两个 trusted launcher 与一个
  `setgroups` 环境项在真实宿主对应为 `2 passed, 1 skipped`，即当前 1,973 项可执行逻辑全部通过。
  宿主 full 的 19 个 failure 全为既有不可读生产 tombstone，隔离精确复跑为
  `13 passed, 9 subtests passed`。clean archive 与 Git tree 均为 22,456 files。生产 Harness image
  `sha256:d05f6f92ae094e0a7f4fc43d2f09bd175316a7484a1b9d8846c8640462b2397d`，revision 精确为
  `cfc0e09d...`，healthy/restart 0，旧镜像为 `rollback-pre-cfc0e09d`；三入口、Harness 内部、
  Backend→Harness、storage identity、SQLite quick/FK、idle AgentRun 和严重日志 smoke 全通过。
- Round 13 历史状态：已完成两个新的顺序 E2E、三源诊断、通用修复、全量回归、
  本地代码 commit 和生产切换。V2.3 conversation `2ca049506d0249418815b64bab500ead` / root
  `5e635b2d7e4b4486bdeb37d88690d34b` 暴露“schema-valid structured tool call 内字段类型错误但旧
  output validator 只给一次提交”的通用缺陷；肺癌 MDT conversation
  `7143d3304a6643c6aa3ff888d63a56d6` / root `01236e10499d43898c0a1ab96cbe4598`
  虽生成 75,337-byte 报告并 durable succeeded，却有 0 child/0 `delegate_task`，暴露显式
  fan-out/fan-in Skill 在 progressive 路径未进入 semantic Workflow IR、动态 boundary 漏装 mandatory
  receipt groups 的通用缺陷。完整 exact Skill/对话/debug/tool/result/artifact 证据见
  `E2E_ITERATION_LOG.md` Round 13。
- 通用修复代码提交为
  `d23c7e4387d43709086e07d7b3f52bc33bcaaf57 fix: validate structured results and explicit agent workflows`：
  delegated typed output 现在是最多 5 次、带 validator feedback、零 registry dispatch/零副作用重放的
  独立 transaction；portable Skill 若结构上明确声明多角色独立/并行执行与汇总/共识，则无论生产默认
  progressive/legacy 都进入已有 content-addressed semantic Workflow IR；动态 capability boundary 原子
  安装 tools、required groups 和 missing requirements。生产逻辑没有 V2.3、疾病、Skill/session/worker、
  固定角色数或报告文件名特判。
- 受影响组合为 `444 passed, 134 subtests passed`；完整隔离回归为
  `1970 passed, 1 skipped, 810 subtests passed`，bubblewrap 用户命名空间内仅两个 trusted
  `/usr/bin/prlimit` 环境校验项失败，同两项在真实宿主 namespace `2 passed`，所以 1,972 项逻辑覆盖
  全部通过。clean archive 与 Git tree 均为 22,456 files。生产 Harness 当前 image 为
  `sha256:c2713d3c08056d549e0d7b5080de561c4d431e12322269a34763a71c60e53ed6`，revision 精确为
  `d23c7e43...`，healthy/restart 0；旧镜像为 `rollback-pre-d23c7e43`。三入口、Harness 内部、
  Backend→Harness、storage identity、SQLite quick/FK、idle AgentRun 和严重日志 smoke 全部通过。
- 用户提供的第二个 `claude-code/` 仓库是本地唯一成熟 Harness 参考；是否官方不作为本项目选择
  参考路径的条件。对照只以冻结 commit 中实际存在的代码为证据，stub 不算实现证据，ChatDS 继续
  采用 clean-room 的独立实现。此前从该仓库吸收的通用不变量是：一次 assistant tool-call batch
  与其全部 tool result 构成不可拆分的 Provider API round；普通 guidance 必须在整批 result 之后；
  发送前必须审计；历史修复不能伪造成功或副作用；压缩只能按完整 round 切割。
- 本日较早的 provider transcript 通用修复提交为
  `b38390c5ef83f2f7ddc52c5b2c70e324017a7583 fix: make provider tool rounds transactional`。
  新增独立 `provider_transcript` 协议层，统一执行 transcript audit、旧历史 canonicalization、
  whole-round compaction boundary、active batch fail-closed close，以及 outbound-only tool-call ID
  唯一化。Agent loop 先提交同一批全部 tool result，再追加 workflow/Knowledge Gate guidance；五类
  pre-dispatch fail-closed 路径都会给尚未派发的 call 写明确的本地 aborted receipt（
  `request_sent=false`、`actual_dispatch_attempted=false`），不会伪造工具成功。Chat Completions
  transport 在任何 SDK 请求前做最后一层严格审计；durable history、artifact receipt 与副作用身份
  不因兼容严格 Provider 的 outbound ID 投影而改变。生产逻辑没有 V2.3、疾病、报告名、Skill、
  session 或 worker 特判。
- 该较早提交从隔离的 workspace/data root 完成全部 Harness 回归：
  `1965 passed, 1 skipped, 809 subtests passed`；唯一 skip 是既有环境条件，3 条 warning 是 Python
  multiprocessing/fork deprecation。三组扩展回归分别为 `44 passed, 49 subtests`、
  `269 passed, 86 subtests`、`278 passed, 127 subtests`；最后一组在默认生产 NFS 根出现的 9 个
  subtest failure 仅由不可读 tombstone 引起，同一 exact test 在隔离根为 `1 passed, 9 subtests`。
  `py_compile`、`git diff --check`、secret scan、genericity scan 与 protected-deletion staging 检查
  均通过。
- `b38390c5` 当时从精确 clean Git archive（tracked/archive 均 22,454 files）构建并只滚动替换生产
  Harness。当时 image 为
  `sha256:099a4fbce03bcfb155dc2b56edff9b6942cfb220f9721d5e4053c7184ba55231`，revision label
  精确匹配完整提交，healthy/restart 0；旧镜像保留为 `rollback-pre-b38390c5-local`。Backend、
  Frontend、四个 Executor、Browser、skill-egress proxy、SearXNG 均未重建。Harness 内部 health、
  Backend→Harness、模型目录（4 个模型）、两块本机地址的 `:5173`、storage identity、SQLite
  quick/FK、idle AgentRun 与部署后严重日志检查全部通过。
- 用户在 Round 13 闭环前再次明确授权从下一轮继续五轮双 Skill 自动 E2E，当前授权范围为
  Round 14--18，替代此前 Round 15 上限。每轮仍顺序运行 V2.3 和 `yangbb` User Skill
  `lung-cancer-mdt` 的全新 conversation/root，并完成三源诊断、通用复现、官方成熟实现对照、
  跨领域修复、回归、本地 commit、clean-archive 部署与生产 smoke。Round 18 是当前硬上限。
- Round 13 较早的首个 V2.3 case 为 conversation
  `a1fb209ffa0f4e7d8135f2959242b1b1` / root `ac3e33dfb62b46ba8a8ee67bff3738c0`，
  约 73 分钟后达到唯一 durable `run.failed/delegate_step_failed`：15 个 child/reducer attempt
  succeeded，只有 Target Biology `945d95019cc746fb86a1058a64b10a3f` 因
  `required_capability_not_attempted` failed；0 业务 Markdown。PICO 首个 reducer 的 37,574-byte /
  9,440-token 完整结果实际能被 10,330-token/676,388-byte downstream consumer 消费，却被历史
  31,457-byte 静态 semantic ceiling 拒绝并做了一次无必要 complete replacement。Target 的最后
  `skill_http_get` 实际 HTTP 200，但 shared NCBI bridge 没有 model-visible candidate handle，handler
  回执匹配 `clinvar-database` 后 maximum matching 将成功调用重新记给已完成组，唯一 pending group
  未前进；这不是网络、timeout、沙箱或 provider stream failure。完整 Skill/对话/debug/tool/result
  三源与逐 attempt 证据见 `E2E_ITERATION_LOG.md` Round 13。
- 上述通用修复已提交并部署为
  `98882f0b18abed5b207c520b3b63ab852a93bc6d fix: bind exact evidence calls and fan-in capacity`：
  pending Knowledge Gate 的 HTTP schema 动态要求 exact `candidate_id` enum；pre-dispatch 验证其仍
  pending 且命中 URL/method，再把 call-local ToolContext 缩窄到唯一已有 grant，receipt 只能记给
  bound candidate。无 gate 的 HTTP 保持兼容，唯一坐标可安全 auto-bind，歧义/完成/无效 handle 在发网
  前 typed reject。fan-in accepted token/byte envelope 改由 downstream 与 provider capacity 推导，wire
  generation reserve 仍独立；未知 provider、coverage、manifest、token/byte 双校验均保持 fail closed。
  delegated exact HTTP request 得到稳定 4xx 后会阻止同参真实 replay，但 408/409/425/429、5xx、transport
  与 changed args/candidate 仍可尝试。生产代码没有疾病、V2.3、Skill/session/worker/KG ID、数据库、
  固定数值或报告文件名特判。
- 该 Round 13 第一阶段的生产 Harness image 曾为
  `sha256:5536a15f50658dec43090db9c6a7e8ef419f29095709d90e28e2a26c74b8ec14`，revision 精确为
  `98882f0b...`，healthy/restart 0；Backend 仍为 `0108c664`。clean archive
  `/tmp/chat_ds_deploy_98882f0b.cU1tKE` 与 tracked tree 均为 22,452 files。受影响组合为
  `399 passed, 209 subtests`；隔离 tmpfs 完整 Harness 为 1939 passed + 唯一无 Node 的 CommonJS
  环境项，该 exact test 在宿主 Node v22.23.1 passed，覆盖全部 1,940 项；candidate 组合为
  `398 passed, 1 skipped, 205 subtests`，唯一 skip 是 clean Git archive 不含未跟踪 reference ZIP。
  部署后三入口、Harness/Backend health/models、storage identity、SQLite/FK、严重日志均正常；生产
  GLM-5.2 thinking smoke 为 200、reasoning 非空、terminal stop。
- Round 13 的上述第一阶段后来已由 `2ca049...` V2.3 与 `7143d3...` 肺癌 MDT 两个全新 case、
  `d23c7e43` 通用修复和生产部署闭环；不得再把 `a1fb...` 或这两个已终态 run 复用为新轮。
- Round 12 已完成。V2.3 `9bb4a0173fc44c5b94cb4258b2a17ab7` / root
  `f96df86c12744cc5bd4cafc176ec6a8f` 完成 intent、7 路 bootstrap 和除 PICO 外的全部
  worker；PICO 的首次与唯一 clean retry 均在 0 provider token 前触发同一确定性内部错误：独立
  reducer 预算能一次预载全部前序结果，但旧 fan-in output allowance 用两个短 ID 的虚拟 artifact
  估算元数据，真实 leaf 携带更多、更长的 immediate source IDs，最终 child 校验无法容纳规划器自己
  批准的 artifact。该 case 为 14 succeeded child attempts、2 failed attempts、0 artifact；不是网络、
  provider、沙箱、timeout 或用户断线。肺癌 MDT `265ffb56b04141fe99e1281ab2811e7d` / root
  `424100dd5ffd4d10afbc1224f1a7f877` 在 semantic plan accepted 后、0 child/0 artifact 时失败：
  worker `overview` 的普通 capability 精确指向 `SKILL.md`，文件资源仍在 authority 中，但 native-only
  plan 安装丢失了冻结根包 digest，runtime compiler 因无法构成 exact file+package identity 而正确
  fail closed。两项均通过 conversation、exact Skill、root/child debug、tool event 与 result spill
  三源关联后定位。
- Round 12 通用修复提交为
  `0406ab72ae48069f923304798f4b34003b82c107 fix: bind semantic roots and account fan-in metadata`：
  fan-in planner v3/output policy v4 从实际 source partition 构造与执行完全相同的 leaf/balanced-tree
  metadata envelope，逐 final/merge request 计算 token/byte body 上限；固定宽 placeholder/final plan ID
  保持 content-address stability，不增加预算、不截断来源。standard semantic plan 安装时重新验证
  run-frozen root snapshot，并始终保留且只保留 exact `SKILL.md` 与完整 package digest；不授予 sibling、
  directory 或 glob。production diff 没有疾病、V2.3、Skill/session/worker/文件名或固定数量特判。
- Round 12 定向组合为 `133 passed, 22 subtests passed`，跨域/契约扩展组合通过；完整 clean tmpfs
  Harness 为 1929 passed + 唯一 CommonJS runtime 环境项。该项因 Harness image 按设计不预装 Node
  而失败，在宿主 Node 22.23.1 单独 `1 passed`，因此全部 1930 项逻辑覆盖通过。clean candidate 同一
  133+22 通过；`py_compile`、diff、secret、genericity 与 protected-deletion 检查通过。
- `0406ab72` 当时从精确 clean archive `/tmp/chat_ds_deploy_0406ab72.fclvYr`（22,452 tracked files）
  构建并只替换生产 Harness。当时 image 为
  `sha256:48dfa72457b2db76284a18f4bf11f241c354b218241825227f902f9e63cfcbad`，revision 精确匹配，
  healthy/restart 0；Backend 保持 `0108c664`。三入口现均 200，容器内/Backend→Harness、models、
  storage identity、SQLite/FK/idle root/schedule 和严重日志 smoke 均通过；旧 Harness image 保留
  `rollback-pre-0406ab72`。该状态随后已由 Round 13 前置修复和 `98882f0b` 生产切换取代。
- Round 11 已完成。V2.3 `49791ec4ef37449c84b7c1611e256a06` / root
  `b75a71b3dbdd48f58dd76ec31a4a3b46` 在 7 路 bootstrap 的最后一项
  `competitive_intel` 重试中，第一次因无 evidence receipt 却填充 typed facts 被正确拒绝，
  第二次仅因模型输出两个严格合法的 `COMPLETION_QUALITY_JSON` 页脚而失败；肺癌 MDT
  `b830029d282447cf8abcce196c7d6b41` / root
  `941e09a080694159ac6d45c205b2d7e0` 在计划第三次通过后、零 worker dispatch 前，因计划中的
  exact same-package 资源路径没有进入 runtime selected-resource closure 而安装失败。两者都不是
  共同网络、沙箱或用户断线问题，且均为 0 artifact。
- Round 11 通用修复提交为
  `ca9f5eac235cb924d3860826482df032d2a542fb fix: bind planned resources and canonicalize child quality`：
  path-shaped ordinary worker selectors 只能从同一冻结 package 精确解析为 digest-bound 只读资源，
  不授予目录/glob authority；完整 selected-resource closure 在提交前重新执行 256 项硬上限。
  delegated typed terminal 对多个严格合法的 completion-quality ledger 做保守 canonicalization，
  `degraded` 胜出；任一 malformed ledger、无 evidence receipt 的 populated facts 或 machine/prose
  completion 冲突仍 fail closed。生产代码没有疾病、V2.3、Skill/session/worker/文件名或固定数量特判。
- Round 11 受影响组合为 `260 passed, 182 subtests passed`；隔离完整 Harness 为
  `1929 passed, 3 warnings`。默认宿主根下 19 个失败均由不可读生产 NFS tombstone 在被测逻辑前
  触发，不是代码回归。clean candidate 中受影响组合为 `259 passed, 1 skipped`，唯一 skip 是
  clean Git archive 按约束不包含未跟踪 V2.3 reference archive；`py_compile`、diff、secret、
  genericity 与 protected-deletion 检查均通过。
- `ca9f5eac` 已从精确 clean archive `/tmp/chat_ds_deploy_ca9f5eac.paRTS7` 构建并只替换生产
  Harness。当前 Harness image 为
  `sha256:c5b07eabae3e4a8af182965c9c0268558e4c37e87647e9e13d4131375b61282d`，revision label
  精确匹配完整提交，healthy/restart 0；Backend 保持兼容的 `0108c664`。三入口、容器内与
  Backend→Harness health/models、storage identity、SQLite/FK/idle terminal/schedule 和严重日志
  smoke 均通过，旧 Harness image 保留 `rollback-pre-ca9f5eac`。Round 12 是下一项已授权测试。
- Round 10 的两个独立 case 已到唯一 durable failed terminal并完成三源诊断。V2.3
  `bc632e897c384f34bfec3433fd477bbe` / root
  `d66b7e4017234ff1853fa7f35dc9224f` 的前序 worker 均成功，最终 I/E child 在 required
  predecessor fan-in 中被旧固定 8K output/240 秒 reducer step 截断；肺癌 MDT
  `cb7515fad602405da4b873ccc37a9ecc` / root
  `09b907e90e534e139bf81424220d3abb` 在零 dispatch 前因 provider schema 要求模型复制 opaque
  instruction ID，三次分别产生 unknown、overlap 与 hallucinated ID 后 fail closed。它们不是共同
  网络、沙箱、浏览器或用户断线问题。
- Round 10 通用修复已提交为
  `45e131e3422dbb611ea79b3578dda8d5ad65ae82 fix: bound generic planning and fan-in lifecycles`：
  provider planner 只见 frozen snapshot-local `document_id + ordinal range` 和 exact enum/const
  catalog identity，runtime 再 late-bind canonical instruction；simple Skill 不暴露空 workflow
  schema。fan-in reducer 拥有独立 32 KiB-aware output reserve、provider-budget deadline、weighted
  admission、Schedule-to-Close、attempt run/authoritative terminal 与仅一次 pure-output complete
  replacement；ordinary artifact verifier 不再进入 reducer 生命周期。Backend 将 reducer attempts
  持久化为 nested delegate，不覆盖 primary/root。生产逻辑没有业务、疾病、Skill/session、worker、
  文件名、固定数量或 93,375-token 样本特判。
- 用户随后要求把 Shaiengine `glm-5.2` 与 `deepseek-v4-pro` 加入模型目录，并把前者设为默认测试
  模型。OpenAI 与 Anthropic 两个兼容面均已实测；OpenAI stream 能稳定保留 reasoning、tool-call
  fragments、usage 和 thinking enabled/disabled，Anthropic tools stream 对 disabled 仍发送
  `thinking_delta`，所以生产主 route 使用 OpenAI compatibility。接入提交为
  `0108c664443665b5748f2c3933f420ac79f9190d feat: add compatible remote agent models`；
  `shaiengine_glm_5_2` 是唯一新默认，历史 `AgentModel` 继续精确绑定本地
  `deepseek_v4_pro`，不会因默认变化重绑旧会话。凭据只存在于权限 0600 的部署配置/受限 secret，
  未进入 Git、文档、日志或 debug。
- `0108c664` 已从精确 22,452-file clean archive 构建并只替换生产 Harness/Backend。镜像分别为
  `sha256:10d65e46efb53a7698a92d2c4835f149131e485bce5855276aff56cf6af457a8`、
  `sha256:1adb71c272df3b3f52cec172e4df7cbdac24d9b8c6d877e7fe9be841c5505b3d`，revision label
  精确匹配完整提交，healthy/restart 0。三入口、Backend→Harness、两模型生产请求、storage
  identity、SQLite/FK/idle terminal/schedule 和严重日志 smoke 全部通过；旧镜像保留
  `rollback-pre-0108c664`。Backend 全量 `237 passed`；隔离 Harness `1925 passed, 1 deselected,
  800 subtests passed`，唯一 deselected CommonJS 环境项在宿主 Node 22.23.1 单独 passed，组合覆盖
  全部 1,926 项。
- Round 10 后的暂停要求已被用户最新五轮双 Skill 明确授权替代；不得复用旧 run，也不得并发运行
  同一轮两个根任务。当前从已部署的 `0406ab72` 开始 Round 13。
- Round 9 的两个 case 均已到唯一 durable failed terminal；该轮开始时生产仍为 `1d2b7d9c`。V2.3 case
  `24239b8bef374c8e9663a0849adafa05` / root
  `0d3a0e9ee41e4153b129cbc4728d7761` 已于 2026-08-02 07:00:36 UTC 到唯一 durable
  failed terminal：14 个 child succeeded，Literature synthesis 的复杂 typed footer 在旧实现中
  从原始 16K 预算错误降到 8K finalizer，形成被截断的 28,662-character JSON tool arguments；
  required barrier 正确 fail closed，没有最终报告。肺癌 MDT case
  `4667d323114c4cce94faf861a6ea4347` / root
  `1b8e7dcde41243558178463da601a60a` 已于 2026-08-02 19:54:21 UTC 自然结束：旧版让模型
  反复手写 241-unit 完整 Workflow IR，20 次 deterministic semantic rejection 后仍无独立
  validation/no-progress budget；最终在约 12 小时 47 分、692 万持久 token 后以损坏的第 21 个
  plan call 终止。该 run 为 0 child、0 artifact、无业务 Markdown；不是网络、沙箱、delegate
  或前端断线问题。
- Round 9 通用修复已提交为
  `6657f3741ae0bb399333e5039dd2da994864e84b fix: compile generic skill workflows deterministically`
  并部署生产 Harness。model-facing `workflow_plan` 仅声明
  语义节点、依赖、连续 instruction ranges 和额外 capability；Harness 从冻结 source/catalog
  确定性编译完整 Workflow IR，注入 mandatory delegate，派生 coverage/result/output/policy/count/
  digest 并复用严格 validator。control tool 只有 typed accepted 才推进 frontier；同一 plan 三次
  semantic rejection 后以稳定 code/path durable fail closed；accepted full IR 不回灌模型历史。
  handler-level accepted 之后还必须完成 frozen-catalog revalidation 和 profile-bound runtime
  preflight，authority 真正原子安装后才消费 plan frontier；安装失败同样返回稳定 receipt 并受
  独立 runtime-install controller 立即 fail closed，不再冒充模型可纠正的 semantic retry。
  catalog amendment 以 digest 作为新 planning epoch：旧 plan/worker/tool authority 全部撤销，
  只有候选定义与 SHA 完全一致的成功只读 resource receipt 可以迁移；新 plan commit 前保持
  plan-only surface。handler 到 installer 之间会再次核验 live Skill authority，所有 runtime
  projection 先在局部 candidate 中派生，最后一次性提交，失败不会产生 `tool.completed` 或半安装
  authority。
  child 与唯一 footer finalizer 共享按 result schema 复杂度计算的 8K/16K/32K budget，terminal
  payload usage 与独立 usage event 单调幂等合并，authoritative child terminal 绑定排序后的
  artifact manifest/count/SHA。终审进一步把 field lexical、128 fields/256-character name/16 KiB
  UTF-8 exact schema projection 提升为 compile/install/legacy dispatch 前共享边界；Workflow IR
  worker/aggregation 都只消费 exact direct predecessor，wave 只是 readiness barrier，不再读取独立
  兄弟分支；`run_skill_process` 的 sync/close 文件也进入与 script/python/command 相同的 artifact
  receipt/terminal manifest 链。实现没有疾病、V2.3、Skill/session/worker/KG、文件名或固定图
  数量特判。终态审计又补上统一 planning/verifier phase boundary：catalog 已发布但 required
  typed plan 未原子安装时，普通 stop、length 和 iteration-budget terminal 都保留 pending-plan
  workflow 原因，artifact verifier 不得提前运行；post-tool closure 也不能用通用 continuation
  重新扩张编译器已收窄的 planner-only surface。完全披露后既无 executable candidate、也无
  delegated workflow 的纯指令 Skill 则直接关闭工具面并遵循正文，不制造无权限收益的空 plan call。
- 本轮终审后的扩展 changed-path 为 486/486 passed（其中 1 项预期 skip）。隔离 workspace/data
  root 且包含 sibling executor module 的 full Harness 共枚举 1,906 项，唯一错误是 clean Harness
  image 按设计不含 Node 造成的 CommonJS 环境 holdout；把宿主 Node 22.23.1 注入同一隔离容器后
  该 exact holdout 单独 passed，因此组合证据覆盖全部 1,906 项且没有代码失败。`py_compile`、
  `git diff --check` 与 diff-only genericity scan 通过。默认生产 NFS 根下的失败仍是不可读 tombstone
  与错误 `PYTHONPATH` 的环境噪声，不是代码回归。肺癌与
  V2.3 冻结 instruction catalog 的 compact/full 比分别为 35.73% 和 34.5%。官方对照已扩展到
  Deep Agents、Codex、OpenClaw、Hermes、Claude Code、Pydantic AI、OpenAI Agents、LangGraph、
  Inspect AI、AutoGen、Temporal Python SDK、Semantic Kernel 与 OpenHands；结论是保留现有
  Harness，采用小型计划投影、运行时确定性展开、精确 predecessor/attempt receipt、独立
  subagent/validation/execution retry budget 和 durable run/events 模式，不整体换栈。Temporal
  的 Workflow history、OpenHands 的 immutable action/observation event 和 Semantic Kernel 的
  typed process/SSRF validator 仅作边界参考；三者都不能替代现有 Skill compiler、artifact
  receipt、session sandbox 与统一 egress。完整证据见
  `E2E_ITERATION_LOG.md` Round 9。生产 Harness image 为
  `sha256:3fbcb23d2c26dbf70fd5469faea7a3418db02faa7d53428b83a392ac79ed5d8a`，revision
  label 精确匹配 `6657f374`，healthy/restart 0；Backend 保持 `1d2b7d9c`，三入口和内部
  health/models 全 200，两端 storage identity 相同，数据库健康空闲。旧 Harness image 保留为
  `rollback-pre-6657f374`。
- 2026-08-02 用户在 Round 8 闭环后明确追加 5 轮自动 V2.3 E2E。新授权覆盖
  Round 9--13，并替代旧的“不得创建 Round 9”限制；每轮仍必须使用全新
  conversation/root，完整执行三源诊断、成熟方案对照、通用复现、回归、本地 commit、
  clean-archive 部署与生产 smoke。用户随后更正：全部五轮 Round 9--13 每轮都运行两个
  独立用例——V2.3 与 `yangbb` 账户 User Skill registry 中的肺癌 MDT Skill——各自使用
  全新 conversation/root，分别核对自身 Skill/对话/debug；肺癌用例必须从 User Skill
  registry 冻结 immutable package/resource digest，不能从历史 session 临时目录猜包。
  同一轮的两个 root 顺序运行，避免 provider 容量竞争干扰归因，
  且不能把两种业务或夹具写成生产特判。Round 13 是本次追加授权的硬上限。
- 自动 E2E campaign Round 8（原八轮 campaign 的最终轮）为 Conversation
  `9ff98843e980458d832629ba9964ec96` / root
  `ad98fb353fb240f2b3ab84f345ceb247`。它运行约 3 小时 3 分并从 SSE 收到唯一 durable
  failed terminal；exact Skill 正确选择
  `healthsim-trialsim/composite_full_protocol_design`，intent、7 路 bootstrap、PICO、Safety、
  Termination 和 Competitive deep-analysis 共 11 个 child 成功。Target deep-analysis 因
  433,287-byte 完整 response 在旧 400K producer ceiling 被先截断而失败；AE 的
  tools-closed final synthesis 又被先前 spill handle 动态追加的 `read_tool_result` schema
  重新打开，92,526 字符 stop body 的 malformed footer 因 phase-incompatible gate 未获得
  独立 output finalizer。required barrier 正确 fail closed，fan-in、模块报告和 strong-final
  未启动；没有报告 Markdown。
- Round 8 通用修复提交
  `1d2b7d9ce412f58e9d21acf6f18a56c1ebef419d fix: preserve generic terminal workflow phases`：
  GET/POST 完整 wire capture 与 5 MiB lossless store 使用同一 hard ceiling，较小 `max_chars`
  仍只控制 inline 展示；terminal retrieval gap 在 exact sibling frontier 结算前只持久化/defer，
  不抢唯一 degraded fan-in；动态回读能力服从当前 phase policy，footer unavailable debug
  输出具体 incompatible reasons。确定性回归使用非临床 inventory Skill，没有 V2.3、疾病、
  包/session/route/worker/KG/文件名或固定数量特判。
- Round 8 Attempt A 在零模型 dispatch 前发现 Harness data bind 与 canonical host root 不一致。
  永久闭环父提交
  `c3f9f582d246d6e63c0af2a6f60e471b9c628267 fix: attest shared storage across services`：
  Backend/Harness health 发布 path-free dev/inode identity，Backend 严格比对，Compose 强制
  canonical data/memory roots 并禁止静默创建错误 bind source。
- Round 8 聚焦为 `137 passed, 62 subtests passed`。完整隔离 Harness 为 1,877 项：
  1,871 通过、5 项资源型 skip，唯一 Harness-image-without-Node holdout 在宿主 Node 22.23.1
  下单独通过，组合为 1,872 pass + 5 skip。Backend 235 项中一个既有 multiprocessing
  timing assertion 首轮抖动，单项复跑通过。`py_compile`、diff、secret 与 genericity scan
  通过。
- `1d2b7d9c` 已从 clean archive `/tmp/chat_ds_deploy_1d2b7d9c.lBwXUs` 构建并按
  Harness -> Backend 顺序部署。两镜像分别为
  `sha256:d335a4d9afd8becc19ae797330cd0c8f13ebd15128207b7f2ec591e1ac3a3d75`、
  `sha256:c763e8e9d55875117a9a7fa54b9242e5923d23cf77315118229f6ca73c5ba501`；revision
  label 均为完整提交，旧镜像保留 `rollback-pre-1d2b7d9c`。三入口、Backend->Harness
  health/models 全 200，两端 storage identity 相同，restart 0、严重日志 0、数据库健康空闲。
  该轮是原八轮 campaign 的最终轮；用户随后已明确授权继续 Round 9--13。
- 自动 E2E campaign Round 7 为 Conversation
  `67119645fa874ecba689c8a61e3874de` / root
  `5e494f191ead47a6ad640295cd48e36e`。它从 2026-08-01 17:37:13 到 20:17:14 UTC
  连续运行约 2 小时 40 分并收到明确 durable failed terminal；exact Skill 正确选择
  `composite_full_protocol_design`，完成 intent、7 路 bootstrap、PICO/Safety/Termination
  与 Target/Competitive worker。唯一 AE worker 因完整约 191.5K-char HTTP wire body 在
  `skill_http_get` producer 内先截成 100K，后续旧 wrapper 只能保存已截断 JSON；该 minified
  payload 又没有安全分页坐标，最终以
  `response_exceeds_visible_limit_no_safe_page_window` fail closed。I/E、Literature、fan-in、
  模块和 strong-final 未启动，Artifact row 为 0。它不是 timeout、断线、provider corrupt、
  沙箱缺失或共同网络故障。
- Round 7 通用修复提交
  `064391529b767a2bb0228a5e74088d4572ad37c0 fix: spill oversized tool results losslessly`：
  任意大文本工具结果在 producer/middleware 边界先无损 spill，再给模型 preview + runtime-owned
  opaque handle；`read_tool_result` 只在本 run 产生 handle 后动态暴露，支持 bounded
  offset/from-end/literal-pattern 回读。GET/POST 的完整 wire body 与 inline presentation
  truncation 分权；真实 pagination 仍独立保持 open。句柄受 user/session/run ledger、dirfd、
  `O_NOFOLLOW`、0600、UID、单硬链接、常规文件和 5 MiB 上限约束，且不参与 Skill/KG
  candidate、mandatory/no-progress 或 mutation 计账。生产代码没有 V2.3、疾病、包、route、
  worker、session、文件名或固定数量特判。
- Round 7 changed-path 为 `268 passed`，宽组合为 `401 passed, 1 skipped`；隔离全量 1,870
  项中 1,864 通过、5 项因未挂 runtime/reference assets 跳过，唯一 Node/CommonJS 环境 holdout
  在宿主 Node 22.23.1 下单独 `1 passed`。`py_compile`、diff、secret 与 genericity scan
  通过。成熟方案对照采用 Pydantic AI Harness 的 lossless Spill/readback 边界，并对照
  Deep Agents thread-scoped backend、LangGraph pending writes、Temporal Activity、OpenAI
  tracing 与 AutoGen state；保留现有 Harness 主循环与 authority/receipt/terminal 主链。
- `06439152` 已从 clean archive `/tmp/chat_ds_deploy_06439152.LEJAcb` 构建并只替换生产
  Harness。当前 image 为
  `sha256:63ddfc85f83dc8aa1d89fc2e51ec80dba42831df6546370f8670a7e9cfdbe95b`，revision label
  精确匹配完整提交，旧镜像保留为 `rollback-pre-06439152`。Harness healthy/restart 0；
  三入口、Harness 与 Backend→Harness health/models 全 200；新回读工具已注册，严重日志 0，
  SQLite quick_check/FK 正常且生产空闲。该部署随后用于全新 conversation/root 的 Round 8；
  Round 8 已完成并成为原八轮 campaign 的最后一轮模型重型 E2E。
- 自动 E2E campaign Round 6 为 Conversation
  `862eb37670634f5394fab116429fa948` / root
  `88d0fd14ec01449cace347fcde4d6858`。它从 SSE 收到明确 durable failed terminal；intent 与
  ClinicalTrials/PubMed/ICH/FDA/EMA/Target Biology bootstrap 均完成，唯一 Competitive
  bootstrap 两次未通过 output contract。exact Skill 的 `drugbank-database` 是需账户/许可、
  只有说明而无 MCP/脚本/HTTP/command bridge 的 supporting Skill，所以 child 没有 evidence
  receipt。第一次模型虽声明 degraded，却填入 7 个未验证字段而被正确拒绝；第二次改成全 null，
  但只给 legacy prose status，没有 exact `COMPLETION_QUALITY_JSON`，父级重试耗尽。
- Round 6 的通用根因是父级 retry 只重新采样相同 task，没有携带上一 attempt 已持久化的
  validator finding。修复提交
  `70df8b51a34fa767c8cf3badb87b14449c76e872 fix: carry validator feedback into delegate retries`：
  所有 declared delegate 类型的唯一 retry 现在附带 bounded、脱敏、Harness-owned 的
  attempt/terminal reason/failure class/validator error 数据；失败正文仍不进入下一 child，工具、
  schema、资源、Skill authority、重试次数和预算不变。没有 V2.3、疾病、包、source、worker、
  route、session、文件名或固定数量特判。
- Round 6 聚焦双根隔离回归为 `290 passed, 188 subtests passed`；Harness 全量为
  `1862 passed, 1 skipped, 782 subtests passed`。默认 NFS 下唯一红灯仍是 root-owned tombstone
  在 provider stream 前阻断，双根隔离单项及全量均通过。`py_compile`、diff、secret 与
  genericity scan 通过。
- `70df8b51` 已从 clean archive `/tmp/chat_ds_deploy_70df8b51.WprOt9` 构建并只替换生产
  Harness。当前 image 为
  `sha256:3d328d1af220fc51531fe9544685e728fc8eecf047d90686be76339c2323bb1b`，revision label
  精确匹配完整提交，旧镜像保留为 `rollback-pre-70df8b51`。Harness healthy/restart 0；三入口、
  Harness 与 Backend→Harness health/models 全 200；严重启动日志 0，数据库健康且生产空闲。
- 自动 E2E campaign 的 Round 5 会话为
  `c8d53cd3f6904e90b88640a9125b7c0b`，root 为
  `6421809b83be4d53a698ddfee550b01c`。它在生产连续运行约 6 小时 26 分后达到唯一
  durable failed terminal；不是浏览器断开、统一沙箱缺失、共同网络故障或 Harness
  timeout。exact Skill 正确选择 `composite_full_protocol_design`，完成 intent、7 路
  bootstrap、PICO/Safety/Termination，以及 AE/Target/Competitive wave；Target worker
  在 22 轮完成约 40.6K 字符正文后触发一次 length continuation，第 23 轮完成约 7.7K
  字符续写，却因主 iteration budget 同时耗尽而没有机会执行 typed-output finalizer。
  I/E、Literature、fan-in、11 个模块、strong-final 与 post-merge verifier 因 required
  worker barrier 未通过而未启动，Artifact row 为 0。
- Round 5 同时发现一个被旧校验误报为 succeeded/degraded 的 AE worker：其正文停在
  `Let me read...`，随后输出 GLM escaped pseudo-call，footer projector 又把 13 个字段
  全部填成空对象/空数组。旧 raw-protocol regex 未识别 `tool_name\":{...}` 方言，粗粒度
  JSON Schema 又把全空 ledger 当成合法完成。Competitive worker 的同批结果为实质性
  typed output，说明不是该 wave 的共同网络或调度故障。
- 通用修复提交为
  `36e8ea43dffe2fd29e3d20a372313f91bf2decfb fix: finalize delegated typed results independently`：
  typed footer projection 现在拥有独立、严格一次的 output-validation slot，不扩大普通
  推理/工具迭代；length continuation 与事务性坏 footer 撤销共用同一清洗前缀；raw
  pseudo-tool audit 覆盖 escaped JSON-key 方言；所有 required fields 均为空时，只有带
  明确 zero-result 或 degraded/gap 解释的实质正文才允许完成。无 V2.3、疾病、包、route、
  worker/KG、session、文件名或固定数量特判。
- Round 5 聚焦回归为 `320 passed, 104 subtests passed`；隔离 workspace/SANDBOX root
  下 Harness 全量为 `1861 passed, 1 skipped, 782 subtests passed`。宿主默认根第一次的
  19 个失败全部由生产 root-owned tombstone 在被测逻辑之前 fail closed，受影响的
  13 个测试/9 个子测试在双根隔离后先行全部通过。`py_compile`、diff、secret 与
  genericity 检查通过。
- `36e8ea43` 当时从 clean archive `/tmp/chat_ds_deploy_36e8ea43.lAJHbD` 构建并只替换生产
  Harness。当时 Harness image 为
  `sha256:09072ee7a688907251a5d4e96a94a08c6aeb791b40be7162423982effb77545c`，revision
  label 为完整提交，切换前镜像保留 `rollback-pre-36e8ea43`。容器 healthy/restart 0，
  三个入口、Harness 与 Backend→Harness health/models 均为 200，严重启动日志 0，
  SQLite quick_check/FK、active root 和 running schedule 均正常。
- 2026-07-31 五轮 E2E campaign 的 Round 3 通用修复提交：
  `3987613c fix: scope delegated frontier recovery`。新会话
  `2dcbcfa305084c5a9e11d4a359075054` / root
  `69cbcaacf1174ab4b9d96821e1bfeb7a` 正确执行 intent、7 路 bootstrap 和真实 worker
  wave，最终因 Safety worker 的第三个独立 mandatory group 首次 non-call 被 run-global
  一次性恢复预算误杀而 durable failed。该轮还暴露了 validated worker 的
  `execute_code` 在 Knowledge Gate runtime projection 中被静默删除，以及 Backend 用
  transport `stop` 覆盖权威 root failure reason 两项跨领域契约漂移。
- `3987613c` 将一次纠形预算绑定 exact mandatory-frontier SHA-256；同一 frontier
  二次 non-call 仍 fail closed，receipt 推进后的新 frontier 可获得自己的单次隔离纠正，
  全局 iteration/hard deadline 不变。Knowledge Gate 只为已由 compiler/validator 证明的
  声明式 worker 保留有界本地 `execute_code`，不会给普通/未验证 child 扩权；客户端终态
  reason 改为服从 authoritative root event。
- Round 3 聚焦 `15 passed`；Backend 全量 `224 passed`；Harness 独立 tmpfs 全量为
  `1836 passed, 1 failed, 772 subtests passed`，唯一失败是生产 Harness image 不含 Node，
  同一 CommonJS 用例在宿主 Node 22.23.1 下 `1 passed`。`py_compile`、diff、secret 与
  genericity 检查通过。完整人工追问链、delegate 明细、成熟机制对照和证据见
  `E2E_ITERATION_LOG.md`。
- `3987613c` 已从 clean archive `/tmp/chat_ds_deploy_3987613c.mAmjOI` 构建并只替换
  Harness/Backend。当前镜像分别为
  `sha256:4f15d7e8afd7b579d0ab0c7d19b979af076642f68b70a66d470333d3161630fb` 与
  `sha256:817390d6069315d69aef3bcd471f60d3f91f16ceac8e55cbb3d777127bfd1767`，revision
  都是完整提交 `3987613c43405b0347bc8606260abde078b707ba`，restart 0；三入口与内部
  health/models 为 200，SQLite/foreign key、active run/schedule/connection 均正常。
- 2026-07-31 五轮 E2E campaign 的 Round 2 通用修复提交：
  `aac60951 fix: isolate delegated recovery contracts`。新会话
  `2b1e321d275543de9328c3079259f5a8` / root
  `b64b7cf03538447588965a602fcdf42b` 正确编译并执行 V2.3 workflow，但在 worker barrier
  暴露了四项跨领域缺陷：mandatory non-call/retrieval correction 重放十几万 token 旧历史；
  typed terminal 仍有一条大历史自由正文重写路径；workspace debug 混淆 inner candidate 与
  outer authoritative terminal；root 只显示并行 wave 的一个失败且 Backend 用 transport
  `stop` 覆盖 event finish reason。上游 4xx/429/DNS/TLS 是来源级退化，不是共同根因。
- `aac60951` 将 mandatory no-call 与 corrupt recovery 统一为两消息 machine frontier
  snapshot；有 result schema 的 terminal repair 统一为非 registry 的 exact-one
  `submit_result_fields`，输入是原任务、已 dispatch 工具坐标/result 形成的 48KiB evidence
  capsule；debug 候选终态改名并增加 receipt unique/transition 计数；root terminal 携带所有
  当前失败节点，AgentRun projection 服从权威 event。生产代码和测试没有 V2.3、疾病、
  session/package/worker/KG ID、报告名或固定数量特判。
- Round 2 聚焦组合为 `428 passed, 142 subtests passed`；Harness 全量为
  `1835 passed, 3 warnings, 772 subtests passed`，唯一 Node 环境项在固定
  `/usr/bin/node` 下通过；Backend 主体 `214 passed`，跨组件 mount cohort 复跑
  `47 passed`。`py_compile`、`git diff --check`、secret/genericity scan 通过。完整三源
  诊断、delegate 明细和成熟官方机制对照见 `E2E_ITERATION_LOG.md`。
- `aac60951` 已从 clean archive `/tmp/chat_ds_deploy_aac60951.npJK2J` 构建并只替换
  Harness/Backend。当前镜像分别为
  `sha256:08a4576feee38a6cec6f845ffc1ad9d4e2b07681e0b62f31cb288520d31925d4` 与
  `sha256:ffc8c793cb67cf5fea3219f67575134b494252b63c71592782e6adab48f34cdb`，revision
  都是完整提交 `aac609518430b348a518712136569f94cc7442db`，restart 0；三入口与内部
  health/models 为 200，SQLite/foreign key、active run/schedule/connection 均正常。
- 2026-07-31 五轮 E2E campaign 的 Round 1 通用修复提交：
  `26d65158 fix: isolate exact mandatory capability phases`。它由新会话
  `8314f40fa1a449f88cca55c140df218d` 暴露的跨领域不变量驱动：同名 bridge 必须在
  handler dispatch 前匹配当前 exact candidate coordinate；mandatory corrupt-tool
  recovery 必须使用 machine receipt/frontier 的 phase-isolated request，不能重放已结算
  assistant tool-call/tool-result 历史。生产代码与通用测试没有 V2.3、疾病、Skill/
  session/worker/KG/文件名或固定数量特判。
- `26d65158` 的核心回归为 `272 passed, 52 subtests passed`，宽组合为
  `566 passed, 214 subtests passed`；clean tracked-tree 全量主体为
  `1818 passed, 3 skipped, 759 subtests passed`，生产 NFS 隔离 cohort 与真实 runtime
  Skill fixture 分别复跑 `13 passed, 9 subtests passed` 和 `3 passed`。逐轮证据与成熟
  Harness 对照记录在 `E2E_ITERATION_LOG.md`。
- `26d65158` 已从 clean archive `/tmp/chat_ds_deploy_26d65158.agPdNd` 构建并只替换
  Harness。当前 image 为
  `sha256:1f25a2f577428e3cb7a3c26a734ae98d96cf592f45902f92b32e474eb86164a8`，
  revision 为完整提交 `26d65158e4a0bf52a9e5256a156feec4c5aee20b`，healthy/restart
  0；Backend/Frontend/沙箱/Proxy/Browser/搜索和数据库均未重建。
- 2026-07-31 最新功能提交：
  `2a07218a fix: preserve mandatory delegated evidence frontiers`。它继续以
  `9b1fc851323b477d95e09b3f531c6903` 为压力测试，但生产代码和合成测试没有加入
  V2.3、疾病、Skill/session、worker/KG、文件名或固定数量特判。该提交把
  Knowledge Gate check 状态改为 handler receipt 派生的 canonical ledger；禁止
  corrupt replan、HTTP 分页收尾和 visible-length continuation 抢占仍未满足的 exact
  receipt frontier；任一仍暴露候选工具的 mandatory turn 都保持
  `tool_choice=required`，provider 无调用时只允许一次有界纠正。
- `2a07218a` 的受影响组合回归为 `552 passed, 151 subtests passed`。隔离全量为
  `1826 passed, 2 skipped`，4 个环境型失败已分别在正确环境复跑 `4 passed`：3 项
  使用未跟踪的真实 runtime Skill fixture，1 项使用本机同版 Node 22/CommonJS。
  当前 worktree 全量另为 `1831 passed` 加 Node holdout `1 passed`；宿主直接全量的
  19 个红灯已证明来自测试默认 ID 命中生产 NFS durable tombstone，脱离生产 NFS 后
  全部通过。`py_compile`、`git diff --check`、staged scope/secret/genericity scan 均通过。
- clean archive `/tmp/chat_ds_deploy_2a07218a.ErJsuQ` 构建出的 Harness 候选/生产
  image 为
  `sha256:5e9689d2f0c6926e7e94a3154a451ea972ad1a61d1d5630e2da2b4e5417f2d90`，
  revision 为完整提交 `2a07218a6f59454ec72a21a878f70d486dba2e46`。当前生产
  healthy/restart 0；只替换了 Harness，其他服务和数据卷均未重建。
- 2026-07-31 本轮功能提交：
  `7ce353d3 fix: converge generic delegated skill execution`。它由用户手工
  `9b1fc851323b477d95e09b3f531c6903` E2E 暴露的跨领域不变量驱动，没有加入
  V2.3、疾病、报告名、固定 worker/KG/文件数量或 session 特判。主要闭环是：
  任意同级 `<name>_instructions` + `<name>_output_schema|format` 全量编译并合并
  worker 结果合同；exact capability/Knowledge Gate gap ledger 改为 receipt-owned；
  pending 独立 Knowledge Gate 组优先于已访问 HTTP family 的深分页；child turn 和
  output budget 按声明式 group/result schema 复杂度分级；编译 worker 可使用无网络的
  有界 `execute_code` 完成声明的计算/仿真；GET 仅对 pre-submit DNS/transport 做一次
  deadline 内幂等重试；安全 pre-submit 失败可形成 exact degraded receipt；所有
  exact receipt adapter 共用同一 URL/body-free projection；conditional browser egress
  method/path 规则不再在编译中丢失。
- 本轮受影响组合回归为 `452 passed, 326 subtests passed`。clean Git archive 全量为
  `1823 passed, 2 skipped`，其中最初 4 个失败均已独立归因并在正确 fixture/runtime
  下复跑通过：3 项需要未跟踪的 runtime Skill fixture，1 项需要生产沙箱同版 Node。
  `py_compile`、`git diff --check`、cached scope/secret/genericity scan 均通过；真实
  session Skill 零模型编译为 9 workers，PICO/AE/I-E 的多块结果字段分别扩展到
  22/13/14，0 compiler errors。
- 历史 clean-archive Harness 候选镜像已构建为
  `chat_ds-harness:candidate-7ce353d3`，revision 为完整提交
  `7ce353d340aa69f632e485ae11a71bde3044414b`，镜像内 compileall/import smoke 通过。
  用户 E2E 到达 durable terminal 后才构建后续 `2a07218a` 并替换生产，没有人为取消
  该 root run。
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
  `ca9f5eac235cb924d3860826482df032d2a542fb`；Backend 功能 revision 为兼容的
  `0108c664443665b5748f2c3933f420ac79f9190d`。交接文档可另有 docs-only HEAD。
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

1. Harness 应准确执行任何符合通用格式规范的 Skill。V2.3 只作为复杂压力测试和业务级验收 oracle，不是运行时目标。不得加入 GAL3、疾病、Skill/package/session ID、route/worker/KG ID、文件名、固定 worker/文件数量或其他夹具字面量特判。
   - V2.3 暴露的缺陷必须先重述为跨领域的 compiler、workflow、capability、sandbox、evidence、artifact、recovery 或 lifecycle 不变量，再修改生产代码。
   - Skill 自身声明的拓扑、数量、名称和产物合同可以作为数据被通用编译和执行，但不得固化为 Harness policy。
   - 每项由 V2.3 发现的修复都必须增加通用合成回归，并在适用时增加至少一个非 V2.3 跨领域 holdout 或 mutation/rename 测试；V2.3 E2E 仅作验收，不能成为证明泛化性的唯一回归。
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

### 3.1 `9b1fc851...` 本轮 E2E 证据

- 必须把三类证据一起看：对话中只有一项用户临床开发请求；当时 exact
  `healthsim-trialsim` Skill 声明 9 workers、bootstrap、fan-in/aggregation 和强终稿；
  AgentRun/debug 显示 Harness 的确编译并调度了该 DAG，并非直接聊天或假 multi-agent。
- 旧生产 `17e261ef` 首批 worker 中 10 个 run 成功、4 个失败：Safety 因模型复制了
  stale `KNOWLEDGE_GATE_GAPS_JSON` 而被外层 receipt audit 拒绝；Target biology 在一次
  pre-submit DNS 失败后连续产生 corrupt tool-call batch，随后输出/repair 耗尽且多个
  activated group 无 dispatch receipt；AE 和 Competition 分别在 13/15 turn 边界前只
  完成 6/7 个 activated group。Competition 的直接诱因是已访问 OpenAlex family 的
  body-truncation continuation 抢占了尚未访问的独立 group。
- 旧 PICO child 虽标记 succeeded，但 loader 只编译 primary 输出 5 个字段，漏掉同一
  worker 文件中的 ICH supplementary 和 statistical simulation 两个 instruction/output
  block；AE、I/E 也存在同构漏编译。这是通用多块声明编译缺陷，不是业务质量问题。
- 根 run 随后自动补跑 Safety、AE，并补调度 Literature worker；补跑继续使用旧生产
  Harness，因此其结果只能用来确认根因/恢复行为，不能验证 `7ce353d3` 修复。
- root `21f5b63e...` 于 14:04 UTC durable failed；数据库终态为 10 succeeded、8 failed、
  0 cancelled（8 包含同一 workflow node 的旧尝试与自动重试，不是 8 个不同必需节点）。
  根错误是 Safety retry 虽已有完整 decision/group receipts，却因正文未再次列出
  `KG-A1/KG-A4` 被外层 required-ID 校验误拒。机器 receipt 和模型正文形成了两个相互
  冲突的 authority。
- Literature retry 在仍缺 mandatory groups 时遇到 corrupt tool batch；旧 Harness
  错误加入“无工具合成”提示，实际下一轮却仍暴露 4 个 schema。该矛盾轮耗尽输出后，
  后续两次 required turn 都生成长 prose 而未调用工具，最终
  `required_capability_not_attempted`。`2a07218a` 现在先恢复 frozen mandatory frontier，
  不会加入 tools-closed synthesis 提示。
- AE retry 在一个 exact group 尚未有 receipt 时，已打开 HTTP family 的分页收尾触发
  synthesis reserve。旧请求仍暴露 5 个 schema 却没有保持 required 标志，模型输出
  8192 tokens 后进入无工具 visible-length continuation，最终缺少该 group receipt。
  `2a07218a` 规定 mandatory frontier 高于检索覆盖收尾，并对所有仍暴露候选的 mandatory
  turn 保持一次有界 non-call recovery。
- 因 required worker cohort 未通过，fan-in、11 个模块文件和强终稿阶段没有启动；
  workspace 除上传的 Skill zip 外没有报告 Markdown。这是 fail-closed 的直接结果，
  不是文件写入丢失。生产已修复并部署，但下一次业务级 V2.3 E2E 仍应由用户手工发起。

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
- 2026-08-04 完成 `8097db3c` Round 16 evidence-terminal transaction 与 actionable compiler feedback：
  - 部署前两次确认 active/nonterminal AgentRun 与 `:5173` established connection 均为 0；SQLite
    `quick_check=ok`、foreign-key violation 0；
  - 候选来自精确 clean archive `/tmp/chat_ds_deploy_8097db3c.ueKBN9`，archive 与 tracked tree
    均为 22,456 files；candidate revision label、import 与受影响 `268 passed` 精确通过；
  - 只 force-recreate Harness；Backend、Frontend、四槽、Proxy、Browser、SearXNG/Valkey 和数据卷
    均未重建；旧 Harness image 保留 `rollback-pre-8097db3c`；
  - 部署后 Harness image 为
    `sha256:75aa609858a9c8d24dd447b1d8565dbdccaf05378cb3123c8c377aa3ba655b9b`，revision
    为 `8097db3ca14d9341cffcf5d4253c5c8c51133728`，healthy/restart 0；三个 Frontend 入口、
    Harness 与 Backend→Harness health/models 全 200，storage identity 相同，SQLite quick/FK 正常，
    active run 与严重启动日志均为 0。
- 2026-08-03 完成 `ca9f5eac` Round11 planned-resource binding 与 child quality canonicalization：
  - 部署前连续两次确认 nonterminal AgentRun/root、enabled/running schedule 与 5173 established
    connection 均为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自精确 clean archive `/tmp/chat_ds_deploy_ca9f5eac.paRTS7`，archive 与 tracked tree
    均为 22,452 files；candidate compileall/import 与受影响回归通过，revision label 精确匹配完整 Git SHA；
  - 只 force-recreate Harness；Backend、Frontend、四槽、Proxy、Browser、SearXNG/Valkey 和
    数据卷均未重建；旧 Harness image 保留 `rollback-pre-ca9f5eac`；
  - 部署后 Harness image 为
    `sha256:c5b07eabae3e4a8af182965c9c0268558e4c37e87647e9e13d4131375b61282d`，revision
    为 `ca9f5eac235cb924d3860826482df032d2a542fb`，healthy/restart 0；三个 Frontend 入口、
    Harness 与 Backend→Harness health/models 全 200，storage identity 相同，严重启动日志 0，
    数据库仍健康空闲。
- 2026-08-03 完成 `45e131e3 + 0108c664` Round10 generic planner/fan-in 与远端模型更新：
  - 部署前连续两次确认 nonterminal AgentRun/root、enabled/running schedule 与 5173
    established connection 均为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自 clean archive `/tmp/chat_ds_deploy_0108c664.BpAKFl`，archive/tracked tree
    均为 22,452 files；candidate compileall/import、默认/legacy alias、两模型真实主循环均通过，
    revision label 精确匹配完整 Git SHA；
  - 只按 Harness → Backend 顺序 force-recreate；Frontend、四槽、Proxy、Browser、
    SearXNG/Valkey 和数据卷均未重建；旧镜像保留 `rollback-pre-0108c664`；
  - 部署后 Harness/Backend image 分别为
    `sha256:10d65e46efb53a7698a92d2c4835f149131e485bce5855276aff56cf6af457a8`、
    `sha256:1adb71c272df3b3f52cec172e4df7cbdac24d9b8c6d877e7fe9be841c5505b3d`，revision
    均为 `0108c664443665b5748f2c3933f420ac79f9190d`，healthy/restart 0；三个入口、
    Backend→Harness health/model catalog、storage identity、两模型 thinking 请求、数据库与日志
    smoke 均通过。
- 2026-08-03 完成 `6657f374` generic compact-plan/compiler/terminal-boundary 更新：
  - 部署前连续两次确认 nonterminal AgentRun/root、enabled/running schedule 与 5173
    established connection 均为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自 clean archive `/tmp/chat_ds_deploy_6657f374.SuZrMf`，archive 文件数与 tracked
    tree 完全一致；candidate image compileall/import 通过，revision label 精确匹配完整 Git SHA；
  - 仅 force-recreate Harness，Backend、Frontend、四槽、Proxy、Browser、SearXNG/Valkey 和
    数据卷均未重建；旧 Harness image 保留 `rollback-pre-6657f374`；
  - 部署后 Harness image 为
    `sha256:3fbcb23d2c26dbf70fd5469faea7a3418db02faa7d53428b83a392ac79ed5d8a`，revision
    为 `6657f3741ae0bb399333e5039dd2da994864e84b`，healthy/restart 0；三个 Frontend 入口、
    Harness 与 Backend→Harness health/models 全 200，两端 storage identity 相同，45 个工具中
    planner/delegate/process/readback/HTTP/Python 必需工具均注册，严重启动日志 0，数据库仍健康空闲。
- 2026-08-02 完成 `1d2b7d9c` terminal workflow phase 与 shared-storage attestation 更新：
  - 部署前连续两次确认 active/nonterminal root、enabled/running schedule 与 5173
    established connection 均为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自 clean archive `/tmp/chat_ds_deploy_1d2b7d9c.lBwXUs`，archive 与 tracked tree
    均为 22,452 个文件；Harness/Backend revision label 精确匹配完整 Git SHA；镜像内
    compileall 通过；
  - 按 Harness -> Backend 顺序 force-recreate，旧镜像分别保留
    `rollback-pre-1d2b7d9c`。Frontend、四槽、Proxy、Browser、SearXNG/Valkey 和数据库卷均
    未替换；
  - 部署后 Harness/Backend image 分别为
    `sha256:d335a4d9afd8becc19ae797330cd0c8f13ebd15128207b7f2ec591e1ac3a3d75`、
    `sha256:c763e8e9d55875117a9a7fa54b9242e5923d23cf77315118229f6ca73c5ba501`，revision
    均为 `1d2b7d9ce412f58e9d21acf6f18a56c1ebef419d`，healthy/restart 0；三个 Frontend
    入口、Harness 与 Backend->Harness health/models 均为 200；两端 path-free storage
    identity 完全一致，HTTP/回读工具已注册，严重启动日志 0，数据库健康且生产空闲。
- 2026-08-02 完成 `06439152` lossless tool-result spill/readback 更新：
  - 部署前连续两次确认 active AgentRun/root、running/enabled schedule 与 5173
    established connection 均为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自 clean Git archive `/tmp/chat_ds_deploy_06439152.LEJAcb`，Harness revision
    label 精确匹配完整 Git SHA；镜像内 compile/import 与工具注册 smoke 通过；
  - 只 force-recreate Harness；旧镜像保留 `rollback-pre-06439152`。Backend、Frontend、
    四槽、Proxy、Browser、SearXNG/Valkey 和数据库卷均未替换；
  - 部署后 Harness image 为
    `sha256:63ddfc85f83dc8aa1d89fc2e51ec80dba42831df6546370f8670a7e9cfdbe95b`，revision
    为 `064391529b767a2bb0228a5e74088d4572ad37c0`，healthy/restart 0；三个 Frontend
    入口、Harness 以及 Backend→Harness health/models 均为 200，`read_tool_result`
    已注册，严重启动日志、active root 和 schedule 均为 0。
- 2026-08-01 完成 `867ebdd9` delegated terminal transaction 更新：
  - 部署前连续两次确认 active root、running/enabled schedule 与 5173 established
    connection 均为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自 clean Git archive `/tmp/chat_ds_deploy_867ebdd9.RfTPTD`，Harness revision
    label 是完整 Git SHA；镜像内 compileall/import smoke 通过；
  - 只 force-recreate Harness；旧镜像保留 `rollback-pre-867ebdd9`。Backend、Frontend、
    四槽、Proxy、Browser、SearXNG/Valkey 和数据库卷均未替换；
  - 部署后 Harness image 为
    `sha256:632069f4cb29b2c77f30f3990e53d35e0c2717199851c84ff97354cb637cad91`，revision
    为 `867ebdd9453790af96bd54efd2f7ead968c81aec`，restart 0；Harness、
    Backend→Harness health/models 与三个 Frontend health 均为 200，严重启动日志、active
    root、schedule 与 established connection 均为 0。
- 2026-07-31 完成 `3987613c` delegated frontier recovery 更新：
  - 部署前连续两次确认 active AgentRun/root、running/enabled schedule 与 5173
    established connection 均为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自 clean Git archive `/tmp/chat_ds_deploy_3987613c.mAmjOI`，Harness/Backend
    revision label 都是完整 Git SHA；镜像内 compileall/import smoke 通过；
  - 只按 Harness → Backend 顺序 force-recreate；旧镜像保留
    `rollback-pre-3987613c`。Frontend、四槽、Proxy、Browser、SearXNG/Valkey 和数据库卷
    均未替换；
  - 部署后 Harness/Backend image 分别为
    `sha256:4f15d7e8afd7b579d0ab0c7d19b979af076642f68b70a66d470333d3161630fb`、
    `sha256:817390d6069315d69aef3bcd471f60d3f91f16ceac8e55cbb3d777127bfd1767`，
    revision 均为 `3987613c43405b0347bc8606260abde078b707ba`，restart 0；Harness、
    Backend→Harness、三个 Frontend health/models 均为 200，严重启动日志、active run、
    schedule 与 established connection 均为 0。
- 2026-07-31 完成 `aac60951` delegated recovery contract 更新：
  - 部署前连续两次确认 active AgentRun/root、running/enabled schedule 与 5173
    established connection 都为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自 clean Git archive `/tmp/chat_ds_deploy_aac60951.npJK2J`，两个 revision
    label 都是完整 Git SHA；镜像内 compileall/import smoke 通过；
  - 只 force-recreate Harness 与 Backend；旧镜像保留
    `rollback-pre-aac60951`。Frontend、四槽、Proxy、Browser、SearXNG/Valkey 和数据库卷
    均未替换；
  - 部署后 Harness/Backend image 分别为
    `sha256:08a4576feee38a6cec6f845ffc1ad9d4e2b07681e0b62f31cb288520d31925d4`、
    `sha256:ffc8c793cb67cf5fea3219f67575134b494252b63c71592782e6adab48f34cdb`，
    revision 都是 `aac609518430b348a518712136569f94cc7442db`，restart 0；Harness、
    Backend→Harness、三个 Frontend health/models 均为 200，严重启动日志、active run、
    schedule 和 established connection 都为 0。
- 2026-07-31 完成 `26d65158` exact mandatory phase 更新：
  - 部署前连续两次确认 nonterminal AgentRun/root、enabled schedule 与 5173
    established connection 均为 0；SQLite `quick_check=ok`、foreign-key violation 0；
  - 候选来自 clean Git archive `/tmp/chat_ds_deploy_26d65158.agPdNd`，revision label
    为完整 Git SHA，镜像内 compileall/import smoke 通过；
  - 仅 force-recreate Harness，旧 image 保留
    `chat_ds-harness:rollback-pre-26d65158`；Backend、Frontend、四槽、Proxy、Browser、
    SearXNG/Valkey 和数据库卷均未替换；
  - 部署后 image 为
    `sha256:1f25a2f577428e3cb7a3c26a734ae98d96cf592f45902f92b32e474eb86164a8`，
    revision 为 `26d65158e4a0bf52a9e5256a156feec4c5aee20b`，healthy/restart 0；
    Harness 与 Backend→Harness 的 health/models、三个 Frontend health 均为 200，
    严重启动日志匹配、active run、schedule 与 established connection 均为 0。
- 2026-07-31 完成 `2a07218a` mandatory evidence frontier 更新：
  - 等用户手工 E2E root durable terminal 后，连续两次确认 active root/AgentRun、
    enabled schedule 与 5173 established connection 都为 0；SQLite
    `quick_check=ok`、foreign-key violation 为 0；
  - 候选来自 clean Git archive `/tmp/chat_ds_deploy_2a07218a.ErJsuQ`，revision label
    为完整 Git SHA；镜像内 compileall/import smoke 通过；
  - 仅 force-recreate Harness。Backend、Frontend、四槽、Proxy、Browser、
    SearXNG/Valkey 和数据库卷均未替换；旧 Harness 保留 rollback tag；
  - 部署后 Harness image 为
    `sha256:5e9689d2f0c6926e7e94a3154a451ea972ad1a61d1d5630e2da2b4e5417f2d90`，
    revision 为完整提交 `2a07218a6f59454ec72a21a878f70d486dba2e46`，
    healthy/restart 0。Harness 和 Backend→Harness 的 `/health`、`/v1/models`，以及
    三个 Frontend `/api/health` 均为 200；启动严重错误匹配、active run 和 schedule
    均为 0。
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
- Harness 单次 provider stream hard ceiling 默认 14,400 秒，Backend→Harness SSE
  deadline 默认 18,000 秒，Frontend Nginx SSE read timeout 为 21,600 秒；各层依次留出
  cleanup、durable terminal 与传输余量，生产性长思考不会再被旧 40/50/60 分钟链路截断。

当前生产镜像：

| 服务 | Image ID | 状态 |
|---|---|---|
| `chat_acits_executor` ～ `_4` | `sha256:7eb2b7a0526aa6b9a2560d5b722c2bf3ae44fc72fdb83c65d3e834050056d17a` | 4 个同质槽 / healthy / restart 0 / revision `f3be516b` |
| `chat_acits_skill_egress_proxy` | `sha256:6f23e97983ace0c4855af3dbf65967678902d2cd8d5c5b33e92eeecb2cec072f` | healthy / restart 0 / revision `f1e59c20` |
| `chat_acits_browser` | `sha256:08bcf8860c10ba8fcd647b6d1a96c2c12e13e46db800c812acea82e17007240c` | healthy / restart 0 / revision `7bbc0809` |
| `chat_acits_harness` | `sha256:c5b07eabae3e4a8af182965c9c0268558e4c37e87647e9e13d4131375b61282d` | healthy / restart 0 / revision `ca9f5eac` |
| `chat_acits_backend` | `sha256:1adb71c272df3b3f52cec172e4df7cbdac24d9b8c6d877e7fe9be841c5505b3d` | healthy / restart 0 / revision `0108c664` / `/api/health` 200 |
| `chat_acits_frontend` | `sha256:ffedcc8db1373f454e5650404ab724be884b6a70a0c8027fc7e99c06a530b0d8` | running / restart 0 / revision `f3be516b` / `/` 200 |

生产 smoke 证据：

- `127.0.0.1`、`10.10.132.126`、`172.30.100.126` 的 Frontend `/` 和
  `/api/health` 均为 200。
- 四个 session-sandbox、browser、skill egress proxy 健康；Harness `/health` 与
  Backend `/api/health` 为 200。四槽 capability probe 的 runtime build 完全一致。
- 生产 SQLite `quick_check=ok`、foreign-key violation 为 0；当前计数为
  conversations/messages/runs/events/tasks/artifacts =
  `218 / 805 / 602 / 84463 / 560 / 832`，nonterminal agent run、active root、running/enabled
  schedule 均为 0。
- `task_items` 中有 18 条历史 `running` 投影，但其对应 root AgentRun 均已终态
  （10 succeeded、8 failed），不是当前活跃执行；判断运行态应以 durable AgentRun
  和 terminal event 为准。
- SearXNG 真实 `OpenAI GPT` 查询返回 27 条结果，命中 `360search`、`bing`、`mojeek`；
  SearXNG/Valkey 均 healthy。免费上游仍可能动态出现 unresponsive engine，不属于
  Harness 执行环境缺失。
- Harness revision label 当前为完整提交
  `ca9f5eac235cb924d3860826482df032d2a542fb`，Backend 为
  `0108c664443665b5748f2c3933f420ac79f9190d`；四槽与 Frontend 为
  `f3be516bdfc13c82e00fba66ac327364a585bb15`；Proxy 为
  `f1e59c20129d9c3ba91b0f80850983e93d24d9dc`；legacy Browser 为
  `7bbc08097a75c618fc8a7338ff96b6577b8772d4`。
  所有长期容器 restart 均为 0。
- executor/proxy/browser/Harness/Backend/Frontend 日志未发现 traceback、
  critical、fatal、unhandled、ProtocolError 或 exception。
- Round 11 两个 case 均已到 durable failed terminal，通用修复、回归、本地 commit 与生产部署
  已闭环；当前生产空闲，Round 12--15 已获用户明确授权。

回滚点：

- `ca9f5eac` 切换前 Harness 保留
  `chat_ds-harness:rollback-pre-ca9f5eac`；候选/部署 tag 为
  `candidate-ca9f5eac` / `deploy-ca9f5eac`，clean archive build 目录为
  `/tmp/chat_ds_deploy_ca9f5eac.paRTS7`。
- `0108c664` 切换前 Harness/Backend 分别保留
  `chat_ds-harness:rollback-pre-0108c664`、
  `chat_ds-backend:rollback-pre-0108c664`；候选/部署 tag 为
  `candidate-0108c664` / `deploy-0108c664`，clean archive build 目录为
  `/tmp/chat_ds_deploy_0108c664.BpAKFl`。
- `6657f374` 切换前 Harness 保留
  `chat_ds-harness:rollback-pre-6657f374`；候选/部署 tag 为
  `candidate-6657f374` / `deploy-6657f374`，clean archive build 目录为
  `/tmp/chat_ds_deploy_6657f374.SuZrMf`。
- `1d2b7d9c` 切换前 Harness/Backend 分别保留
  `chat_ds-harness:rollback-pre-1d2b7d9c`、
  `chat_ds-backend:rollback-pre-1d2b7d9c`；候选/部署 tag 为
  `candidate-1d2b7d9c` / `deploy-1d2b7d9c`，clean archive build 目录为
  `/tmp/chat_ds_deploy_1d2b7d9c.lBwXUs`。
- `06439152` 切换前 Harness 保留
  `chat_ds-harness:rollback-pre-06439152`；候选/部署 tag 为
  `candidate-06439152` / `deploy-06439152`，clean archive build 目录为
  `/tmp/chat_ds_deploy_06439152.LEJAcb`。
- `867ebdd9` 切换前 Harness 保留
  `chat_ds-harness:rollback-pre-867ebdd9`；候选/部署 tag 为
  `candidate-867ebdd9` / `deploy-867ebdd9`，clean archive build 目录为
  `/tmp/chat_ds_deploy_867ebdd9.RfTPTD`。
- `3987613c` 切换前 Harness/Backend 分别保留
  `chat_ds-harness:rollback-pre-3987613c`、
  `chat_ds-backend:rollback-pre-3987613c`；候选/部署 tag 为
  `candidate-3987613c` / `deploy-3987613c`，clean archive build 目录为
  `/tmp/chat_ds_deploy_3987613c.mAmjOI`。
- `aac60951` 切换前 Harness/Backend 分别保留
  `chat_ds-harness:rollback-pre-aac60951`、
  `chat_ds-backend:rollback-pre-aac60951`；候选/部署 tag 为
  `candidate-aac60951` / `deploy-aac60951`，clean archive build 目录为
  `/tmp/chat_ds_deploy_aac60951.npJK2J`。
- `26d65158` 切换前 Harness 保留
  `chat_ds-harness:rollback-pre-26d65158`；候选 tag 为
  `chat_ds-harness:candidate-26d65158`，clean archive build 目录为
  `/tmp/chat_ds_deploy_26d65158.agPdNd`。
- `2a07218a` 切换前 Harness 保留
  `chat_ds-harness:rollback-pre-2a07218a`；候选 tag 为
  `chat_ds-harness:candidate-2a07218a`，clean archive build 目录为
  `/tmp/chat_ds_deploy_2a07218a.ErJsuQ`。
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
- Shaiengine provider 凭据：`.local_secrets/shaiengine.env`；生产 `.env` 仅同步同名
  `SHAIENGINE_API_KEY`，两者均为 0600，不得输出值。
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

### 9.1 最多十八轮 E2E 迭代协议（用户于 2026-07-31 至 08-04 多次明确授权）

用户此前明确授权执行到 Round 8；Round 8 闭环后于 2026-08-02 追加 Round 9--13，
又于 2026-08-03 在 Round 10 暂停后追加五轮。Round 13 闭环前，用户最新再次授权从下一轮继续
五轮，因此当前可按同一协议继续 Round 14--18；该最新授权替代“下一轮必须由用户手工发起”的
默认限制和旧 Round 15 上限，Round 18 是当前绝对上限。Round 14--18 每轮由两个独立
acceptance case 组成：V2.3 与 `yangbb` 账户 User Skill registry 中的肺癌 MDT Skill
分别使用全新 conversation/root；只有两个
case 都达到 durable terminal 并分别完成三源诊断，才算该轮结束。两个 root 默认顺序
执行，不以并发模型竞争污染验收结果。每轮必须使用新的
conversation/root run，并在该轮达到 durable terminal 后才计数；同一 run 的重试、补跑、
刷新或重复解读不算新一轮。任何生产切换必须先确认没有其他用户 active root run，且不得
为了赶轮次人为取消正在运行的任务。

肺癌 MDT 基线来自 `yangbb` 的非 session User Skill `lung-cancer-mdt`：当前 `SKILL.md`
SHA-256 为 `2955c00a456f7ca4215e27091c55ceeca6c84d170e4af99560adb54e0d5b4d42`，
36-file tree digest 为 `200708f85f8186b04f96646bc8d20bdd85354e8aa931c5b7ca05566712ede254`。
历史会话 `0f495...` 与 `a78cf...` 的测试 prompt 仅有空行排版差异；移除空行后的共同
semantic SHA-256 为 `ecc16dc8f97994015c62b529e210cbc67296160b4fa54a99a95499916166bd9b`。
自动轮次复用较新的 `a78cf...` 原始 user message（raw SHA-256
`eefb885294e6849d1e5ab5ce9f6799a30dfff1b9520761bd403138b7f4b135b7`），不能从 assistant
回复或 debug 推断/改写输入。

每轮固定执行以下闭环：

1. 冻结并核对持久化对话、该 run 实际 Skill/package/resource 内容寻址快照，以及
   debug/AgentRun/tool/provider/artifact 事件，构造从 compile/bind 到唯一 terminal 的时间线。
2. 对照 Skill 声明的 route、worker DAG、Knowledge Gate、exact capability、fan-in、
   artifact/strong-final/post-merge 合同与 ground truth 的结构、覆盖、证据链、表格、附录、
   traceability 和可用性；不要求逐字节相同。
3. 将异常分别归因到 Harness、Skill、provider/model、沙箱/依赖、网络/策略或上游来源；
   逐个解释 succeeded/degraded/failed/cancelled attempt，不能用前端最后一条文案代替证据。
4. 在修改生产代码前，把缺陷重述为跨领域不变量，并先建立 ScriptedProvider、故障注入、
   mutation/rename 或非临床 holdout 的确定性复现。V2.3 E2E 只能验收，不能单独证明泛化。
5. 冻结本地独立仓库 `claude-code/` 的 exact commit，并只读取与本轮故障相关的实际代码路径，
   把问题映射到 durable checkpoint/pending write、typed state/structured output、幂等 activity
   retry、subgraph failure isolation、sandbox/workspace boundary、trace 与 exactly-one terminal
   等机制，形成 problem -> code path/pattern -> adopt/adapt/reject 记录。该源码是主要依据；只有
   相关路径为 stub、调用链断裂或存在真实语义疑点时，才允许针对该疑点做最小化 Web 补证，并
   分别记录源码证据、Web 补证与取舍。不恢复 OpenClaw/Hermes 或其他 Harness 的常规框架调研。
   遇到 stub 必须先标记未知，不能自行推断缺失行为；仅罗列文件名或概念、不核对代码路径不算
   完成对照。
6. 修复只能进入通用 compiler/workflow/capability/evidence/artifact/recovery/lifecycle 层；
   不得加入疾病、V2.3、package/session/route/worker/KG ID、固定数量或报告文件名特判。
   只有确实提升任意规范 Skill 执行能力、并由通用复现及跨领域 holdout 证明的修改，才计为
   一轮“修复”；纯 V2.3 workaround 不计数。若某轮验收没有暴露通用缺陷，则记录为通过轮，
   不为了凑修复数量制造代码改动。
7. 运行受影响回归、跨领域 holdout、隔离基础全量、secret/genericity/diff 检查；从 clean
   Git archive 构建候选并按现有无活跃任务部署协议切换。记录代码 revision、镜像、回滚点
   和生产 smoke 后再开始下一轮。

维护代理应在每轮 terminal 后自动模拟用户此前有效的修复追问链，而不是等待用户再次发送：
“这个 session 在干什么/哪里失败” -> “结合具体 Skill、对话、工具调用、思考/回复和 debug
log 仔细查验” -> “逐个排查 delegate 的 failed/degraded/cancelled” -> “针对各问题先定义原因、
观察信号和彻底修复思路，并设计更多测试复现” -> “核对冻结 `claude-code/` 中相关实际代码路径，
不要闭门造车” -> “实现跨 Skill 的系统性通用改进，完成回归、commit、部署并继续观察”。
这条追问链属于维护侧诊断流程；ChatDS E2E 的用户业务输入仍保持历史手工基线，不把内部
测试答案、工作流提示或修复暗示注入被测模型。

控制面遵循单调阶段：`compile/bind -> conditional decision -> mandatory receipts -> optional
retrieval -> synthesis -> fan-in -> artifact validation -> exactly one durable terminal`。所有
bounded recovery 必须停留在当前 mandatory frontier；模型正文不能覆盖 handler receipt、
effect ledger、artifact CAS 或 durable terminal 等机器事实。

本 campaign 每轮的持久化记录至少包含：conversation/root/child IDs，代码与镜像 revision，
Skill/package/workflow digest，provider/model/context/max-output/finish/elapsed，实际 tool schemas、
tool_choice、dispatch/preflight/receipt，recovery 原因与次数，fan-in cohort，artifact 路径/大小/
摘要/合同结果，inner/outer terminal 关联，以及成熟方案的 problem-to-pattern-to-decision 对照。

### 9.2 当前最多十八轮 campaign 状态

逐轮证据、模拟人工追问链、delegate 明细、成熟实现对照、通用不变量、确定性复现、
revision/image 与生产 smoke 统一记录在 `E2E_ITERATION_LOG.md`。Round 1 的新会话为
`8314f40fa1a449f88cca55c140df218d`，root 为
`25f48718174746118e2e3662bd177816`；Round 2 为
`2b1e321d275543de9328c3079259f5a8`，root 为
`b64b7cf03538447588965a602fcdf42b`；Round 3 为
`2dcbcfa305084c5a9e11d4a359075054`，root 为
`69cbcaacf1174ab4b9d96821e1bfeb7a`；Round 4 为
`205709a7f8b447119670b6686f2e7601`，root 为
`7287d853563d46cd949e86727db11ef4`；Round 5 为
`c8d53cd3f6904e90b88640a9125b7c0b`，root 为
`6421809b83be4d53a698ddfee550b01c`；Round 6 为
`862eb37670634f5394fab116429fa948`，root 为
`88d0fd14ec01449cace347fcde4d6858`；Round 7 为
`67119645fa874ecba689c8a61e3874de`，root 为
`5e494f191ead47a6ad640295cd48e36e`；Round 8 为
`9ff98843e980458d832629ba9964ec96`，root 为
`ad98fb353fb240f2b3ab84f345ceb247`。八轮均已到 durable failed terminal，各自通用修复
已在 `26d65158`、`aac60951`、`3987613c`、`867ebdd9`、`36e8ea43`、`70df8b51`、
`06439152` 和 `1d2b7d9c` 完成回归、本地 commit、clean-archive 部署与生产 smoke。
Round 8 还包含 shared-storage attestation 父提交 `c3f9f582`。用户已明确追加 5 轮；Round 9
的 V2.3 与肺癌 MDT case 均已到 durable failed terminal，并已按 exact Skill、对话、debug/
AgentRun/tool/provider/artifact 完成三源诊断。Round 9 通用修复已提交 `6657f374`、从 clean archive
部署并通过生产 smoke。Round 10 的 V2.3/肺癌 MDT case 分别为
`bc632e897c384f34bfec3433fd477bbe` / `d66b7e4017234ff1853fa7f35dc9224f` 与
`cb7515fad602405da4b873ccc37a9ecc` / `09b907e90e534e139bf81424220d3abb`；两者均已到
durable terminal，三源诊断、通用修复 `45e131e3`、回归、本地 commit、clean-archive
`0108c664` 部署与生产 smoke 已闭环。用户随后明确恢复并追加五轮。Round 11 的
V2.3/肺癌 MDT case 分别为 `49791ec4ef37449c84b7c1611e256a06` /
`b75a71b3dbdd48f58dd76ec31a4a3b46` 与 `b830029d282447cf8abcce196c7d6b41` /
`941e09a080694159ac6d45c205b2d7e0`；两者均已到 durable terminal，三源诊断、通用修复
`ca9f5eac`、完整回归、本地 commit、clean-archive 部署与生产 smoke 已闭环。Round 12 的
V2.3/肺癌 MDT 分别为 `9bb4a0173fc44c5b94cb4258b2a17ab7` /
`f96df86c12744cc5bd4cafc176ec6a8f` 与 `265ffb56b04141fe99e1281ab2811e7d` /
`424100dd5ffd4d10afbc1224f1a7f877`，通用修复为 `0406ab72`。Round 13 最终验收的
V2.3/肺癌 MDT 分别为 `2ca049506d0249418815b64bab500ead` /
`5e635b2d7e4b4486bdeb37d88690d34b` 与 `7143d3304a6643c6aa3ff888d63a56d6` /
`01236e10499d43898c0a1ab96cbe4598`，通用修复为 `d23c7e43`，生产已切换并通过 smoke。
Round 14--16 已闭环；Round 16 的 V2.3/肺癌 MDT 分别为
`8bdd202c6b854c07b21e61100723a977` / `3fef4aeefbd74600866712c02ecb3853` 与
`7f8382b53003479b9c38d5f7d43d1c15` / `129194592ba943b4842d7cc610902fe5`，通用修复为
`8097db3c`，生产已经切换并通过 smoke。Round 17--18 仍获授权；下一项是从 `8097db3c` 顺序建立
Round 17 的两个全新 case。不得复用
任何已终态 run，或把同一 run 的重试、补跑和重复解读计为新轮。

## 10. 已知非 blocker 边界

- 本轮没有重建四个既有 Legacy Executor 与 Browser；其 Docker `unhealthy` 是旧容器 healthcheck
  `exec` 在本宿主触发已复现的 `no-new-privileges + seccomp -> errno 524`，不是主进程退出，现有
  Harness 请求路径仍在工作。Claude Runner/Proxy 已采用上述已验证的 host-compatible 安全模式并
  均 healthy。不要仅为消除状态标签而放松 Legacy 隔离；若后续迁移 Legacy 容器，应先在基础镜像
  清除 setid/file-capability、校验 label，再逐槽 drained rollout 并运行完整 sandbox 回归。
- V2.3 与 ground truth 的业务级一致性仍需真实模型重型 E2E；基础回归不能替代这项验收。
  用户最新授权继续 Round 17--18；Round 16 已闭环。Round 18 后如仍需模型重型 E2E，必须
  重新获得授权。
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
- Legacy Harness 的 session-wise 隔离仍是固定同质容器池内的 lease/root-run 隔离；可选
  ClaudeCodeEngine 则由 Supervisor 为每个正在执行的 Turn 动态创建独立容器，结束即删除，
  workspace/state 通过当前 Session 的精确挂载持续保存。
- 依赖 profile 固定且不可运行时安装；复杂动态 Bash/Node/Python 需 exact marker/manifest。
- Skill sandbox 的公网与显式白名单私网 HTTP(S) 都必须经过签名 egress policy；
  不支持 CAPTCHA、stealth、反爬绕过或未确认的重要操作。
- “只允许下载、禁止上传”能显著缩小风险，但 HTTP retrieval 本身仍会发送
  域名、路径、查询词和协议元数据；GET 也可把数据编码进 query/header。当前已拒绝
  GET/HEAD body 并精确限制 method/origin/path，严格 DLP 仍需查询/header schema、
  内容检查和出站字节/速率预算，不能把 GET/HEAD 等同于数学意义上的单向通道。
- Claude CLI 当前仍需在其 worker 环境获得部署 Provider credential。精确 Provider endpoint 和
  signed egress policy 可阻止把它发送到未授权目的地，但不能从数学上阻止模型把 workspace 内容
  编码进本来就获准的 Provider 请求；不要宣称零泄露。若威胁模型要求模型/Skill 完全不可见
  Provider secret，下一步必须实现由受信 Proxy 注入认证头的 credential gateway，而不是扩大网络。
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

## 11. 2026-08-05 Docker 历史项清理

- 本次只清理本机 Docker 中可证明未被当前运行面依赖的 ChatDS 历史容器和镜像；没有使用
  `docker system prune -a`、`docker image rm --force` 或其他强制删除，也没有清理 volume、
  workspace、network、BuildKit cache、模型权重或非 ChatDS 项目。
- 共删除 10 个无运行依赖的 ChatDS 残留容器：3 个 exited socket/smoke 容器，以及 7 个从未
  启动的 Claude runner candidate/smoke 容器。删除前后，全部运行中 ChatDS 容器和 26 个运行中
  非 ChatDS 容器的 ID/name/image 冻结快照保持不变；vLLM、HIS、OpenELIS、PACS、SearXNG、
  Browser MCP 和本地 Git 服务均未触碰。
- 共删除 212 个旧 `chat_ds-*` candidate/deploy/rollback/test 镜像引用，以及 7 个无容器引用的
  旧 `chatds-*` acceptance/execmode/weston/test 引用。当前只保留 20 个 ChatDS 镜像引用：每个
  生产组件的当前 `latest`/部署标签、每个组件最近一个有意义的 rollback，以及精确
  `chat_ds-claude-runner:2.1.152`。生产 image ID 与本节前记录一致。
- Docker image inventory 从 283 降至 84，Docker 汇总的 image virtual size 从 254.1 GB 降至
  232.8 GB；该 21.3 GB 是含共享层的汇总差值，不能当作物理磁盘净释放量。第一次安全
  `docker image prune -f` 明确报告回收 1.01 GB。dangling image ID 从 102 降至 5；剩余 5 个均
  被现存容器引用，其中 3 个属于运行中的 HIS/OpenEMR/ChatDS SearXNG，2 个属于已停止但不在
  本次授权范围内的 insurance 项目，因此保留。
- 清理后 Backend、Legacy Harness、Claude runner Supervisor 和 Egress Proxy 均为
  healthy/restart 0；三个 Frontend `/` 始终为 200。Backend 到 Harness 的 `/health`、
  `/v1/models` 以及 `127.0.0.1`、`10.10.132.126`、`172.30.100.126` 的 `/api/health`
  曾同时恢复为 200，但下述活跃 Legacy root 未结束时后续探针又同步返回 503，因此不能把这一轮
  业务健康探针记为稳定通过。
- 最终 smoke 期间恰有会话 `6be9862f7fc143c4b590d6a1f187c41b` 的 Legacy root
  `4143fe85324b4a198d0e39f16fe3f99a` 及 3 个 delegate 在运行。Harness 单 Uvicorn 进程一度仍可
  建立 TCP，但本地和容器间 `/health` 都无法在 2--5 秒内返回，导致 Backend `/api/health`
  暂时为 `harness_health_unavailable`；未重启、取消或干预该任务后探针自行恢复并连续返回 200。
  这不是镜像删除造成的容器/网络缺失，而是一条后续应以独立测试复现的 Legacy 长任务事件循环
  饥饿观测，不能通过放宽健康超时掩盖。

## 12. 2026-08-13 Shaiengine Kimi K3 接入

- 提交 `20d950890cc1dc6f3e31c0925d6515e87c83d734` 将
  `shaiengine_kimi_k3` 加入 Backend、保留的 Legacy provider catalog 和 Claude Runner
  `shaiengine` profile；它是非默认、可 agentic 的多模态候选模型，默认模型仍为
  `shaiengine_glm_5_2`。
- 现有生产 `SHAIENGINE_API_KEY` 的无泄漏探测结果：`GET /v1/models` 为 200，精确列出
  `kimi-k3`；Anthropic Messages facade 为 200/`end_turn`，OpenAI chat completions 也能
  进入 reasoning。目录条目只有 `id/object/created/owned_by/supported_endpoint_types`，没有
  `max_model_len` 或 `context_length`，单模型端点同样不提供容量。因此 1,000,000 token
  声明来自 Kimi first-party K3 规格，不得记录成 shaiengine runtime discovery。
- 使用 1206x2622 的无敏感合成 PNG 直接调用 shaiengine `kimi-k3` Anthropic facade，返回
  200 并正确识别为纵向。这证明 Claude Code `Read` 的 2000x2000 失败是本地客户端图像
  预处理边界，不是 Kimi 或 qwen 服务端视觉尺寸上限。仅切换到 Kimi 不会绕过同一 Claude
  Code `Read` 前置处理；统一 Session attachment rendition/tiling 与 Turn deliverable terminal
  contract 仍是后续独立通用修复。
- 回归：Backend 52 项、Harness/model routing 16 项、Claude Runner config 6 项，共 74 项
  通过；`docker compose config --quiet` 通过。候选来自 clean archive
  `/tmp/chat_ds_deploy_20d95089.NfFRSy`，archive/tracked tree 都为 22,518 files。
- 部署前连续确认 nonterminal AgentRun、engine active run、running/enabled schedule 与 5173
  established connection 均为 0，SQLite `quick_check=ok`、外键违规 0。只滚动 Claude Runner
  Supervisor 和 Backend，Frontend 原容器仅在切换窗口停止后原样启动；数据库卷、Claude Turn
  image、四个 Legacy slot、Harness、Proxy、Browser、搜索和其他生产容器均未重建。回滚 tag 为
  `rollback-pre-20d95089`。
- 部署后 Backend image 为
  `sha256:b86d627a36e5ca7d5d63663046d4a1c59ea0b3066e49615e8b861bf9a64c7664`，Supervisor
  image 为 `sha256:cd1c7e97e7f63c40e67c8ab143ddf614bef296b28e9375f8a9673fb628c1207a`；
  两者 revision 都为完整 `20d950890cc1dc6f3e31c0925d6515e87c83d734`、healthy/restart 0。
  Backend 与 Supervisor 均回读 `kimi-k3`/1M，四个 Frontend 入口 `/api/health` 为 200，
  Supervisor 鉴权 health 为 200，SQLite 仍健康、active run 为 0，严重启动日志匹配为 0。
# 2026-08-14 DeepSeek Harness peer-engine implementation (pre-deployment)

- Added the official `deepseek-ai/deepseek-harness` as the pinned, independent
  `deepseek-harness-clean/` Git submodule at commit
  `47f943859bef60e4160492346772ded9b24f765a` (`0.1.0-rc.5`, MIT). ChatDS does
  not patch that source tree; `deepseek_runner/` is the adapter/control plane.
- DeepSeek Harness is now a peer `AgentEngine` (`deepseek_harness`) rather than
  Legacy Harness policy. Each Turn runs in a fresh `network_mode=none`
  container which mounts only that user's exact Session workspace, the
  Session-owned runtime state, the immutable compiled Skill view, and
  controller-owned receipts. The model process runs unprivileged; PID 1 owns
  egress, artifact discovery and the single authoritative terminal.
- Model/provider bindings fail closed and are deployment-owned. OpenAI model
  traffic and SearXNG `/search` traffic use the existing signed exact-egress
  proxy; arbitrary Docker/host/other-user/other-Session paths are not mounted.
  SearXNG is activated by the `deepseek-harness` Compose profile.
- The composer now shows a Harness selector immediately left of the model
  selector. Model choices are filtered by explicit engine compatibility;
  Harness is immutable after the first durable message/run and switching an
  existing Session requires a fork.
- Generic native-engine scheduling, cleanup and lifecycle handling now cover
  both Claude Code and DeepSeek Harness. Claude Code remains a thin native
  adapter and its runtime behavior was not changed.
- Verification completed before deployment: backend `337 passed`; frontend
  `47 passed`, targeted ESLint and production build; DeepSeek/Claude contract
  suites passed (the old Claude test image has one unrelated pre-existing
  `/usr/bin/python3` fixture mismatch); Compose validation passed; SearXNG live
  query and the native DeepSeek SearX adapter both returned results. No V2.3
  model-heavy E2E was launched.
- Protected user-owned tracked deletions remain unstaged and must not be
  restored or committed:
  `XGAL-101_Galectin-3_AD_Comprehensive_Development_Plan_v1.0_claudecode执行参考.md`
  and `xClinicalTrial-Design-V2.2.zip`.

# 2026-08-24 Native-engine recovery and four-Session adapter repair

- The work continued in Claude Session `59903124-d548-48c5-bf38-f069322cd5e3`
  is present in this branch as commits `341df61c`, `ecbfdfc5`, `a3a2a487`, and
  `1d72515c`. The last commit restores the missing `compatibleModels` and
  `workspaceOpen` declarations. The source build passed; the earlier one-off
  container copy was not treated as an immutable deployment receipt.
- Follow-on generic native workflow/activity recovery is in `e5657ed0` and
  `f1310cdd`. The current local-only HEAD is `7d9f12f9` (`fix: compact native
  activity and bind fresh skills`), six commits ahead of the configured
  remote-tracking branch. No remote push was performed.
- `ce2348...`, `05bd42...`, `54bd48...`, and `b58f7f...` were diagnosed from
  persisted conversation, exact immutable Skill/route, and native/debug
  receipts. Detailed frozen timelines and classifications are in
  `E2E_ITERATION_LOG.md`. No defect was found in either pinned native core.
  The terminal Claude run was interrupted by provider `ECONNRESET`; the
  terminal DSH run missed the fresh native Skill invocation and later failed
  the machine artifact byte contract. The two reference DSH runs exposed
  provider stream/capacity errors plus a ChatDS token-level projection backlog.
- `7d9f12f9` changes only ChatDS-owned boundaries: fresh DSH Sessions lower the
  exactly-one selected primary Skill to the public native slash invocation;
  resume turns remain exact user text. DSH token deltas remain in the lossless
  native/raw ledger but Web presentation persists complete semantic blocks and
  stable tool/run events. Raw writes are bounded batches with a terminal
  barrier; Session hydration uses one newest 5,000-event tail window. Frontend
  now distinguishes successful tools from a later failed workflow, keeps one
  tool card per call identity, and marks partial output as non-terminal.
- Generic warehouse/museum rename and 10,000-delta failure-injection
  regressions were added. Final verification: Backend `387 passed, 119
  warnings, 2 subtests`; Frontend `56 passed`; Vite production build emits
  `/assets/index-l8V9nsyn.js`; modified-file ESLint, Python compile, and diff
  checks pass. The two full-repo ESLint findings outside this diff remain the
  pre-existing effect/setState cases documented in `E2E_ITERATION_LOG.md`.
- Clean archive `/tmp/chat_ds_deploy_7d9f12f9.ANGN44` produced candidates:
  Backend `sha256:ac828fa2...`, Frontend `sha256:6e9c1d45...`, and DeepSeek
  Supervisor `sha256:a5af4870...`; all carry revision `7d9f12f9` and passed
  isolated import/config smoke. Neither `deepseek-harness-clean/` commit
  `47f943859bef60e4160492346772ded9b24f765a` nor `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7` was changed or rebuilt.
- Deployment completed after the user explicitly authorized abandoning the two
  live reference Turns. Both were closed through the native Supervisor cancel
  API and received exactly one `cancelled` terminal before their containers
  exited. Backend, Frontend, and DeepSeek Supervisor now run the `7d9f12f9`
  candidates (`sha256:ac828fa2...`, `sha256:6e9c1d45...`,
  `sha256:a5af4870...`); all carry revision `7d9f12f9`, restart 0, and the two
  health-checked services are healthy. Rollback tags are
  `rollback-pre-7d9f12f9`. Neither native Turn image was rebuilt or switched.
- Post-deployment proof: all three Frontend entry coordinates return `/` and
  `/api/health` 200 with `/assets/index-l8V9nsyn.js`; SQLite quick-check is OK,
  foreign-key violations and nonterminal AgentRuns are zero; the four diagnosed
  roots reconcile to two failed and two cancelled terminals. Authenticated
  production tail queries are bounded and expose `has_earlier`; internal
  SearXNG returns real results. Headless Chromium loaded the large `05bd42...`
  Session without a white screen, Runtime exception, console error, or stale
  running tool label. A new user-driven model E2E remains the next acceptance
  action; upstream transport availability is not claimed as repaired.
- The two protected tracked deletions above remain unstaged. Existing
  untracked Session/runtime/reference directories are user/runtime state and
  must not be bulk-added or cleaned.
- GitHub synchronization is complete at
  `https://github.com/feng4251/chat_ds`, branch
  `fix/native-adapter-production-20260824`. The pushed tip was verified equal
  to local HEAD. The pre-existing remote branch
  `fix/generic-skill-harness-20260717` has an unrelated rewritten history (no
  common merge-base), so it was deliberately neither force-pushed nor merged;
  the local branch now tracks the new production branch.

# 2026-08-25 Shaiengine recovery, GLM-5.3 and stable native Web projection

- Claude conversation `57211a178c9640c5a9cf8edaa4a9967f`/root
  `7fbe534e82954d34be3e5fabda00f72d` and DeepSeek conversation
  `25b87a66246a4e3795b2fa1f6e2f66c3`/root `3fe9779303184b5693c090262d192e2e` were correlated from persisted
  conversation, exact immutable Skill/route and native/debug receipts. Both failed because the production Shaiengine credential
  returned structured 401; Claude's old `native_result_duplicated` and DeepSeek's old `workflow_contract_failed` hid that causal
  receipt. Full evidence, worker/barrier detail and mature-source comparison are in `E2E_ITERATION_LOG.md`.
- The replacement credential is active only through restricted ignored deployment files; never copy it into Git, logs or commands.
  It was disclosed in chat, so recommend another controlled rotation after current verification. OpenAI and Anthropic probes for
  `glm-5.3` are 200.
- Commit `be8850a5` adds generic structured-provider precedence, stale-retry protection, stable task progress identity, one tool card
  per `tool_use_id`, terminal settlement of progress, less fragmented reasoning/content projection and the current
  `shaiengine_glm_5_3` route. Historical `shaiengine_glm_5_2` remains exact and visible; new native Sessions default to GLM-5.3.
  No Skill/session/domain literals occur in production logic.
- Local native references remain untouched: `claude-code/` is clean at
  `6f6f12b37f529488b10e53928dd5508bb93535c7`; `deepseek-harness-clean/` is clean at
  `47f943859bef60e4160492346772ded9b24f765a`. ChatDS still owns only IO/session/workspace/policy/projection boundaries.
- clean-archive verification: Backend `394 passed`; Claude Runner `125 passed, 1 skipped`; Frontend `59 passed`; Vite build,
  targeted ESLint, Compose, AST/diff/genericity and both native-image self-tests pass. Production runs revision `be8850a5` images:
  Backend `cf6446c4...`, Frontend `62b9a5c4...`, Claude Supervisor `fbd37c39...`, Claude Turn `73c32a6b...`, DeepSeek
  Supervisor `031e03d0...`, DeepSeek Turn `ab0ad450...`; all prior images have `rollback-pre-be8850a5` tags.
- Production smoke is complete: six switched components restart 0; Backend and both Supervisors healthy; three Web/API entries 200
  on `/assets/index-FoxG4oKM.js`; SQLite quick-check OK/foreign-key violations 0; SearXNG returns real results. Fresh Claude
  `700997...`/`dcc3890b...` and DeepSeek `9f5257...`/`4dd6a0...` GLM-5.3 smokes both succeeded. Headless Chromium saw no
  white screen, login redirect, stale running label or Runtime/console/Log error.
- Do not rerun either old failed root or rewrite its terminal. The next complex Skill/V2.3 acceptance remains user-driven. Keep the
  two protected tracked deletions unstaged and preserve all untracked Session/runtime/reference data.
- GitHub push for this Aug-25 closure is still pending authentication. Remote
  `fix/native-adapter-production-20260824` had no concurrent commits and was exactly two commits behind before the attempt, but this
  Codex environment has no HTTPS credential, SSH key/agent or connected GitHub plugin. Both push transports failed before any write.
  Keep the local commits; request a non-interactive GitHub authorization, then push `HEAD` explicitly to that branch without force.

# 2026-08-26 signed Provider idle and append-only Web projection

- Commit `5a16eacd0245c2f1eb7acf74b452b22da5b4e7f1` fixes only ChatDS-owned boundaries: v3 signed exact-POST Provider
  routes may carry a bounded response-idle budget; public-read/Skill/MCP retain the short default. Frontend hydration now preserves
  earlier activity across bounded-tail refresh, ignores empty polls and avoids repeated smooth-scroll. DSH projection backfills a
  late native worker label on the same run identity and preserves structured Provider codes with safe UI guidance.
- Three same-Skill production Sessions were correlated from conversation, immutable Skill and debug/native/AgentRun evidence.
  Claude `6d3b...` succeeded with 8 workers. Shaiengine DSH `dce...` completed 8 workers then received external
  `provider_http_403` precharge rejection; balance/entitlement remains external. Local DSH `ae7...` ended failed at
  `2026-08-26 03:44:29Z`: all 14 child attempts were transport errors under the old 30-second relay, and the final
  `workflow_contract_failed` was downstream. No native-core or Skill/compiler defect was found.
- Clean archive `/tmp/chat_ds_deploy_5a16eacd.SeBj3u` built and deployed eight revisioned candidates. Running image IDs are:
  runtime `dc956576...`, proxy `da92eb53...`, Claude Turn `9ab8048d...`, DSH Turn `774db4fe...`, Claude Supervisor
  `246ae4b9...`, DSH Supervisor `de4b4707...`, Backend `4d132cf1...`, Frontend `884542b1...`. All old images have
  `rollback-pre-5a16eacd`. DSH upstream remains clean at `47f943859bef60e4160492346772ded9b24f765a`; local Claude
  reference remains clean at `6f6f12b37f529488b10e53928dd5508bb93535c7`; Claude binary remains official `2.1.152`.
- Production verification: all target containers running/restart 0; Backend, both Supervisors, proxy and SearXNG healthy;
  three Web/API entries 200 on `/assets/index-Bbukj5_6.js`; SQLite quick-check OK, FK violations/nonterminal runs 0;
  signed idle settings all 14,400 seconds; SearXNG real aggregate query returns 9 results; headless login root is nonempty with
  no application ReferenceError. Compose also recreated the SearXNG container as a dependency, but all persistent volumes were
  preserved. The inert image anchors intentionally have no healthcheck, so Compose `--wait` warns even though independent state
  verification passes.
- Regression evidence and exact terminal timeline are in `E2E_ITERATION_LOG.md`. No model-heavy V2.3 was launched. The next
  acceptance must be a fresh user-driven Session; do not reinterpret old failed roots. Preserve the two protected tracked deletions
  and all existing untracked runtime/reference state. This commit remains local-only unless the user explicitly requests a push.

# 2026-08-27 unbounded native Turns and Web-native runtime controls

- Commit `48999f33ebd54c08082d9b02c6c7c98487352e90` removes the default total wall-clock deadline from both native Turn
  Supervisors. Production now resolves `CLAUDE_RUNNER_MAX_RUN_SECONDS=0` and
  `DEEPSEEK_HARNESS_RUNNER_MAX_RUN_SECONDS=0`; cancellation, resource ceilings and transport-liveness bounds remain distinct
  deployment safety controls and are not total task-duration limits.
- Commit `bb0b399b9952483f46d247253dd47952d8cd5e24` exposes three engine-neutral controls only through ChatDS-owned Web,
  persistence, Supervisor and Turn-I/O adapters: queued follow-up, immediate steer and Esc-style interruption. Requests are
  immutable and idempotent by control ID, persisted before transport, acknowledged only after the native public boundary accepts
  them, and reattached from bounded durable receipts after refresh/reconnect. An interruption produces one authoritative cancelled
  terminal while retaining the native Session, workspace and checkpoint for a later Turn.
- Neither native implementation was modified. The vendored trees remain byte-identical to HEAD trees
  `claude-code=ef7589945b3767ead85fc52f68d013f88094bd47` and
  `deepseek-harness[-clean]=f904efab9ef435201d6ba4da88a34d6366568272`; the frozen source references remain Claude
  `6f6f12b37f529488b10e53928dd5508bb93535c7` and DSH
  `47f943859bef60e4160492346772ded9b24f765a`. Claude lowering is justified by
  `claude-code/src/cli/print.ts` and its typed SDK schemas (`control_request/interrupt`, user priority `later`/`now`). DSH lowering
  is justified by `packages/core/agent/src/runtime-types.ts`, `packages/core/agent-loop/src/agent.ts` and
  `packages/host/apiproxy/src/api-proxy.ts` (`followup`, `steer`, and `cancel(..., {keepInbox:true})`). The adopted pattern is a thin
  authenticated I/O projection plus durable receipts; a second agent loop, retry/compaction state machine or control prompt was
  explicitly rejected.
- Generic verification passed: Backend engine/persistence/rename holdouts `127 passed, 2 subtests`; Frontend utility/reconnect/
  projection tests `69 passed`, targeted ESLint and Vite production build; all DSH adapter tests `18 passed`; shared/Claude/DSH
  control tests and both Supervisor idempotency tests passed. Candidate-image smokes exercised the installed Claude stream-json
  lowering and DSH Agent Host lowering, verified official Claude Code `2.1.152`, preserved DSH upstream revision, and confirmed the
  production cron dependency. Host-only collection misses (`pytest`, browser runtime path) were not treated as product failures;
  the same affected controls were executed in their actual Turn images. No V2.3/model-heavy E2E was launched.
- Clean archive `/tmp/chat_ds_deploy_bb0b399b.ncGZOW` built six candidates. Running image IDs are Backend `3d2ffab9...`,
  Frontend `295e5870...`, Claude Supervisor `0309f5ca...`, Claude Turn `f83ecb8a...`, DSH Supervisor `fe892c88...`, and DSH Turn
  `047f963e...`. All prior six images retain `rollback-pre-bb0b399b`; candidates and `deploy-bb0b399b` tags point to the accepted
  images. DSH keeps upstream revision in `org.opencontainers.image.revision` and the ChatDS packaging revision in
  `org.opencontainers.image.chatds.revision`.
- Production acceptance: Backend and both Supervisors are healthy; all six switched containers have restart count 0. The three
  entry coordinates `127.0.0.1`, `172.30.100.126`, and `10.10.132.126` return root/API/hashed asset 200 on
  `/assets/index-u6x97V7B.js`; containerized Chromium renders a nonempty login application without an application ReferenceError.
  SQLite quick-check is OK, foreign-key violations and active runs are zero; SearXNG returns 10 real results. The production
  Backend imports the native-control route and exact action schema. A fresh user-driven long/model execution remains the behavioral
  acceptance for interrupt/follow-up/steer; do not manufacture a V2.3 run solely for this release.
- Preserve the two protected tracked deletions and all untracked runtime/reference data. Only stage this handoff file for the
  deployment-evidence commit. The user explicitly authorized pushing current `main` to `https://github.com/feng4251/chat_ds` after
  verification; push without force and verify the remote tip.

# 2026-08-27 three-Session terminal audit and Web/egress boundary repair (pre-deployment)

- Three independent user-driven V2.3 stress runs are terminal and frozen. Claude `426126...` root `2dad218b...` succeeded after
  more than four hours: all 8 workers, workflow and artifact passed; final is 154,575 bytes/2,644 lines. DSH local DeepSeek
  `70ec34...` root `8001f671...` failed after two attempts of one mandatory worker: raw native/provider evidence shows malformed
  model-generated Bash JSON followed by compatibility-facade HTTP 400; the phase barrier correctly blocked later work. DSH qwen
  `63d9e5...` root `32cf1bed...` completed all 8 workers and both phases, but its 153,800-byte final has only 1,681 lines versus the
  immutable Skill's 2,000-line minimum; `artifact_contract_failed` is correct. Do not relax either machine-owned gate and do not
  modify a native Harness for these two model/content failures.
- All three used the same input SHA `2f042f...`, ZIP SHA `78b890...`, Skill view `9aee2a...`, exact
  `composite_full_protocol_design` route SHA `b7ff3b...`, and declared 11-module artifact contract. The complete three-source
  timelines, full immutable hashes, delegated attempts and classifications are in `E2E_ITERATION_LOG.md`.
- A separate ChatDS Web defect explains Enter/Esc appearing inert during `70...`: the persisted conversation has no native-control
  message and Nginx recorded zero `/controls` POSTs. A live SSE request disabled all durable run-card polling; a stale empty
  hydration could erase the just-accepted root; controls required a hydrated target and silently returned otherwise. The repair
  keeps SSE authority over transcript order while polling control authority, preserves only an accepted root across an older empty
  read, and recovers a target only from exactly one durable active root. Missing, different or ambiguous roots fail visibly and do
  not create another Turn. Existing idempotent control IDs/receipts and Supervisor/native lowering are unchanged.
- Frontend presentation now follows streaming only while the user was already pinned to the bottom. Only the current reasoning or
  worker block auto-opens; terminal/history blocks auto-collapse and explicit user disclosure survives lifecycle updates. Existing
  stable tool-call identity continues to replace `执行中` in place; no second completion card path was added.
- `63...` also exposed a generic exact-egress parser bug: printable `%`/`#` query data encoded as `%25/%23` was rejected before
  SearX. Proxy and runtime bridge now distinguish query data from path traversal. Encoded controls and all dangerous encoded path
  forms remain rejected; exact signed origin/method/path/query authority is unchanged.
- Generic regressions use renamed engines plus harbor/inventory/factory cases, never V2.3 literals. Current verification is
  Frontend `74/74`, proxy/runtime `112/112`, targeted ESLint, `git diff --check`, and Vite production build. The frozen mature
  reference is root tree object `claude-code=ef7589945b3767ead85fc52f68d013f88094bd47` (documented upstream source commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7`); relevant patterns are typed stdin controls in `src/cli/print.ts`, historical
  thinking disclosure in `AssistantThinkingMessage.tsx`, and URL/query data separation in `ccrClient.ts`. Neither native source
  tree was edited. No additional model-heavy E2E was launched.
- Modified production paths are limited to Frontend projection/control UI, shared exact-egress policy, browser-runtime bridge and
  their tests/docs. Preserve the two protected tracked deletions and all untracked runtime/reference directories. Stage explicit
  paths only. The user authorized production deployment and a non-force push of final `main` to
  `https://github.com/feng4251/chat_ds`; append exact clean-archive image/deploy/health/remote receipts here after completion.
