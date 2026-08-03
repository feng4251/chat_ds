# 通用 Skill Harness 五轮 E2E 迭代日志

本文件记录 2026-07-31 获得用户明确授权的五轮 V2.3 复杂 Skill E2E campaign。
V2.3 只作为业务压力测试和结构/质量 oracle；任何生产修复都必须先重述为跨领域
Harness 不变量，并由通用合成测试与非临床或 mutation/rename holdout 证明。每轮只有在
新的 root run 到达 durable terminal 后才计数，同一 run 的重试不算新一轮。

每轮自动模拟以下人工排障追问链：

1. 这个 session 在做什么、在哪里失败或异常？
2. 同时对比持久化对话、当时 exact Skill/package/resources、debug/AgentRun、工具调用、
   provider 思考/回复与 workspace artifacts，执行意图是否符合 Skill？
3. 逐个解释 delegate 的 succeeded/degraded/failed/cancelled，而不是只读前端终态。
4. 对每个问题先定义通用根因、可观察信号、确定性复现和彻底修复思路。
5. 对照成熟 session-wise Harness/workflow 的官方机制，明确 adopt/adapt/reject。
6. 只实现跨 Skill 的 compiler/workflow/capability/evidence/artifact/recovery/lifecycle
   改进，完成回归、local commit、clean-archive 部署后再开始下一轮。

## Round 1：同名桥接工具的坐标漂移与历史锚定

### 冻结身份与终态

- 生产代码：`2a07218a6f59454ec72a21a878f70d486dba2e46`。
- Harness image：
  `sha256:5e9689d2f0c6926e7e94a3154a451ea972ad1a61d1d5630e2da2b4e5417f2d90`。
- Conversation：`8314f40fa1a449f88cca55c140df218d`。
- Root run：`25f48718174746118e2e3662bd177816`。
- Skill archive SHA-256：
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`。
- Primary Skill：`healthsim-trialsim`；primary `SKILL.md` SHA-256：
  `85ecc2fc48b290596c0cf2153b8268cc9f1a6b4f50ca75fb3989f477c8e7df1b`；
  package SHA-256：
  `2a91527a2c1a72d3608a1969ead3310491ae6e6effa81a70d1c050148098a4eb`；
  orchestrator SHA-256：
  `6e8520593ebe68c5fb19c32dcae846f2525668f22d81701b9f201300d41939df`。
- Provider/API model：`deepseek_v4_pro` → `AgentModel`；可用 context 250,368，
  provider stream hard cap 2,400 秒。
- Ground truth SHA-256：
  `0b7a30eb45a0699b8281c72e65679f5b270d9b77416300499f9eb4c55e73690f`；
  200,094 bytes / 3,383 lines / H1 13 / H2 74 / H3 172 / table rows 1,159 /
  code fences 92。
- Root durable terminal：`run.failed / delegate_step_failed`。根因文本指向
  `worker-pico-standards` 的 bounded corrupt-tool recovery 耗尽；不是客户端断线、
  root timeout、路由失败或文件丢失。
- Workspace 没有业务 Markdown。required worker cohort 尚未通过，fan-in、模块报告、
  strong-final 与 post-merge verifier 均未进入；只有运行配置/debug、上传 ZIP、缓存和
  child result 文件。

### 对话、Skill 与执行图对比

业务输入保持历史手工基线，没有向模型注入测试答案或修复暗示。Harness 正确选中一个
primary 加 18 个 supporting members，正确进入 `composite_full_protocol_design`，执行
intent classification、7 路 bootstrap 和第一批 3 个 workers。根 run 没有退化为普通
chat，也没有伪造 multi-agent；失败位于已编译 DAG 的 mandatory worker barrier。

### Delegate/attempt 逐项结果

| Run | 语义身份 | 终态 | input/output tokens | 观察结论 |
|---|---|---|---:|---|
| `733d07b3...` | Intent classification | succeeded | 1,547 / 638 | typed intent 正常 |
| `ee969a7c...` | clinicaltrials_gov | succeeded/degraded | 177,855 / 1,870 | 有界来源退化，不是全局断网 |
| `5ea21add...` | pubmed | succeeded/degraded | 183,637 / 1,448 | 保留 unresolved retrieval |
| `6c58f194...` | ich_guidelines | succeeded/degraded | 151,082 / 4,182 | 一次 metasearch 质量失败 |
| `5fc36188...` | fda_guidance | succeeded/degraded | 282,364 / 5,273 | 一次 HTTP 404，其他来源仍推进 |
| `6ca1d5de...` | ema_guidance | succeeded/degraded | 112,028 / 2,571 | typed degraded 完成 |
| `a221f848...` | target_biology_intel | succeeded/degraded | 236,280 / 2,192 | OpenTargets 400 被隔离为来源失败 |
| `6ee5d25d...` | competitive_intel | succeeded/degraded | 36,134 / 1,027 | 无 verified evidence dispatch receipt |
| `0ac67115...` | Safety extraction | succeeded/degraded | 556,463 / 6,075 | KG 3/3 receipts |
| `84b69f50...` | Termination analysis | succeeded/degraded | 859,105 / 20,686 | KG 1/1，但先发生 3 次错误坐标调用 |
| `148abdf5...` | PICO/standards/simulation | failed | terminal usage 未落入外层摘要 | KG 4 个激活组仅 3 个 receipt，corrupt recovery 耗尽 |

0 个 child 被取消。HTTP 400/404、脚本/API错误和搜索质量不足均有来源级 typed 证据；
它们不是本轮 root 失败的共同网络原因。

### 通用根因与可观察信号

1. **同名 bridge 只有 tool-name frontier，没有 pre-dispatch exact-coordinate frontier。**
   Termination worker 的当前 mandatory group 只允许 PubMed，但模型先用
   `skill_http_get` 调了 3 次 ClinicalTrials 坐标。三次请求都真实进入 dispatcher 并
   成功返回，Harness 之后才记录 `knowledge_gate.candidate.dispatch_unmatched`。
   这证明共享 tool name 不能作为当前阶段的派发授权。
2. **corrupt-tool recovery 没有隔离已经结算的阶段历史。** PICO 已完成 3 个激活组，
   最后只剩一个 exact group；provider 仍反复输出已完成/foreign tool name、约 6K 字符
   malformed arguments，并多次达到 2,048 output limit。修复请求仍带约 142K input
   tokens、17–18 条历史消息和旧 assistant tool-call/tool-result envelope，导致 schema/
   phase anchoring。8 次 corrupt stream、4 次 nonstream replacement failure 后，正确的
   fail-closed 终态触发。
3. **控制面应保持单调。** 已完成 receipt 不得因模型正文或旧历史重新打开；pending
   mandatory frontier 必须先于 optional retrieval/synthesis，且越界调用应在外部 effect
   前被拒绝。

### 成熟实现对照与决策

| 问题 | 成熟机制 | 本轮决策 |
|---|---|---|
| 已完成 sibling/step 不应随失败丢失 | LangGraph checkpoint/pending writes；Temporal event history/activity retry | adapt：保留现有 durable receipt ledger，不重跑已结算组 |
| 子图/子 Agent 上下文污染 | LangGraph subgraph isolation；Pydantic AI/Deep Agents fresh isolated subagent runs | adapt：repair 生成独立 phase request，durable history 不变 |
| 工具 surface 随状态变化 | Pydantic AI dynamic tool preparation/structured schema | adopt：当前 phase 只发布 pending schema，并校验 exact args coordinate |
| 不确定副作用后的恢复 | Temporal activity/idempotency boundary；Pydantic AI settled step snapshots | adapt：派发前可证明的 mismatch 零 effect；派发后仍由 handler receipt 判定 |
| 完整框架替换 | LangGraph/Temporal/Pydantic AI Harness | reject：现有内容寻址 Skill、authority、sandbox、CAS、terminal 主链已成熟；用小范围 adapter/invariant 修复风险更低 |

官方参考：

- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/use-subgraphs>
- <https://docs.langchain.com/oss/python/deepagents/subagents>
- <https://docs.temporal.io/workflows>
- <https://pydantic.dev/docs/ai/harness/step-persistence/>
- <https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/>
- <https://pydantic.dev/docs/ai/harness/subagents/>

### 通用修复与确定性证明

- 新增 candidate-kind 通用 pre-dispatch target matcher；native/MCP、Skill resource、
  script/process、declared command、GET/POST exact HTTP prefix 使用 compiler-owned
  coordinate 与当前 grant 交叉验证。HTTP 共享 bridge 的 host/path 不匹配时不进入
  handler，`actual_dispatch_attempted=false`，且不计入 cross-tool failure streak。
- corrupt-tool mandatory repair 改为两消息 phase snapshot：有界 task capsule、机器
  completed-receipt ledger 和当前 pending frontier。旧 assistant tool-call envelope 与
  tool result 仍保留在 durable history 供最终 synthesis 使用，但不会重放到原子调用
  生成请求；debug 记录前后 message/token 计数和 retained-envelope=0。
- 非临床 mutation test 让同一个 HTTP bridge 同时拥有 active/inactive 两个合法 grant，
  证明 inactive URL 在派发前被拒，dispatcher 只收到 active URL，之后仍可正常完成。
- 跨工具 phase test 声明 `web_search -> read_file` 两个强制组，先结算搜索，再故意让
  provider 连续输出旧搜索工具的破损流；恢复请求只暴露 `read_file`，只含 system/user
  phase snapshot，无旧 tool-call/tool-result 消息，并成功收敛。

当前验证：两组核心测试 `64 passed, 15 subtests passed`；扩展的 Knowledge Gate、
capability、stream 与 convergence 组合为 `272 passed, 52 subtests passed`；更宽的
Knowledge Gate/delegation 组合除一个已知宿主生产 NFS tombstone 隔离噪声外为
`566 passed, 214 subtests passed`。clean tracked-tree 全量为 `1818 passed, 3 skipped,
759 subtests passed`，另有 22 个环境/fixture 红灯：生产 NFS 隔离 cohort 在同时替换
workspace 与 path-security root 后为 `13 passed, 9 subtests passed`；3 个依赖未跟踪
runtime Skill fixture 的合同测试在真实 fixture 下为 `3 passed`。这些复跑确认没有本轮
代码回归。
本轮生产修复提交：
`26d65158e4a0bf52a9e5256a156feec4c5aee20b`（`fix: isolate exact mandatory
capability phases`）。clean archive：`/tmp/chat_ds_deploy_26d65158.agPdNd`；候选/
生产 image：
`sha256:1f25a2f577428e3cb7a3c26a734ae98d96cf592f45902f92b32e474eb86164a8`，
revision label 与提交全 SHA 一致；旧 Harness 保存为
`chat_ds-harness:rollback-pre-26d65158`。

部署前连续两次确认 nonterminal root/run、enabled schedule 与 5173 established
connection 均为 0，SQLite `quick_check=ok`、foreign-key violations 为 0；只
force-recreate Harness。部署后 Harness healthy/restart 0，容器内与 Backend→Harness
的 `/health`、`/v1/models` 均为 200，三个 Frontend `/api/health` 均为 200，严重启动
日志匹配为 0；再次确认 active/schedule/connection 为 0。Backend、Frontend、四个统一
沙箱、egress proxy、Browser、SearXNG/Valkey 和数据库卷未替换。

## Round 2：mandatory non-call、终态 schema 与并行失败可观测性

### 冻结身份与终态

- 生产代码：`26d65158e4a0bf52a9e5256a156feec4c5aee20b`；Harness image：
  `sha256:1f25a2f577428e3cb7a3c26a734ae98d96cf592f45902f92b32e474eb86164a8`。
- Conversation：`2b1e321d275543de9328c3079259f5a8`；root run：
  `b64b7cf03538447588965a602fcdf42b`。
- 原始 ZIP SHA-256：
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`；
  primary `SKILL.md` SHA-256：
  `85ecc2fc48b290596c0cf2153b8268cc9f1a6b4f50ca75fb3989f477c8e7df1b`；
  package SHA-256：
  `2a91527a2c1a72d3608a1969ead3310491ae6e6effa81a70d1c050148098a4eb`。
- SSE 被维护端持续消费到正常 EOF；root 的唯一 durable terminal 是
  `run.failed / delegate_step_failed`。因此不是浏览器断线、Backend 无 terminal、Harness
  HTTP timeout 或人为取消。
- 上传 ZIP 已持久化到 session workspace（829,621 bytes）；没有业务 Markdown，因为
  required worker barrier 未通过，literature、I/E、aggregation、11 模块写入、README、
  checklist、strong-final merge 与 post-merge validation 都尚未开始。

### 对话、Skill 与执行图对比

业务输入与历史手工测试相同。Harness 正确选择一个 primary 与 18 个 supporting Skills，
正确解析 `composite_full_protocol_design`，完成 intent、7 路 bootstrap，并按声明并行启动
PICO、Safety、Termination、AE adjudication、Target biology、Competitive landscape。
执行没有退化为直接 chat，也没有漏编 DAG；失败发生在 worker receipt/typed-result 层。

### Delegate/attempt 逐项结果

| Run | 语义身份 | 终态 | input/output tokens | 结论 |
|---|---|---|---:|---|
| `558e...` | Intent classification | succeeded | 1,547 / 511 | route 正常 |
| `2d870...` | clinicaltrials bootstrap | succeeded/degraded | 175,237 / 1,334 | 3 个工具调用成功 |
| `8adfe...` | pubmed bootstrap | succeeded/degraded | 295,089 / 3,093 | typed gap + contract repair |
| `2edc...` | ICH bootstrap | succeeded | 149,508 / 4,221 | 正常 |
| `9f8...` | FDA bootstrap | succeeded/degraded | 212,441 / 5,523 | 一个上游 404，其他证据推进 |
| `11266...` | EMA bootstrap | succeeded | 150,911 / 3,820 | 正常 |
| `fdfe...` | target biology bootstrap | succeeded/degraded | 498,884 / 3,339 | DNS/400/TLS 来源级退化后仍有 3 次成功 |
| `033247...` | competitive bootstrap | succeeded/degraded | 36,148 / 1,015 | 无工具 dispatch |
| `a7ee...` | PICO worker | succeeded/degraded | 1,213,732 / 27,243 | partial/open-chain gap 被显式保留 |
| `e55d...` | Safety worker | failed/retryable | 1,072,087 / 8,311 | raw pseudo protocol 后走大历史正文重写，footer 仍坏 |
| `d682...` | Termination worker | succeeded | 321,501 / 8,853 | 2 次脚本成功；无效 `dict` 未 dispatch |
| `ed523...` | AE adjudication worker | failed/terminal | 1,075,931 / 25,526 | mandatory call 被忽略；纠正请求再次 length |
| `93f...` | Target biology worker | succeeded/degraded | 2,034,571 / 19,367 | 404/400/429 与坐标拒绝均保留为来源/前置证据 |
| `9d292...` | Competitive worker | failed/retryable | 1,891,847 / 6,904 | HTTP continuation 纠正再次 length |

上游 404/429/400/DNS/TLS 并非共同根因：同一 run 内其他请求成功，且真正 root blocker
是 mandatory non-call/typed terminal convergence。Round 1 的 pre-dispatch exact-coordinate
修复在本轮生效：3 次越界坐标都以 `actual_dispatch_attempted=false` 被挡在 handler 前。

### 模拟人工追问后的通用根因

1. **non-call correction 没有复用 phase isolation。** AE 初始 mandatory 请求约 139K
   tokens，纠正仍约 137K；Competitive 的 retrieval correction 约 155K，随后 continuation
   请求约 185K。模型已经忽略 `tool_choice=required`，Harness 却只在旧 conversation 后
   追加一句纠正，导致下一采样继续受旧 tool/result/phase 锚定并耗尽 2,048 output。
2. **终态修复存在两套不一致协议。** 小型 `submit_result_fields` schema 投影已有两消息、
   强制 tool choice、严格校验的可靠路径；Safety 却进入旧的 tools-closed 正文重写，带 19
   条消息、约 157K input 和 6 个旧工具 envelope，生成 21K 字符后 footer 仍不合法。
3. **debug 把内部 convergence terminal 写成第二个 `run.*`。** durable DB 每个 child
   实际只有一个权威 terminal，但 workspace JSONL 同时出现内部候选和外层合同终态；Safety
   因而看起来先 completed 后 failed。问题是 debug 命名歧义，不是 durable terminal 翻转。
4. **receipt event 数量不等于完成组数量。** 同一 Knowledge Gate 组的分页/重取会再次
   发 `group.receipt`；真实 ledger 按 group ID 唯一，但原 debug 没有 first/no-op transition
   和 unique/activated/pending 计数，人工统计容易虚增。
5. **root 只显示一个 blocker。** 同一并行 wave 中 Safety、AE、Competitive 都失败，
   workflow snapshot 有三者，但 root error 只显示 AE；同时最终 assistant projection 又用
   OpenAI transport `stop` 覆写 AgentRun 的 `delegate_step_failed` finish reason。

### 成熟机制对照与决策

| 问题 | 官方成熟机制 | 决策 |
|---|---|---|
| 并行 sibling 成功不能因一项失败丢失 | LangGraph checkpoint/pending writes | 保留现有 receipt-owned pending writes，不整批重跑 |
| 子 Agent/恢复阶段上下文隔离 | LangGraph per-invocation subgraph | adapt 为 machine phase snapshot，不替换主循环 |
| 结构化终态 | Pydantic AI output function/schema validation/ModelRetry | adopt 现有内部 submitter，废除有 schema 时的自由正文重写 |
| 易失败 I/O 与 LLM 重试边界 | Temporal Activity retry | adapt：只重试当前无副作用 control/activity phase，不重放 whole child |
| 全框架迁移 | LangGraph/Temporal/Pydantic AI | reject：会分叉既有 authority、sandbox、CAS、event 与终态语义 |

官方参考：

- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/use-subgraphs>
- <https://docs.temporal.io/encyclopedia/retry-policies>
- <https://pydantic.dev/docs/ai/core-concepts/output/>
- <https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/>

### 通用修复与确定性证明

- mandatory non-call 与 exact HTTP continuation correction 统一使用两消息 phase snapshot；
  只含有界原任务、机器 completed receipts、当前 pending frontier 与精确 HTTP action，旧
  assistant/tool messages 不上 wire，durable history 不被删除。
- 有声明 `required_result_fields/schema` 的 raw pseudo/malformed terminal 统一进入内部
  `submit_result_fields`：Harness 从原任务、最近已 dispatch 的工具坐标及 tool results 构造
  最多 48KiB 的不可信 evidence capsule，强制 exact-one schema call；该调用不进入 registry、
  capability ledger 或副作用计数，缺证据必须产生 typed degraded gap。
- workspace debug 将内部候选终态记录为 `debug.agent_loop.terminal_candidate`，保留原
  candidate type 和 lifecycle scope；外层仍只写一个权威 `run.completed/failed`。
- Knowledge Gate receipt debug 增加 `first_transition`、transition type、unique completed、
  activated 与 pending count；事件数不再被误读为已完成 DAG 节点数。
- root terminal payload 同时包含所有当前失败节点及 terminal/retryable 状态；最终
  `AgentRun.finish_reason/error` 以权威 root event 为准，不再被 transport `stop` 覆写。

通用合成测试覆盖：大上下文 mandatory no-call、HTTP 精确 continuation、raw pseudo 后
内部 schema 投影、无 registry dispatch、无旧协议重放、同组多次 receipt transition、并行
sibling failure snapshot、debug 候选/权威终态区分，以及 Backend finish reason 投影。
聚焦组合为 `428 passed, 142 subtests passed`；Harness 全量为
`1835 passed, 3 warnings, 772 subtests passed`，唯一 Node 环境项在生产同版固定
`/usr/bin/node` 下 `1 passed`；Backend 主体 `214 passed`，缺少 `/harness` mount 的 9 项
在正确跨组件 mount 下随该文件 `47 passed`。`py_compile`、`git diff --check` 与生产代码
genericity scan 通过。

本轮功能提交为 `aac60951 fix: isolate delegated recovery contracts`。候选来自 clean
archive `/tmp/chat_ds_deploy_aac60951.npJK2J`；Harness image 为
`sha256:08a4576feee38a6cec6f845ffc1ad9d4e2b07681e0b62f31cb288520d31925d4`，
Backend image 为
`sha256:ffc8c793cb67cf5fea3219f67575134b494252b63c71592782e6adab48f34cdb`，
两者 revision label 都是完整提交
`aac609518430b348a518712136569f94cc7442db`。部署前连续两次确认 active AgentRun/root、
running/enabled schedule 和 5173 established connection 均为 0；SQLite
`quick_check=ok`、foreign-key violation 为 0。只 force-recreate Harness/Backend，旧镜像
保留 `rollback-pre-aac60951`；部署后三入口、Harness 与 Backend→Harness health/models
均为 200，长期容器 restart 0，严重启动日志 0，数据库与空闲状态再次通过。

## Round 3：恢复预算粒度、计算能力投影与权威终态

### 冻结身份与终态

- 被测生产代码：`aac609518430b348a518712136569f94cc7442db`；Harness image：
  `sha256:08a4576feee38a6cec6f845ffc1ad9d4e2b07681e0b62f31cb288520d31925d4`；
  Backend image：
  `sha256:ffc8c793cb67cf5fea3219f67575134b494252b63c71592782e6adab48f34cdb`。
- Conversation：`2dcbcfa305084c5a9e11d4a359075054`；root run：
  `69cbcaacf1174ab4b9d96821e1bfeb7a`。
- 原始 ZIP SHA-256：
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`；
  829,621 bytes，1 个 primary 加 18 个 supporting Skills，上传件已持久化到该
  session workspace。
- 维护端连续消费 SSE 4,816 秒直到正常 EOF；root 的唯一 durable terminal 是
  `run.failed / delegate_step_failed`。不是浏览器断线、Backend/Harness timeout、人工取消、
  沙箱缺失或共同网络故障。Backend 的客户端终态错误显示成 transport `stop`，与权威 root
  event 冲突，这是独立的投影缺陷。
- 0 个业务 Markdown、0 个 Artifact row。required worker barrier 尚未通过，因此 fan-in、
  11 个模块、strong-final 和 post-merge verifier 没有启动；已完成 sibling 的 child result
  仍被持久保存。

### 对话、Skill 与执行图对比

业务输入保持历史手工基线。Harness 正确选择内容寻址的 `healthsim-trialsim` 包，完成
intent、7 路 bootstrap，并进入声明的 worker wave；不是直接 chat，也没有伪造 multi-agent。
13 个 AgentRun 中有 10 个 succeeded、3 个 failed（包含 competitive bootstrap 的一次
retryable 失败及成功重试）。root 的 `delegate_failures` 正确携带 Safety blocker，证明
Round 2 的并行失败可见性修复已生效。

### Delegate/attempt 逐项结果

| Run | 语义身份 | 终态 | input/output tokens | 结论 |
|---|---|---|---:|---|
| `a07e5f43...` | Intent classification | succeeded | 1,547 / 658 | typed route 正常 |
| `cf156c15...` | clinicaltrials bootstrap | succeeded/degraded | 225,679 / 1,994 | 来源级降级 |
| `d887a3ae...` | PubMed bootstrap | succeeded/degraded | 186,979 / 3,475 | 来源级降级 |
| `39beed73...` | ICH bootstrap | succeeded/degraded | 111,316 / 4,361 | 来源级降级 |
| `1d082720...` | FDA bootstrap | succeeded/degraded | 284,562 / 4,281 | 一次脚本退出和一次 404，其余链推进 |
| `5e025fc0...` | EMA bootstrap | succeeded/degraded | 149,090 / 2,578 | 一次 metasearch provider 失败 |
| `1a1a6816...` | Target biology bootstrap | succeeded/complete | 398,397 / 2,846 | 多次来源失败后仍有 5 个 POST receipt |
| `7f6185d6...` | Competitive bootstrap 首次 | failed/retryable | 36,143 / 4,641 | 填充了无证据字段，被 typed contract 拒绝 |
| `130f2f34...` | Competitive bootstrap 重试 | succeeded/degraded | 36,143 / 873 | 以 null/gap 合法收敛 |
| `7c3e7d4c...` | PICO/standards/simulation | succeeded/degraded | 860,827 / 12,365 | 计算工具投影丢失后猜测不存在的脚本 |
| `b33813fc...` | Termination analysis | succeeded/complete | 1,156,520 / 10,517 | 多个成功工具 receipt |
| `bd1d5425...` | Safety extraction | failed | 432,827 / 5,863 | 第三个独立 mandatory group 首次 non-call 即被误判终态 |

没有 child cancelled。FDA/EMA/target 的 4xx、DNS、API 和搜索失败各自被限制在来源链内；
Safety 的两次 `skill_http_get` 均成功，说明 root blocker 不是“网站都不可达”。PICO 的 3 次
脚本/可调用对象错误均在 dispatcher 前拒绝，暴露的是能力 surface 与已验证 authority
不一致，而不是沙箱执行失败。

### 模拟人工追问后的通用根因

1. **一次纠形预算错误绑定到整个 child，而不是 immutable mandatory frontier。** Safety
   有 5 个 Knowledge Gate checks/groups，其中 3 个激活。第一个 exact group 完成后，模型
   在第二组上返回 length prose；Round 2 的两消息隔离纠正把约 374K 字符旧历史压到 2 条、
   约 35K 字符，并成功取得第二组 receipt，证明隔离机制有效。receipt 推进后第三组形成了
   新 frontier，模型第一次再次忽略 required call，旧的 run-global boolean 却立即 fail
   closed。正确语义应是“同一 exact frontier 最多纠正一次”，而不是“整个子任务最多一次”。
2. **编译、校验和模型 surface 三层发生 authority 漂移。** PICO worker 的声明要求有界
   Monte Carlo/统计计算；compiler 把 `execute_code` 放入 worker tools，delegation validator
   也以 `bounded_calculation_allowed` 明确接受，但 Knowledge Gate 最终 runtime projection
   只保留 evidence candidates/readers/decision tool，静默删除 `execute_code`。模型因此猜测
   package 中不存在的脚本和 callable。计算是独立的 compiler-owned worker control，不是
   Knowledge Gate evidence candidate。
3. **客户端终态服从了传输层而非运行事件。** OpenAI-compatible transport 正常 EOF 的
   `stop` 被写入 `stream_terminal.finish_reason`，覆盖了已持久化 root
   `delegate_step_failed`。状态虽为 failed，reason 却为 stop，削弱了 debug 的可判定性。

### 成熟实现对照与决策

| 问题 | 官方成熟机制 | 决策 |
|---|---|---|
| 多个独立步骤各自重试 | LangGraph node/task retry 与 checkpointed pending writes；Temporal Activity retry | adapt：恢复预算绑定 exact frontier fingerprint；全局 iteration/hard deadline 继续封顶 |
| 成功 receipt 不随 sibling/frontier 失败回滚 | LangGraph persistence/pending writes | 已采用并保留；只重采样当前未结算 frontier |
| 工具 surface 与运行状态一致 | Pydantic AI dynamic tool preparation/typed dependencies | adapt：runtime projection 复用已验证的 compiler-owned control，不新授予权限 |
| 终态唯一权威 | Temporal event history；LangGraph durable state | adopt：root terminal event 优先，transport reason 仅在无终态时 fallback |
| 替换现有主循环 | LangGraph/Temporal/Deep Agents | reject：本轮是三个局部契约漂移；替换会分叉现有 Skill authority、沙箱、CAS 和 durable event 语义 |

官方参考：

- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/durable-execution>
- <https://docs.temporal.io/encyclopedia/retry-policies>
- <https://ai.pydantic.dev/tools-advanced/>

### 通用修复与确定性证明

- mandatory non-call recovery 由单个 run-global boolean 改为 exact frontier SHA-256 集合。
  fingerprint 只含 required call kind、机器 mandatory frontier、精确 HTTP continuation action
  与当前暴露工具；同一 fingerprint 第二次 non-call 仍 fail closed，真实 receipt 推进产生的
  新 fingerprint 可获得一次新的两消息纠正。全局 iteration budget 不变，不能无限循环。
- Knowledge Gate runtime projection 仅在 validator 已证明“声明式 worker file + 已准入
  `execute_code`”时保留本地计算控制；非 worker、未验证节点和普通 child 不扩权。
- Backend 的 `stream_terminal.finish_reason` 服从权威 root event 的 `finish_reason`/
  `terminal_reason`；只有无 root terminal 时才使用 transport reason。
- 通用两来源集成测试构造两个相继出现的 exact frontier，各自第一次 length non-call、纠正后
  取得 receipt 并完成；同时保留同一 frontier 二次 non-call 的既有 fail-closed 回归。另以
  非领域纯 projection 测试证明 validated worker 保留计算、未授权 worker 删除计算。

聚焦回归为 `15 passed`；Backend 全量 `224 passed`。Harness 宿主全量的 19 个红灯全部是
当前用户无权读取生产 NFS tombstone 的已知隔离噪声；换成独立 tmpfs 后为
`1836 passed, 1 failed, 772 subtests passed`，唯一失败是生产 Harness 镜像按设计不带
Node 的 CommonJS round-trip，同一测试在宿主 Node 22.23.1 下 `1 passed`。`py_compile`、
`git diff --check`、staged secret/genericity scan 均通过。

本轮功能提交为
`3987613c43405b0347bc8606260abde078b707ba fix: scope delegated frontier recovery`。
clean archive：`/tmp/chat_ds_deploy_3987613c.mAmjOI`；生产 Harness image：
`sha256:4f15d7e8afd7b579d0ab0c7d19b979af076642f68b70a66d470333d3161630fb`；
Backend image：
`sha256:817390d6069315d69aef3bcd471f60d3f91f16ceac8e55cbb3d777127bfd1767`；
revision label 均为完整提交。旧镜像保留 `rollback-pre-3987613c`。

部署前两次确认 active AgentRun/root、enabled/running schedule 和 5173 established
connection 均为 0，SQLite `quick_check=ok`、foreign-key violation 为 0。只依次
force-recreate Harness、Backend；Frontend、四个统一沙箱、egress proxy、Browser、
SearXNG/Valkey 和数据库卷均未替换。部署后三入口 `/`/`api/health`、Harness 与
Backend→Harness `/health`/`v1/models` 均为 200；两容器 restart 0、严重日志 0，数据库
健康且生产仍空闲。

## Round 4：闭式结构化修复的事务替换边界

### 冻结身份与终态

- 被测生产代码：`3987613c43405b0347bc8606260abde078b707ba`；Harness image：
  `sha256:4f15d7e8afd7b579d0ab0c7d19b979af076642f68b70a66d470333d3161630fb`。
- Conversation：`205709a7f8b447119670b6686f2e7601`；root run：
  `7287d853563d46cd949e86727db11ef4`。第一次 setup-only conversation
  `345819b2c94b4e22bad306e8ccf7123f` 在发送业务 prompt 前因维护脚本无权读取外部
  workspace 而退出，没有 root run，不计入 E2E 轮次。
- 原始 ZIP SHA-256：
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`，
  829,621 bytes，1 个 primary 加 18 个 supporting Skills，上传件已持久化到 session
  workspace。primary `SKILL.md` digest 为
  `85ecc2fc48b290596c0cf2153b8268cc9f1a6b4f50ca75fb3989f477c8e7df1b`，orchestrator
  resource digest 为
  `6e8520593ebe68c5fb19c32dcae846f2525668f22d81701b9f201300d41939df`。
- 维护端连续消费 1,556 条 SSE 到正常 EOF；root 的唯一 durable terminal 是
  `run.failed / delegate_step_failed`。运行约 17 分钟，不是浏览器断线、Backend 无
  terminal、Harness timeout、人工取消或全部网站不可达。
- 0 个业务 Markdown：失败发生在 required bootstrap barrier，worker/fan-in、11 个模块、
  strong-final 与 post-merge verifier 尚未开始。

### 对话、Skill 与执行图对比

业务输入继续使用历史手工基线。Harness 正确选择内容寻址的 `healthsim-trialsim` 包，完成
intent，并按声明启动 6 路 bootstrap；不是直接 chat，也没有漏编 workflow。Exact Skill
对 `target_biology_intel` 声明 `opentargets-database` 和 7 个 typed fields：
`target_ensembl_id`、`disease_efo_id`、`overall_association_score`、
`tractability_small_molecule`、`tractability_antibody`、`genetic_association_score`、
`safety_liabilities`。失败发生在该 child 已成功完成闭式 typed projection 之后的外层提交审计。

### Delegate/attempt 逐项结果

| Run | 语义身份 | 终态 | input/output tokens | 结论 |
|---|---|---|---:|---|
| `e6dd9...` | Intent classification | succeeded | 1,547 / 661 | route 正常 |
| `9d6a...` | clinicaltrials bootstrap | succeeded/degraded | 175,214 / 1,369 | 来源级降级后收敛 |
| `5fdd...` | PubMed bootstrap | succeeded/degraded | 262,777 / 3,259 | 来源级降级后收敛 |
| `7047...` | ICH bootstrap | succeeded/degraded | 150,276 / 4,355 | 收敛 |
| `719b...` | FDA bootstrap | succeeded/degraded | 216,299 / 7,174 | 来源级降级后收敛 |
| `c5a6...` | EMA bootstrap | succeeded/degraded | 150,367 / 2,646 | 收敛 |
| `87e12...` | Target biology bootstrap | failed/nonretryable | 352,561 / 1,678 | 内层修复成功，外层误审计已被替代草稿 |

Target biology 共 101 条 debug events。前 5 次迭代中虽然有 POST/脚本来源失败，但也有
4 次成功 POST receipt；因此网络不是共同根因。第 6 次模型在可见长度降级合成中返回
1,068 字符、带 raw pseudo-tool protocol 的污染终态。Harness 正确省略该污染正文并构造
evidence capsule；第 7 次只有 2 条隔离消息、约 16,221 estimated input，模型以
`submit_result_fields` 一次返回精确 7 字段，内层记录
`delegate.result_footer_repair.completed` 与 provisional completed terminal。随后外层
`_run_child` 仍审计累计的第 6 次污染正文，最终以
`delegated_result_footer_structured_repair` 失败。root 正确只列该 blocker，已完成 sibling
均保留，且没有 child cancelled。

### 模拟人工追问后的通用根因

1. **内层 replace 与外层 commit 语义不一致。** 闭式结构化 projector 的语义是用已验证
   typed result 替代污染候选；外层 turn transaction 却仍按 append-only 聚合旧候选。
2. **控制面成功没有形成原子提交边界。** 内层 repair completed 只是事件提示，外层不知道
   哪一段 source turn 已被 supersede，因而权威审计再次看见已被隔离的 raw protocol。
3. **普通 malformed footer 与 protocol-contaminated candidate 不能共用同一保留策略。**
   前者应保留干净业务正文、只剥离坏 footer；后者必须整 turn 丢弃，只提交闭式投影。

### 成熟实现对照与决策

| 问题 | 官方成熟机制 | 决策 |
|---|---|---|
| 失败 attempt 的写入不能污染成功状态 | LangGraph persistence 的 pending writes / task-local retry | adapt：显式标记 superseded source turn，外层原子丢弃该 attempt |
| 已完成 sibling/更早已提交段不能回滚 | LangGraph fault tolerance 与 checkpoint | 保留更早 committed terminal segments，只替换当前污染 turn |
| 易失败活动与最终状态分离 | Temporal Activity/event-history retry | adapt：repair 是 control-plane transaction，不重放整个 child |
| typed output 成为唯一权威结果 | Pydantic AI structured output / validation | 继续采用内部 submitter，外层只提交 canonical validated footer |
| 整体迁移框架 | LangGraph/Temporal/Pydantic AI | reject：局部事务边界可在既有 authority、sandbox、CAS 和 event 模型内修复 |

官方参考：

- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/durable-execution>
- <https://docs.temporal.io/encyclopedia/retry-policies>
- <https://ai.pydantic.dev/output/>

### 通用修复与确定性证明

- footer repair context 新增 machine-owned `replace_invalid_source_turn` 控制位；只有 raw
  pseudo protocol 已被闭式 schema projection 替代时为 true。普通 malformed footer
  repair 保持既有 append/strip 语义。
- turn boundary 将该控制位传给外层 `_run_child`；外层原子丢弃当前污染 turn 的 content
  和 reasoning，只保留更早 committed segments，再提交 canonical structured footer。
  该标志不进入工具 registry、不扩权，也不允许模型自行声明。
- 新增非临床 inventory/shipment 跨层 holdout：模拟污染终态、闭式 structured submitter、
  synthetic canonical release，证明持久结果不含 raw protocol/source body，只有一个合法
  footer；既有普通 footer repair 回归继续证明干净正文不会丢失。
- 聚焦受影响组合为 `311 passed, 96 subtests passed`。隔离 tmpfs 全量为
  `1837 passed, 1 failed, 772 subtests passed`；唯一失败是生产 Harness 镜像不带 Node
  的 CommonJS runtime 项，同一测试在宿主 Node 22.23.1 下 `1 passed`。`py_compile`、
  `git diff --check`、secret scan 与生产逻辑 genericity scan 通过。

本轮功能提交为
`867ebdd9453790af96bd54efd2f7ead968c81aec fix: replace superseded delegated terminal turns`。
clean archive：`/tmp/chat_ds_deploy_867ebdd9.RfTPTD`；生产 Harness image：
`sha256:632069f4cb29b2c77f30f3990e53d35e0c2717199851c84ff97354cb637cad91`；revision
label 为完整提交，旧镜像保留 `rollback-pre-867ebdd9`。

部署前两次确认 active root、enabled/running schedule 和 5173 established connection 均为
0，SQLite `quick_check=ok`、foreign-key violation 为 0。只 force-recreate Harness；
Backend、Frontend、四个统一沙箱、egress proxy、Browser、SearXNG/Valkey 和数据库卷均未
替换。部署后 Harness restart 0，三入口 `/api/health`、Harness 与 Backend→Harness
`/health`/`v1/models` 均为 200；严重启动日志 0，数据库健康且生产仍空闲。

## Round 5

### 冻结身份、对话与 durable terminal

- 被测生产代码为 `0fe27ab6`；Conversation：
  `c8d53cd3f6904e90b88640a9125b7c0b`；root run：
  `6421809b83be4d53a698ddfee550b01c`。用户业务输入仍是历史手工基线，没有注入维护提示。
- 原始 ZIP SHA-256 仍为
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`；primary
  `SKILL.md` SHA-256 仍为
  `85ecc2fc48b290596c0cf2153b8268cc9f1a6b4f50ca75fb3989f477c8e7df1b`。
  exact package 位于该 session 的内容寻址 Skill root，Harness 正确选择
  `healthsim-trialsim/composite_full_protocol_design`。
- root 从 09:11 到 15:37 连续运行约 6 小时 26 分，唯一权威终态为
  `run.failed / delegate_step_failed`；无 child cancelled，0 个 Artifact row。失败不是
  SSE/浏览器断线、1500 秒或 4 小时 stream timeout、统一沙箱缺失、人工取消或所有网站
  共同不可达。
- ground truth 是 200,094 bytes/3,383 行，H1/H2/H3 为 13/74/172、table rows 1,159、
  code fences 92。该轮 required worker barrier 未通过，I/E、Literature、fan-in、11 个
  模块、strong-final 与 post-merge verifier 都未启动，因此没有可做业务结构验收的最终 MD；
  这是明确失败，不能用已持久化的 child results 冒充报告完成。

### exact Skill、执行图与 delegate 明细

Skill 为 comprehensive route 声明 8 个 worker：PICO、Safety、Termination、I/E、AE、Target、
Competitive、Literature，并为各 worker 编译独立 Knowledge Gate、exact tools 和 typed
output fields。实际完成 intent、7 路 bootstrap、PICO/Safety/Termination wave，以及
AE/Target/Competitive wave；Target 是唯一 terminal blocker：

| Run | 语义身份 | 终态 | input/output tokens | 结论 |
|---|---|---|---:|---|
| `56dc8...` | Intent classification | succeeded | 1,547 / 758 | route 正常 |
| `3f4a6...` | PubMed bootstrap | succeeded/degraded | 246,423 / 5,749 | 来源级降级 |
| `bb5ec...` | ICH bootstrap | succeeded/degraded | 152,035 / 5,988 | 收敛 |
| `d44d3...` | CT.gov bootstrap | succeeded/degraded | 175,872 / 2,469 | 收敛 |
| `66214...` | FDA bootstrap | succeeded/degraded | 434,492 / 4,897 | 收敛 |
| `80f30...` | Target bootstrap | succeeded/degraded | 346,580 / 1,793 | 收敛 |
| `d540e...` | EMA bootstrap | succeeded/degraded | 110,333 / 2,748 | 收敛 |
| `5894f...` | Competitive bootstrap | succeeded/degraded | 36,132 / 904 | 收敛 |
| `2807e...` | Termination worker | succeeded/degraded | 1,112,657 / 11,728 | 实质结果已持久化 |
| `55e88...` | Safety worker | succeeded/degraded | 549,594 / 14,986 | 实质结果已持久化 |
| `79f9f...` | PICO worker | succeeded/degraded | 1,843,261 / 33,164 | 实质结果已持久化 |
| `227b1...` | Competitive worker | succeeded/degraded | 1,866,639 / 16,402 | 实质 typed result |
| `af7ef...` | AE worker | 旧 Harness 误报 succeeded/degraded | 1,254,713 / 2,774 | 空 ledger 假完成 |
| `e9538...` | Target worker | failed | 3,135,884 / 44,202 | finalizer 预算耦合 |

Target exact schema 有 11 个 required fields。它完成 7/7 Knowledge Gate groups；末端来源
HTTP 429 被正确封为 degraded gap。第 15–21 轮执行多个有界 evidence-processing 工具，
第 22 轮 tools closed，生成 40,637 visible chars 并以 `length` 停止；Harness 保留前缀并
排入唯一 synthesis-length continuation。第 23 轮生成 7,747 chars、正常 `stop`，但仍无
`RESULT_FIELDS_JSON`。旧逻辑把正文/工具 iteration 与 output finalization 共用同一个
23-turn budget，而且把 synthesis continuation 当作禁止再接 footer repair 的错误恢复态，
于是记录 `delegate.result_footer_repair.unavailable / iteration_budget_exhausted` 并失败。

AE 的已持久化结果只有 3,323 bytes，正文实际停在 “I have ... Let me read ...”，紧跟
`<tool_call>read_file\":{...}` escaped pseudo-call；后面基本全是 Harness receipt ledger。
footer projector 将该 worker 的 13 个 required fields 全部写成 `{}`/`[]`。旧 raw-tool
regex 没识别这个 GLM 方言，coarse object/array schema 又把全空 ledger 当成合法，所以形成
语义 false-positive。Competitive 同批结果为 14,086 bytes，包含实质表格、provenance、
degraded gaps 和非空 typed fields；这证明并行 wave、Provider 和网络并非共同故障。

### 模拟人工追问后的通用根因

1. **执行预算与输出定稿预算耦合。** 完成长正文所需的最后一次 continuation 可以耗尽
   reasoning/tool iteration，但一个已保留正文仍需要独立的、严格有界的 typed commit node。
2. **续写去重与事务性坏尾撤销使用了不同前缀。** 当 length 截断落在半个 typed footer
   上时，collector 会撤销坏尾，deduplicator 却仍可能把续写中的完整替代 footer 一并删掉。
3. **raw pseudo-call 识别没有覆盖 provider escaped JSON-key 方言。** 审计器只接受 `(`、
   `>`、换行、`{` 等 delimiter，没有覆盖 `tool_name\":{...}`。
4. **JSON shape valid 不等于语义完成。** 所有 required fields 都是 empty/null 时，必须有
   明确 zero-result 或 degraded/gap 的实质解释；过程叙述和机器 receipt 不能替代结果。

### 成熟 session-wise Harness 对照与决策

| 问题 | 官方成熟机制 | 决策 |
|---|---|---|
| typed result 占用最后一个普通 turn | Pydantic AI 为 output validator/output tool 单独维护 output retry budget，默认 1，可独立配置 | adopt：exact-one internal submitter 获得独立 finalization slot，不增加普通 reasoning/tool budget |
| partial stream 与 final validation 混淆 | Pydantic AI `partial_output` 区分流式中间值和最终完整值；OpenAI Agents SDK 的 `final_output` 在完整结束前保持 `None` | adopt：只在 retained body 完成后做 final-only schema/semantic audit |
| sibling 成功不能因一个 worker 失败丢失 | LangGraph step checkpoint 与 pending writes 保存同一 super-step 的已完成 sibling | 已采用并保留：所有 completed child results 持久化，root 只封 target blocker |
| 长 activity 的 timeout/retry 粒度 | Temporal 区分 Start-to-Close、Schedule-to-Close、heartbeat，并把 retry 绑定 activity attempt | 已采用/继续：provider stream、batch hard cap、progress lease 独立；本轮不是 timeout |
| 规划、文件卸载、命名 subagent、Skill 隔离 | Deep Agents 的 planning/filesystem/subagent/response_format 和 LangGraph durable runtime | adapt 其边界；reject 整体换栈，避免分叉现有 authority、sandbox、CAS 与 durable event 主链 |

官方参考：

- <https://pydantic.dev/docs/ai/core-concepts/output/>
- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/functional-api>
- <https://docs.langchain.com/oss/python/deepagents/overview>
- <https://docs.langchain.com/oss/python/deepagents/subagents>
- <https://docs.temporal.io/encyclopedia/detecting-activity-failures>
- <https://openai.github.io/openai-agents-python/results/>

### 通用修复、确定性复现、回归与部署

- footer projector 获得一个 run-scoped、exact-one、internal-submit-tool-only 的独立
  output-validation slot。普通推理、工具、HTTP、纠错 budget 均未放宽；provider failover
  也不能重复消费第二个 finalizer。
- synthesis-length completion 可进入 finalizer，并把已保留 prefix 与 unique suffix 的
  合并正文交给 projector。去重器在 typed candidate 被事务撤销时改用相同 clean prefix，
  不再删除完整替代 footer。
- raw protocol audit 覆盖 escaped JSON-key call；typed semantic audit 忽略 Harness-owned
  receipt 行，拒绝全空 required ledger，除非模型正文明确证明 zero-result 或 degraded gap。
- 新增非临床、非固定 route 的 ScriptedProvider 测试：两个工具调用后长正文 length、最后
  continuation 用完主预算仍执行一次 finalizer；半个 footer 的撤销/替代；escaped pseudo-call；
  全空 ledger 拒绝、明确零结果接受、无关实质正文仍拒绝。
- 聚焦三组为 `320 passed, 104 subtests passed`；隔离 workspace/SANDBOX root 的 Harness
  全量为 `1861 passed, 1 skipped, 782 subtests passed`。默认生产 NFS 下的 19 个红灯均在
  被测逻辑前命中 root-owned tombstone；双根隔离的失败 cohort 先行
  `13 passed, 9 subtests passed`，随后全量全绿。`py_compile`、diff、secret 与 production
  genericity scan 均通过。

本轮功能提交为
`36e8ea43dffe2fd29e3d20a372313f91bf2decfb fix: finalize delegated typed results independently`。
clean archive 位于 `/tmp/chat_ds_deploy_36e8ea43.lAJHbD`；Harness image 为
`sha256:09072ee7a688907251a5d4e96a94a08c6aeb791b40be7162423982effb77545c`，旧镜像保留
`rollback-pre-36e8ea43`。部署前两次确认 active root、schedule、5173 established connection
均为 0，SQLite quick_check/FK 正常；只 force-recreate Harness。部署后三入口与
Harness/Backend→Harness health/models 均为 200，restart 0、严重启动日志 0，数据库健康且
生产空闲。Round 5 尚未收敛到 strong-final artifact，按用户授权继续 Round 6，最多 Round 8。

## Round 6：父级重试缺少 validator feedback

### 冻结身份、exact Skill 与 durable terminal

- 被测生产代码为 `36e8ea43dffe2fd29e3d20a372313f91bf2decfb`；Conversation：
  `862eb37670634f5394fab116429fa948`；root run：
  `88d0fd14ec01449cace347fcde4d6858`。维护端消费 1,774 条 SSE，并从 SSE 明确收到
  `run.failed`；运行约 21 分钟，不是页面断线、SSE 无 terminal、provider timeout、沙箱缺失、
  人工取消或共同网络故障。
- 原始 ZIP SHA-256 仍为
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`；primary
  `SKILL.md` SHA-256 仍为
  `85ecc2fc48b290596c0cf2153b8268cc9f1a6b4f50ca75fb3989f477c8e7df1b`。19 个 Skill
  全部安装，Harness 正确选择 `healthsim-trialsim/composite_full_protocol_design`；数据库对话
  只有原始业务问题与明确失败回复。
- intent 与前 6 个 bootstrap 全部完成，Competitive bootstrap 消耗唯一父级重试后仍未
  通过 required barrier。worker、aggregation、artifact synthesis、merge、strong-final 与
  post-merge verifier 均未启动，Artifact row 为 0；不能把 bootstrap 结果冒充终稿。

### exact Skill、执行图与两次 Competitive attempt

| Run | 语义身份 | 终态 | input/output tokens | 结论 |
|---|---|---|---:|---|
| `888039b4...` | Intent classification | succeeded | 1,547 / 730 | route 正常 |
| `51f5ace4...` | ClinicalTrials.gov | succeeded/degraded | 172,560 / 1,754 | 来源级降级后收敛 |
| `8e242d14...` | PubMed | succeeded/degraded | 278,316 / 1,484 | 来源级降级后收敛 |
| `0b2eb151...` | ICH | succeeded/degraded | 151,715 / 5,493 | 收敛 |
| `3ad663d2...` | FDA | succeeded/degraded | 259,867 / 4,575 | 收敛 |
| `80dd5434...` | EMA | succeeded/degraded | 110,194 / 2,590 | 收敛 |
| `1710c8b5...` | Target Biology | succeeded/degraded | 397,911 / 2,751 | 收敛 |
| `2b324409...` | Competitive attempt 1 | failed/retryable | 36,119 / 2,471 | 无 receipt 却填充 7 字段 |
| `ca1d8481...` | Competitive attempt 2 | failed/exhausted | 36,119 / 958 | 全 null，但缺 machine quality ledger |

Exact orchestrator 把该 source 绑定到 `skill:drugbank-database`，要求 7 个字段。该 supporting
Skill 是说明型包：描述需账户/许可的 DrugBank 下载、解析和依赖安装，没有 MCP、可运行脚本、
literal HTTP grant 或声明命令。Harness 正确预载其主说明，但两个 child 的有效模型工具均为空，
没有 evidence dispatch receipt。

第一次模型仍返回 6,389 字符，带合法 degraded `COMPLETION_QUALITY_JSON`，但给 7 个字段全部
填入未经 receipt 支持的事实。Harness 正确以 `agent_contract_noncompliance` 拒绝，且丢弃整个
output transaction。父级随后保留 7 个成功 sibling，只为该 node 启动一次新 child。第二次
模型已把 7 字段全改为 JSON null，并写了文本/legacy degraded 状态，却没有原始 prompt 要求的
精确单行 `COMPLETION_QUALITY_JSON`；因此不能由模型自称的 prose gap 替代机器声明，最终
`delegate_retry_exhausted` fail closed。

### 模拟人工追问后的通用根因

1. **父级 retry 是无反馈的重新采样。** `delegate_step_status` 已持久化第一 attempt 的精确
   terminal reason、failure class 和 validator error，但第二 task 仍获得与第一次完全相同的
   goal/context；被丢弃的输出不会污染新 child 是正确的，丢掉 validator finding 则不正确。
2. **说明型 capability 允许合法 gap，但机器协议不能靠猜。** 无可执行候选时不得编造事实；
   nullable schema 加 exact degraded ledger 可以收敛。旧父级重试没有告诉新 child 上轮究竟是
   哪条输出合同失败，浪费了唯一有界修正机会。
3. **不能用“全 null 自动通过”修复。** 那会削弱 forged/unscoped gap 防线。正确边界是保留
   receipt 和 machine-ledger 审计，只给下一 attempt 最小、脱敏、Harness-owned 的错误反馈。

### 成熟实现对照与决策

| 问题 | 官方成熟机制 | 决策 |
|---|---|---|
| output validator 失败后重试不知道修什么 | Pydantic AI `ModelRetry` 把 validator 错误反馈给模型，并使用独立 output retry budget | adopt：父级唯一 retry 带结构化 validator feedback，不增加次数 |
| fresh retry 与失败草稿隔离 | LangGraph task retry/pending writes 保存成功 sibling，但失败 task-local write 不提交 | 保留：不携带旧正文，只携带 control-plane finding |
| activity 重试边界 | Temporal 对单一 Activity attempt 应用 retry policy，不重放整个 workflow | 保留：只重跑失败 node，7 个 sibling 不重跑 |
| named subagent、planning、filesystem offload | Deep Agents 以 LangGraph runtime 组织这些能力 | 借鉴边界；不整体迁移，避免分叉既有 Skill authority、session sandbox、CAS 与 durable events |

官方参考继续采用：

- <https://pydantic.dev/docs/ai/core-concepts/output/>
- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/functional-api>
- <https://docs.langchain.com/oss/python/deepagents/overview>
- <https://docs.temporal.io/encyclopedia/detecting-activity-failures>

### 通用修复、确定性测试与生产部署

- 所有 declared delegate 类型（intent、artifact binding、bootstrap、worker、aggregation、
  artifact synthesis）的父级重试统一附加一段 Harness-owned feedback JSON：前/后 attempt、
  terminal reason、failure class、validator error。它位于既有 context 中，不更改 node 身份、
  tools、resources、schema、Skill authority、重试次数或预算。
- feedback 只投递到已失败且 retryable 的下一 attempt；URL 被换成内容寻址句柄，credential
  assignment 被遮蔽，控制字符展平，error 上限 2,000 字符。失败正文和 reasoning 继续整段丢弃，
  不允许复制未经验证的值。
- 新增非临床 inventory holdout：首轮不带 feedback；记录一个 retryable typed validator
  failure 后，下一轮只出现一次结构化反馈，包含精确 reason/class/attempt，且 URL/token/password
  原文不可见、总长度有界。既有 nullable machine-degraded、伪造 gap、side-effect retry、batch
  lease、workflow gate 回归共同证明安全边界未放宽。
- 聚焦组合双根隔离为 `290 passed, 188 subtests passed`；完整 Harness 双根隔离为
  `1862 passed, 1 skipped, 782 subtests passed`。第一次非隔离组合的唯一红灯在 provider stream
  之前命中生产 NFS root-owned tombstone，单项双根隔离后通过。`py_compile`、diff、secret 与
  production genericity 检查通过；生产 diff 中 V2.3/疾病/包/source/route 特判为 0。

功能提交为
`70df8b51a34fa767c8cf3badb87b14449c76e872 fix: carry validator feedback into delegate retries`。
clean archive：`/tmp/chat_ds_deploy_70df8b51.WprOt9`；生产 image：
`sha256:3d328d1af220fc51531fe9544685e728fc8eecf047d90686be76339c2323bb1b`；旧镜像保留为
`rollback-pre-70df8b51`。切换前 active root/schedule/5173 connection 均为 0；只替换 Harness。
部署后三入口、Harness 与 Backend→Harness health/models 均为 200，restart 0、严重启动日志 0，
数据库健康且生产空闲。Round 6 仍未产生 strong-final artifact，按授权继续 Round 7。

## Round 7

### 冻结身份、exact Skill 与 durable terminal

- 被测生产代码为
  `70df8b51a34fa767c8cf3badb87b14449c76e872`；user/session：
  `6b5692d4e2484af19e3521fd90c57e13`；Conversation：
  `67119645fa874ecba689c8a61e3874de`；root run：
  `5e494f191ead47a6ad640295cd48e36e`。业务输入仍是历史手工基线，没有向模型注入维护提示。
- 原始 ZIP SHA-256 为
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`；primary
  `SKILL.md` SHA-256 为
  `85ecc2fc48b290596c0cf2153b8268cc9f1a6b4f50ca75fb3989f477c8e7df1b`。Harness
  正确选择 `healthsim-trialsim/composite_full_protocol_design`，完整读取 orchestrator、intent、
  CNS 与 phase 1/2/3 路由资源，编译并执行声明式 DAG。
- root 从 2026-08-01 17:37:13 到 20:17:14 UTC 连续运行约 2 小时 40 分，唯一权威终态为
  `run.failed / delegate_step_failed`。它不是 provider timeout、SSE/浏览器断线、人工取消、
  corrupt tool stream、统一沙箱缺失或共同网络故障。
- intent、7 路 bootstrap、PICO/Safety/Termination wave 均完成；AE/Target/Competitive wave
  中只有 AE 失败。required worker barrier 因此正确 fail closed，I/E、Literature、fan-in、
  11 个模块、strong-final 和 post-merge verifier 未启动；Artifact row 和报告文件均为 0。

### delegate 时间线与精确失败链

| Run | 语义身份 | 终态 | input/output tokens | 结论 |
|---|---|---|---:|---|
| `63dacfa4...` | Intent classification | succeeded | 1,547 / 817 | route 正常 |
| `08d653d5...` | ClinicalTrials.gov bootstrap | succeeded/degraded | 417,248 / 3,082 | 来源级降级后收敛 |
| `1459b44f...` | PubMed bootstrap | succeeded/degraded | 222,196 / 1,272 | 来源级降级后收敛 |
| `3ad712b4...` | ICH bootstrap | succeeded/degraded | 150,887 / 5,128 | 收敛 |
| `2a1a9e0a...` | EMA bootstrap | succeeded/degraded | 110,081 / 2,261 | 收敛 |
| `b48a19ac...` | Target Biology bootstrap | succeeded/degraded | 280,735 / 2,052 | 收敛 |
| `f2789c4a...` | FDA bootstrap | succeeded/degraded | 207,770 / 3,483 | 收敛 |
| `c2095348...` | Competitive bootstrap | succeeded/degraded | 36,146 / 1,135 | 收敛 |
| `556c10e7...` | Safety worker | succeeded/degraded | 875,224 / 16,809 | 实质结果已持久化 |
| `9d41d280...` | PICO/standards/simulation worker | succeeded/degraded | 938,150 / 27,083 | 实质结果已持久化 |
| `599cc4c7...` | Termination worker | succeeded/degraded | 1,174,319 / 31,217 | 实质结果已持久化 |
| `26c28ceb...` | AE adjudication worker | failed | 0 / 0 outer projection | 完整大响应在 producer 边界丢失 |
| `ceab0e0d...` | Target biology worker | succeeded/degraded | 1,889,603 / 17,029 | 实质结果已持久化 |
| `ca86d8dd...` | Competitive worker | succeeded/degraded | 1,732,860 / 26,216 | 有界 partial synthesis 收敛 |

AE worker 的 first-party debug/provider/tool receipt 证明：一次 HTTP 响应已经完整读到 wire
边界，约 191,805 bytes / 191,503 chars；模型按 30K、60K、100K 尝试提高 `max_chars`。
旧 `skill_http_get` 却在 handler 内先执行 `full_body[:max_chars]`，因此 100K 返回仍只有前缀。
随后通用大结果 wrapper 虽写入 `results/`，保存的只是已经截断的 JSON，而不是完整 wire body。
两次 observation 共约 383,610 bytes；retrieval tracker 正确看到一个未闭合
`body_truncated` chain，但该 minified JSON 没有 cursor/offset/page-size 等安全续取坐标，最后以
`response_exceeds_visible_limit_no_safe_page_window` 和
`Delegated required-evidence HTTP retrieval reached a bounded completeness limit` 终止。

### 模拟人工追问后的通用根因

1. **producer 边界先丢数据，consumer 再持久化已来不及。** 大工具输出必须在首次产生完整
   payload 的 handler/middleware 边界无损 spill，不能先截断再由对话 history wrapper 保存。
2. **完整获取与上下文展示是两个独立事实。** `body_truncated=true` 应继续表示模型 inline
   preview 被截断；若完整 wire body 已有受控句柄，则 evidence acquisition 可闭合，但真实
   pagination signal 仍必须独立保持 open。
3. **不能只把 100K 上限调大。** 固定提高 context cap 会在其他大 JSON、日志、文件与模型重试
   中重复放大 token，并仍会在下一尺寸边界失败；需要通用 handle + bounded slice/readback。
4. **回读器不能成为新的 ambient 文件权限或 Skill candidate。** 句柄必须由 runtime 创建并
   精确绑定当前 user/session/run；无句柄时不应出现在模型工具面，也不能满足 Knowledge Gate、
   required capability 或 no-progress 计账。

### 成熟 session-wise Harness 调研与决策

| 当前问题 | 官方成熟机制 | 决策 |
|---|---|---|
| 大工具结果反复占 context 或被截断 | Pydantic AI Harness `OverflowingToolOutput` 的 `Spill`：完整 payload、opaque handle、preview/shape、`read_tool_result(offset/limit/from_end/pattern)`，失败回退 truncate；retry 使用独立 handle | **直接 adopt 边界语义**，在现有 registry/session sandbox 中实现等价的 lossless spill/readback |
| 长线程/子 Agent 的中间文件和大上下文 | Deep Agents `StateBackend`/sandbox backend：thread-scoped VFS、自动大输出卸载、分段回读、显式 namespace/permission | **adapt 隔离原则**；使用现有 user/session `results/` 和 exact runtime ledger，不引入共享 ambient VFS |
| 并行 sibling 成功后一个节点失败 | LangGraph checkpoint 与 pending writes 保存同 super-step 已完成 sibling，恢复时不重跑成功节点 | 已采用：completed child results 保留，root 只封 AE blocker；后续可继续强化 crash-resume checkpoint |
| workflow replay 与外部副作用混淆 | Temporal 把 deterministic Workflow 与 Activity 分开，Activity 必须幂等或 non-retryable | 已采用 effect receipt/fence；spill 是本地事务，不改变 HTTP/POST 的 replay policy |
| 运行/工具/handoff 可观测性与敏感数据 | OpenAI Agents SDK trace 覆盖 generation/tool/handoff/guardrail，并允许关闭敏感 trace data | adopt：只记录 spill source、字符数与 handle SHA，不持久化 payload、URL 或句柄原文 |
| multi-agent state 保存与恢复 | AutoGen team `save_state/load_state`，并警告 running team 的 snapshot 可能不一致 | 参考；当前 append-only durable event + terminal barrier 更符合现有运行时，不整体迁移 |

官方参考：

- <https://pydantic.dev/docs/ai/harness/overflowing-tool-output/>
- <https://docs.langchain.com/oss/python/deepagents/backends>
- <https://docs.langchain.com/oss/python/deepagents/overview>
- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/fault-tolerance>
- <https://docs.temporal.io/>
- <https://openai.github.io/openai-agents-python/tracing/>
- <https://microsoft.github.io/autogen/stable/reference/python/autogen_agentchat.teams.html>

整体决策仍是保留当前 Harness。LangGraph/Deep Agents/Temporal 的整体替换会分叉已经建立的
frozen Skill authority、exact Knowledge Gate、session sandbox/egress、artifact CAS、effect
receipt 和 exactly-one terminal 语义；本轮问题有清晰的 middleware seam，采用成熟模式即可。

### 通用修复、确定性测试与生产部署

- 任意超过工具 cap 的文本结果现在先原子、无损写入 session `results/`，再向模型返回 bounded
  preview 与不可猜 opaque handle。每次调用/重试使用独立 UUID handle；spill 超过 5 MiB 或
  写入失败时显式退回 lossy preview，不谎报完整。
- 新增 `read_tool_result`：只接受本 run runtime ledger 中的 handle，支持最大 20K 字符的
  offset/from-end/literal-pattern 窗口。它用 session root、dirfd、`O_NOFOLLOW`、0600、当前 UID、
  `nlink=1`、regular-file 与 size 检查；伪造、跨 run、symlink/hardlink 均 fail closed。
- 回读 schema 在无句柄时不暴露；创建句柄后才动态加入当前 run/context。它是 prerequisite
  auxiliary，不进入 Skill/KG candidate、mandatory completion、no-progress 或 workspace mutation
  计账。普通聊天、模型路由和 Skill 最小工具面保持不变。
- `skill_http_get/post_json` 在完整 wire body 大于 inline cap 时直接 spill 原始 decoded body，
  返回 `body_spilled_complete/body_retrievable_complete` receipt。presentation truncation 与真实
  pagination 分离；spill 失败保留旧 fail-closed continuation。
- 确定性用例覆盖 GET/POST 成功 spill、persistence failure fallback、pagination 独立 open、
  动态工具暴露、exact handle grant、offset/pattern/from-end、distinct retry handles、伪造/
  cross-run/symlink/hardlink 拒绝和 Knowledge Gate/frontier 不受干扰。
- changed-path 聚焦为 `268 passed`；更宽的 agent-loop/KG/model-routing/workflow 组合为
  `401 passed, 1 skipped`。隔离生产数据后的 Harness 全量运行 1,870 项：1,864 通过、5 项因
  未挂 runtime/reference assets 跳过，唯一 CommonJS holdout 是 Harness 镜像按设计无 Node；
  同一项在宿主 Node 22.23.1 下单独 `1 passed`。`py_compile`、diff、secret、scope 与
  production genericity scan 通过。

功能提交为
`064391529b767a2bb0228a5e74088d4572ad37c0 fix: spill oversized tool results losslessly`。
clean archive：`/tmp/chat_ds_deploy_06439152.LEJAcb`；生产 image：
`sha256:63ddfc85f83dc8aa1d89fc2e51ec80dba42831df6546370f8670a7e9cfdbe95b`；旧镜像保留
`rollback-pre-06439152`。切换前两次确认 active/root run、enabled/running schedule 和 5173
established connection 均为 0，SQLite `quick_check=ok`、FK 0。只 force-recreate Harness；
部署后 revision label 精确匹配完整提交，healthy/restart 0，三入口、Harness 与
Backend→Harness health/models 均为 200，工具注册成功、严重启动日志 0、数据库健康且生产空闲。

Round 7 仍未产生 strong-final artifact。按用户授权，下一轮为全新 conversation/root 的
Round 8；它是本 campaign 最后一轮模型重型 E2E，不能复用失败 run。

## Round 8（最终轮）：producer ceiling、兄弟 frontier 与 terminal phase 泄漏

### 冻结身份、exact Skill 与 durable terminal

- 被测 Harness 生产 revision 为
  `064391529b767a2bb0228a5e74088d4572ad37c0`；Conversation：
  `9ff98843e980458d832629ba9964ec96`；root run：
  `ad98fb353fb240f2b3ab84f345ceb247`。维护端消费 4,132 条 SSE，并从 SSE 收到唯一
  `run.failed`；root 从 2026-08-01 21:10:20 到 2026-08-02 00:13:01 UTC，约 3 小时
  3 分钟。不是页面断线、Harness/provider timeout、人工取消、共同网络故障或沙箱缺失。
- 同一轮的 Attempt A 在任何模型 dispatch 之前发现 Harness `/app/data` 与 canonical host
  data bind 不一致并 fail closed；修复 mount 后仍沿用同一个 Round 8 编号重新创建干净
  conversation/root。该启动前失败不计为 Round 9。通用永久闭环是提交
  `c3f9f582d246d6e63c0af2a6f60e471b9c628267`：Backend/Harness health 发布相同目录
  inode 的 path-free identity，Backend 启动/健康严格比对，Compose 强制 canonical
  `CHATDS_DATA_ROOT/CHATDS_MEMORY_ROOT` 且禁止静默创建错误 bind source。
- ZIP SHA-256 仍为
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`；19 个 Skill
  完整安装，primary route 正确选择
  `healthsim-trialsim/composite_full_protocol_design`。实际 API model 为 `AgentModel`，
  provider metadata context 为 250,368；没有退回 Qwen 或按旧 8K 上下文运行。
- intent、7 路 bootstrap、PICO、Safety、Termination 与 Competitive deep-analysis 共
  11 个 child 成功；Target deep-analysis 和 AE adjudication 两个 required worker 失败。
  required barrier 因而正确 fail closed，I/E、Literature、fan-in、模块报告、strong-final
  与 post-merge verifier 未启动。workspace 只有 Skill ZIP、系统文件和两个 FDA cache，
  没有报告 Markdown；Artifact row 为 2，不能冒充最终交付物。

### delegate 时间线与逐项结论

| Run | 语义身份 | 终态 | input/output tokens | 结论 |
|---|---|---|---:|---|
| `8cb8b84b...` | Intent classification | succeeded | 1,547 / 701 | route 正常 |
| `7b11ff1a...` | ClinicalTrials.gov bootstrap | succeeded | 178,832 / 1,907 | 收敛 |
| `5b19711c...` | PubMed bootstrap | succeeded | 195,699 / 2,273 | 收敛 |
| `259ed530...` | ICH bootstrap | succeeded | 151,339 / 3,926 | 收敛 |
| `9c760c3c...` | FDA bootstrap | succeeded | 230,690 / 5,840 | 收敛 |
| `dcc88704...` | EMA bootstrap | succeeded | 189,261 / 4,421 | 收敛 |
| `e45bea0f...` | Target Biology bootstrap | succeeded | 238,374 / 2,449 | 收敛 |
| `d6b11031...` | Competitive bootstrap | succeeded | 36,168 / 1,525 | 收敛 |
| `9926ef21...` | PICO/standards/simulation worker | succeeded | 694,254 / 15,553 | typed result 已持久化 |
| `9cb29bfa...` | Safety worker | succeeded | 564,549 / 14,183 | typed result 已持久化 |
| `c9e65c41...` | Termination worker | succeeded | 1,432,625 / 25,552 | typed result 已持久化 |
| `75611ddd...` | Target biology deep analysis | failed | outer 0 / 0 | 433,287-byte wire body 被 400K producer cap 截断 |
| `b87044fb...` | AE adjudication | failed | 1,857,573 / 85,860 | tools-closed phase 被动态回读 schema 重新打开，footer finalizer 未运行 |
| `306805b9...` | Competitive deep analysis | succeeded | 1,617,825 / 16,125 | 大结果 spill/readback 后正常收敛 |

Round 7 的无损 spill 在本轮得到正面生产证明：多个 110K--191K 字符的完整 HTTP body 和
delegate result 成功写入内容句柄，`read_tool_result` 能回读 bounded slice，伪造句柄仍被拒绝。
Competitive worker 在多次大结果 spill 后仍成功，说明不是统一网络、provider 或 spill store
故障。

### 模拟人工追问后的三项通用根因

1. **producer ceiling 与 lossless store 上限不一致。** Target 第三个 HTTP response 实际读到
   433,287 bytes，但 `skill_http_get/post_json` 的 producer 固定只保留 400,000 bytes；5 MiB
   spill store 因此永远看不到完整 payload。minified body 又没有可证明的 page/cursor window，
   tracker 只能以 `response_exceeds_wire_byte_limit_no_safe_page_window` fail closed。正确修复是
   区分较小的模型 inline `max_chars` 与较大的完整 wire capture hard ceiling，而不是盲目放大
   context。
2. **一个 terminal retrieval family 错误消费了整个 child 的 degraded synthesis turn。**
   Target 的机器 gap 在其他 exact Knowledge Gate/required sibling frontier 尚未结算时就进入
   tools-closed synthesis；后续 sibling 仍需调用时，唯一 degraded fan-in 已被消费，最后报
   “no remaining synthesis turn”。terminal family 应成为不可变 gap/pending write，不能取消
   独立 DAG sibling。
3. **动态辅助能力绕过了当前 phase policy。** AE 第 18/19 turn 的
   `workflow_forced_tools=[]` 已声明 final synthesis，但此前 spill 产生的 handle 让
   `read_tool_result` 在 schema 构建后被无条件追加，所以 debug 同时出现
   `delegate_forced_synthesis=true`、forced tools `[]` 和 `tool_schema_count=1`。模型返回
   92,526 字符 stop body 与 malformed `RESULT_FIELDS_JSON`；footer repair 明明
   `repair_count=0`、remaining=1，却被 catch-all incompatible gate 拒绝。能力 authority
   不能覆盖 step/phase policy；结构化 output validation 也必须有独立、可观察的一次预算。

### 成熟 session-wise Harness 调研与 adopt/adapt/reject

| 本轮问题 | 成熟官方机制 | 决策 |
|---|---|---|
| 完整大输出在 producer 先丢失 | Pydantic AI Harness `OverflowingToolOutput/Spill` 在工具返回产生时无损保存，再给 preview + opaque handle + bounded reader | **adopt**：GET/POST producer 与 spill store 使用同一硬上限；inline cap 独立 |
| 并行节点之一失败时兄弟状态被吞 | LangGraph checkpoint `pending writes` 在同一 super-step 保存已完成 sibling，恢复时不重跑或取消它们 | **adopt**：terminal family gap 持久保留，exact siblings 先结算，最后 exactly-one degraded fan-in |
| malformed structured terminal | Pydantic AI 将 output retry 与 tool retry 分开，validator finding 可触发独立 bounded output retry | **adopt**：保留现有 exact-one internal footer submitter；phase policy 优先于动态 tool authority |
| 长 run 的 step/event 与 crash 恢复 | Pydantic Harness Step Persistence 记录 append-only boundary/snapshot，但明确不是完整 graph checkpoint；Inspect 在 turn boundary 同时保存 agent、sandbox 与 events，明确不捕获 in-flight tool/external effect | **adapt**：继续使用 durable events/effect ledger/fence；不声称可重放不确定外部 effect |
| session filesystem/shell | Deep Agents backend/sandbox 支持 thread namespace、VFS 与 sandbox-as-tool，并明确 path permission 不能限制 shell/network exfiltration | **adapt**：保留现有统一四槽、session snapshot、无直连网络和签名 egress，不使用 host LocalShell |
| stream 与 UI 观察者生命周期 | OpenAI Agents SDK 要求持续 drain stream，最终输出/持久化可能在最后 visible token 后完成；Semantic Kernel 的 result timeout 不取消后台 orchestration | 已采用：SSE subscriber 与 durable producer 解耦，terminal barrier 独立 |
| 团队状态 | AutoGen 提供 `save_state/load_state`，但警告 running team snapshot 可能不一致 | 参考；现有 per-event durability 与 settled boundary 更严格 |

官方参考：

- <https://pydantic.dev/docs/ai/harness/overflowing-tool-output/>
- <https://pydantic.dev/docs/ai/harness/step-persistence/>
- <https://pydantic.dev/docs/ai/harness/subagents/>
- <https://pydantic.dev/docs/ai/core-concepts/agent/>
- <https://docs.langchain.com/oss/python/langgraph/persistence>
- <https://docs.langchain.com/oss/python/langgraph/fault-tolerance>
- <https://docs.langchain.com/oss/python/deepagents/backends>
- <https://docs.langchain.com/oss/python/deepagents/sandboxes>
- <https://openai.github.io/openai-agents-python/streaming/>
- <https://inspect.aisi.org.uk/checkpointing.html>
- <https://microsoft.github.io/autogen/stable/reference/python/autogen_agentchat.teams.html>
- <https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/agent-orchestration/advanced-topics>

生产 Harness 容器实测 `pydantic_ai`、`pydantic_ai_harness`、`langgraph`、`deepagents`、
`agents`、`autogen`、`temporalio`、`semantic_kernel` 和 `inspect_ai` 均未安装。整体迁移会引入
第二套 agent loop、authority、checkpoint、sandbox、effect 与 terminal 控制面，而本轮三项
问题都有清晰的现有 seam。因此决定保留现有 Harness，采用上述成熟状态语义；Inspect AI 可在
未来作为外部 E2E runner 评估，不作为生产 runtime 替换。

### 通用修复、确定性回归、提交与生产部署

- `MAX_LOSSLESS_SPILL_BYTES` 成为公开的 5 MiB 完整 payload ceiling；GET/POST 共用一个
  bounded async reader。`max_chars` 仍最多 100K，只控制模型 inline 展示；超过 5 MiB 仍明确
  partial-wire fail closed。
- post-dispatch retrieval terminal 先重算 exact KG、standard required 与 legacy required
  frontier。有独立 sibling 时只写
  `http_retrieval.degraded_synthesis_deferred`；所有 sibling 结算后才消费 exactly-one
  tools-closed degraded synthesis。
- 动态 `read_tool_result` 只在 ordinary open phase 自动加入；任何当前
  `iteration_workflow_policy`（尤其 `tools=[]`）都优先。footer unavailable debug 现在列出
  精确 `incompatible_reasons`，不再用一个 catch-all 隐藏 phase 泄漏。
- 新增非临床 inventory holdout：两组 exact HTTPS Knowledge Gate 中第一 family terminal、
  第二 sibling 仍必须 dispatch 后再 degraded fan-in；另一用例先产生 spill handle、完成 bounded
  readback，再进入 tools-closed synthesis，malformed footer 必须获得一次 internal submitter，
  且 terminal turn 不暴露回读工具。GET/POST >410K complete body 和 >hard-cap partial-wire 也有
  确定性测试。
- 聚焦组合为 `137 passed, 62 subtests passed`。完整只读源码、隔离生产 NFS 的 Harness
  `unittest` 共 1,877 项：1,871 通过、5 项因 runtime/reference assets 跳过，唯一 CommonJS
  环境项因 Harness image 无 Node 未通过；同一项在宿主 Node 22.23.1 下 `1 passed`，因此组合
  证据为 1,872 passed + 5 skipped。Backend 全量 235 项中一个既有 multiprocessing timing
  assertion 首轮抖动，隔离单项复跑通过。`py_compile`、diff、staged secret/scope/genericity
  scan 通过；生产代码无 V2.3、疾病、包/session/route/worker/KG/报告名/固定数量特判。

功能提交为
`1d2b7d9ce412f58e9d21acf6f18a56c1ebef419d fix: preserve generic terminal workflow phases`，
并包含其父提交
`c3f9f582d246d6e63c0af2a6f60e471b9c628267 fix: attest shared storage across services`。
clean archive：`/tmp/chat_ds_deploy_1d2b7d9c.lBwXUs`，文件数与 tracked tree 均为 22,452。
生产 Harness/Backend image 分别为
`sha256:d335a4d9afd8becc19ae797330cd0c8f13ebd15128207b7f2ec591e1ac3a3d75` 和
`sha256:c763e8e9d55875117a9a7fa54b9242e5923d23cf77315118229f6ca73c5ba501`，revision label
均精确匹配 `1d2b7d9c...`；旧镜像分别保留 `rollback-pre-1d2b7d9c`。

切换前两次确认 active/nonterminal root、enabled/running schedule 与 5173 established
connection 均为 0，SQLite `quick_check=ok`、FK 0。按 Harness -> Backend 顺序仅
force-recreate 两个服务；Frontend、四槽、Proxy、Browser、SearXNG/Valkey 和数据卷均未重建。
部署后三入口 `/api/health`、Harness 与 Backend->Harness `/health`/`/v1/models` 全 200；
两端 storage identity SHA-256 完全一致且 available，两个容器 healthy/restart 0，数据库仍
健康空闲，严重启动日志 0，三个 HTTP/回读工具均注册。

Round 8 未产生 strong-final artifact，因此业务级 V2.3 验收仍未通过；但它已经到唯一 durable
terminal，三项新暴露的缺陷均完成跨领域复现、通用修复、回归、commit 与生产部署。这里记录的
旧八轮上限随后已被用户于 2026-08-02 的明确新授权替代：可继续 Round 9--13，且每轮顺序运行
V2.3 与肺癌 MDT 两个独立 acceptance case；Round 13 是当前新上限。

## Round 9：紧凑语义计划、运行时完整 IR 与结构化终态预算一致性

### V2.3 case：冻结身份、exact Skill 与 durable terminal

- 被测生产 revision 为 `1d2b7d9ce412f58e9d21acf6f18a56c1ebef419d`；Conversation
  `24239b8bef374c8e9663a0849adafa05`，root
  `0d3a0e9ee41e4153b129cbc4728d7761`，从 2026-08-02 01:09:21 到 07:00:36 UTC
  连续运行约 5 小时 51 分。维护端消费 5,300 条 SSE，唯一权威终态为
  `run.failed / delegate_step_failed`，不是前端断线、人工取消、统一沙箱缺失、共同网络故障、
  provider stream timeout 或主 run timeout。
- V2.3 ZIP SHA-256 为
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`，19 个 Skill
  完整安装；primary route 正确选择 `healthsim-trialsim/composite_full_protocol_design`。
  intent、7 路 bootstrap、PICO/Safety/Termination、AE/Target/Competitive 共 14 个 child
  succeeded。唯一 Literature synthesis child `cf7e005b...` failed，required barrier 正确
  fail closed；fan-in、11 模块、strong-final 与 post-merge verifier 未启动，没有最终报告文件。
- Literature child 已连续完成 16 个普通迭代，原始复杂 typed contract 的 child 输出预算为
  16,384 tokens。第 16 轮进入独立 footer finalizer 后，旧实现却把修复预算固定降为 8,192；
  第 17 轮在 8,192-token 上限正好截断，形成 28,662-character 非完整 JSON tool arguments，
  最终以 `delegated_result_footer_structured_repair_failed` 结束。它不是检索来源不可达，也不是
  registry tool 执行失败；内部 footer submitter 在 dispatch 前即被结构校验拒绝。
- 该 failed child 的 debug 累计了约 186 万 input/output tokens，但 AgentRun 行为 0/0/0。
  原因是 terminal event 自身携带的 cumulative usage 没有并入父级 provisional terminal，
  只有单独 usage event 的旧路径会写回。这是可观测性错误，不是 child 没有实际调用模型。

### 肺癌 MDT case：exact Skill、完整终态与逐项失败归因

- `yangbb` User Skill registry 的独立 case 使用 Conversation
  `4667d323114c4cce94faf861a6ea4347`、root
  `1b8e7dcde41243558178463da601a60a`。输入 raw SHA-256 为
  `eefb885294e6849d1e5ab5ce9f6799a30dfff1b9520761bd403138b7f4b135b7`；`SKILL.md`
  SHA-256 为 `2955c00a456f7ca4215e27091c55ceeca6c84d170e4af99560adb54e0d5b4d42`，
  package 共 36 files，registry row 为 `3ac0ed1c89eb4244ae0ed4a057392865`；runtime 记录的
  viewed/installed digest 完全相同，catalog SHA-256 为
  `4999a8313852a4ed730510f73ad7e3285c1ec13efcd926f32d1867b17da10ac0`。
  本 case 与 V2.3 顺序运行，没有 provider 并发污染。模型路由保持
  `deepseek_v4_pro -> AgentModel`，metadata 将 context 修正为 250,368。
- root 从 2026-08-02 07:06:43 到 19:54:21 UTC 自然运行约 12 小时 47 分 37 秒；维护端持续
  消费 1,095,796 条 SSE，最终且唯一 durable terminal 是
  `run.failed / provider_tool_stream_corrupt_after_content`。AgentRun、event ledger、SSE status
  和 persisted assistant message 四处一致，不是前端断线、人工取消、沙箱/浏览器依赖、网络来源
  不可达或 delegate worker 失败。
- exact Skill 要求 Round 0 数据 gate、11 个专科 Agent 的 Round 1、无条件 Round 2 复核/冲突解决、
  Coordinator Round 3 共识投票和最终 MDT 报告。实际 run 只有 1 个 primary AgentRun、0 child、
  0 delegate、0 artifact；workspace 除 session scaffold/debug 外没有业务文件。因此执行意图在
  planning frontier 即失败，不能把 39,584-character 草稿或前端 tool progress 当作 Skill 结果。
- 旧版把 241 个 instruction units 的完整 runtime Workflow IR 交给模型手写。37 次 LLM finish
  共产生 23 个已装配 tool result：2 次 `skill_view` 成功；20 次完整
  `submit_skill_capability_plan` 进入 handler 后被 deterministic IR validator 拒绝；另 1 次因
  `workflow_ir.skill.version=null` 在 schema preflight 拒绝且
  `actual_dispatch_attempted=false`。语义拒绝中的 14 次甚至返回完全相同的 bounded result hash，
  但旧实现没有独立 plan-validation/no-progress budget，仍可消费 160 次普通 iteration。
  这些 planning handler dispatch 没有执行 worker、出网或写文件副作用。
- 失败计划反复产生 59K--112K 字符级 arguments 和巨大 reasoning，并把控制面继续带入模型历史。
  最后第 37 次调用自身仍正常完成 151,775 input + 97,904 output tokens，产生 181,910 reasoning
  chars；但 28,249-character plan arguments 在末尾不完整（object balance 3、array balance 1，
  0 valid object），以 `malformed_arguments_json` 在 dispatch 前被拒。由于此前同一 frontier 已耗尽
  consecutive no-progress repair，Harness 正确不再重放，最终 corrupt-stream 文案只是终止触发器，
  不是前面 12 小时不收敛的根因。
- 持久 usage 为 4,812,385 input + 2,115,422 output = 6,927,807 tokens；assistant reasoning
  3,084,412 chars，仍未产生业务 artifact。19:03:06 UTC 在 plan 尚未接受时还错误发布了一次
  `artifact_integrity` verifier（空 target，结论为“未产生 artifact”），随后又返回 planning；这是
  旧 `direct_called_tools` 将 attempted 当 accepted、且 length/budget verifier 不认识 pending plan
  frontier 的共同后果。前一问题已由 typed accepted/install-only receipt 修复；终态审计又补上
  `catalog present - installed plan absent` 的 mandatory workflow state，所有普通/length/budget 路径
  均先结算 workflow frontier，artifact verifier 不再跨阶段运行。

### 模拟人工追问后的通用根因与修复不变量

1. **规划投影与执行 IR 混为一体。** 完整 source binding、逐 instruction coverage、result ID、
   output mapping、join/failure policy、counts 与 digest 都是 Harness 可确定推导的运行时事实，
   不应要求模型重复序列化。通用不变量是模型只提交小型语义节点/依赖/连续 instruction range/
   capability 选择；Harness 依据冻结 catalog 确定性展开完整 IR，再复用既有严格 validator。
2. **dispatch 不等于语义接受。** 任何 control-plane tool 只有返回 typed `status=accepted` 才能
   推进 mandatory frontier；preflight、parse 或 semantic rejection 都必须停留在当前 frontier。
   同一 plan 的结构错误最多重试三次，随后发出包含稳定 error code/path 的唯一 durable failure，
   不能降权进入执行面，也不能用 160 个主循环迭代反复碰运气。
3. **handler 接受不等于运行时安装。** control-plane handler 的 `status=accepted` 仍需通过冻结
   catalog 的二次校验和 profile-bound runtime preflight；只有 authority 真正原子安装后才消费
   mandatory frontier。schema/semantic rejection 是模型可纠正的三次有界 budget；runtime/profile/
   live-authority/派生失败则属于独立、非模型可纠正的 install controller，应立即 typed fail closed，
   不能要求模型把同一 JSON 重交三次，也不能先发成功事件。成功安装才提交 tool context、execution/
   workflow/artifact plan 与 receipt frontier。
4. **结构化原始输出与修复预算漂移。** typed child 与其唯一 footer finalizer 必须使用同一个、
   由 required field/schema 结构复杂度计算的 8K/16K/32K 档位；context/provider clamp 仍具有最终
   权威。预算不得按疾病、模型、worker 名或报告类型特判。
5. **机器状态不应回灌大历史。** accepted full IR/worker plan 保留为 runtime-owned state，模型历史
   只接收 digest、节点/输出/worker 数和 capability receipt。被拒绝计划只返回有界 validator
   feedback。这样既不丢执行权威，也避免每轮重复数万字符。
6. **终态 usage 必须单调合并。** child usage 从 standalone usage event 与 terminal payload 两条
   可能重复的路径按字段 cumulative max 合并，并强制 `total >= input + output`，保证幂等且不双计。
7. **catalog revision 是权限 epoch。** reference amendment 必须先撤销旧 plan、worker/aggregation/
   artifact state、脚本/HTTP/MCP/sandbox grants 与 planner receipt，再发布新 catalog。只有候选结构、
   path 与 SHA 全部相同的成功只读 resource receipt 可跨 epoch 复用；新 epoch 的 plan 未 commit 前
   只能看到 progressive `skill_view` 与 planner，旧工具或 worker 不得复活。
8. **编译可接受必须等价于 child wire contract 可传输。** Workflow IR 原先只限制完整 schema 为
   64 KiB，而 delegation 另有限制 128 fields、256-character field name 和 16 KiB 精确 per-field
   projection；这会让 authority 安装后才发现 worker 永远不可调度。现已由
   `delegated_result_contract` 提供共享投影/限额，compact/full IR 都在 compile/install 前使用同一
   字节级边界；16 KiB 精确接受，增加一个 ASCII 或多字节 UTF-8 字节即拒绝，128/129 fields
   边界也在零 dispatch 阶段确定。字段名的非空、唯一、无首尾空白/换行/NUL 与 256-character
   规则也由同一 validator 实施；legacy mapping/list 不再保留字符数计量或静默 strip 的旁路。
9. **DAG 声明是 prerequisite 的唯一 authority。** Workflow IR aggregation/validation/synthesis
   原先会先注入全部已完成 worker，再追加声明依赖，导致独立兄弟分支污染结果。现在仅对
   `selection=workflow_ir` 使用该 step 的 exact `input_worker_ids` 与 `depends_on`；legacy 计划保持
   兼容。两个 worker 但 aggregate 只选择一个、以及 aggregate→synthesize 两个 holdout 均证明
   未声明 worker/aggregate 不再进入 child context。同理，wave dependency 只作为 readiness barrier；
   `A/B` 同 wave 而 `C` 只声明依赖 `A` 时，`B` 不再因 wave 展开而成为 `C` 的数据输入。
10. **所有受支持执行器必须产生同构 artifact receipt。** `run_skill_process` 已能在 sync/close
    返回 path/size/SHA，但 delegated success capture 与 verifier runner 列表漏掉该 tool，合法文件
    会得到空 manifest。两处现已统一纳入 persistent process；真实 workspace 文件的 path、size、
    SHA 和 terminal manifest 由同一验证链闭环，不能由 child prose 冒充。
11. **后置 verifier 不得越过 mandatory workflow frontier。** active standard Skill 的 catalog 已
    发布但 typed plan 尚未安装时，`catalogs - plans` 是一等 planning frontier；它现在由
    `needs_more_skill_workflow()` 与 artifact verifier 共用。普通 stop、length 和 iteration-budget
    terminal 均先记录同一 `workflow_reason`，不能在 plan/worker 尚未结算时发布 artifact verifier、
    action-promise 或 artifact-enforcement。非医疗 portable workflow 的两迭代预算耗尽反例验证：
    一次 invalid plan 后 0 verifier、0 delegate、0 artifact，terminal 保留 pending-plan 原因。
12. **规划 frontier 不应制造空控制事务。** 完整披露后若 standard Skill 的 catalog 既无
    executable candidate、required group，也未要求 Workflow IR，则正文已是全部执行权威；Harness
    直接关闭工具面并允许模型遵循正文回答，不要求一个不会收窄任何 grant 的空 plan call。反之，
    只要存在候选、mandatory group 或 delegated workflow，typed plan 仍是不可跳过的 frontier。
    post-tool closure 会让下一轮编译器发布 exact planner-only surface，不再用通用 workflow
    continuation 把 `skill_view` 重新打开。纯指令 Skill 与脚本/浏览器/写文件三类标准 Skill 的
    holdout 同时覆盖该分界。

### 成熟官方实现的源代码/文档对照与决策

| 当前问题 | 官方实现提供的模式 | 决策 |
|---|---|---|
| 模型手写巨大执行图 | Codex `update_plan` 只接受短小 1 句步骤清单，并明确与完整 Plan Mode 分离；Deep Agents 使用 `TodoListMiddleware`，而 graph/runtime 由框架组装 | **adopt** 小型 plan projection；完整 IR 由本 Harness 编译 |
| 计划、文件与子代理职责混杂 | Deep Agents 将 planning、`FilesystemMiddleware`、`SubAgentMiddleware`/`AsyncSubAgentMiddleware` 分层，默认 `StateBackend`，生产建议 sandbox/backend | **adapt** 其分层；保留现有 content-addressed Skill、session workspace、sandbox lease 与 receipt |
| 长 run 绑定前端 stream | Hermes runs API 用稳定 `run_id` 创建任务，再通过独立 events SSE 订阅；tool/subagent lifecycle 独立于正文 delta | 已有 durable AgentRun/event 主链；继续要求 exactly-one terminal，不整体换栈 |
| sandbox/agent scope 与能力漂移 | OpenClaw 的 agent/sandbox 文档按 session/agent scope 计算可见工具并强调后层限制不能重新授予已拒绝能力 | **adopt** phase policy 优先于动态能力，能力边界单调收紧 |
| subagent 独立上下文与预算 | Claude Code 官方 subagent 配置分别声明 context、tools、permission mode、skills/MCP 与 max turns | **adapt** 独立 child context/result contract；Claude Code 实现闭源，不虚构其内部源代码 |
| provider/validation/execution 错误混成一次 retry | Pydantic AI/OpenAI Agents/LangGraph/Inspect AI 分别提供 structured validation、run/event、checkpoint/pending writes 与 turn-boundary state 模式 | **adopt** provider、validation、execution 三类恢复预算分离；本轮只新增 plan semantic validation budget |
| plan revision 与安装竞态 | OpenClaw `planIntegrity`/fresh-plan digest 比对；Codex/Inspect/OpenClaw 的 update-plan 都只提交小型状态投影 | **adopt** catalog digest epoch、live authority 二次校验、staged candidate + short commit |
| child terminal 与 artifact 证明分离 | OpenClaw subagent registry 区分 execution/outcome/delivery；Inspect `AgentFuture` 保证每个 child settle；Hermes 持久化 typed subagent lifecycle | **adapt** authoritative terminal 绑定排序 artifact receipt manifest、result hash、usage；父级只接受验证后的 terminal |
| SSE/刷新后状态丢失 | OpenClaw event ledger 使用 run-local monotonic seq 与 durable replay；Codex rollout recorder 单 writer；Inspect DB cursor/reconstruction | 已有 durable event 主链，继续要求 terminal/event receipt 为唯一事实，不以浏览器连接决定生命周期 |
| sandbox 文件与网络边界 | Codex 将 filesystem/network permission profile 分离；OpenClaw 做 session-exclusive workspace、canonical path/symlink guard，默认 sandbox network none | **adapt** 一个统一 session sandbox + workspace boundary + signed egress proxy；“只下载”不能替代 URL/method/private-network policy |

官方依据：

本轮只读 clone 的冻结 revisions 分别为 Deep Agents `46ee772b45e1`、Codex
`2b5bdcf67547`、OpenClaw `98c0d9deca5d`、Hermes `845031ad81e4`、Pydantic AI
`2375e5a3120d`、OpenAI Agents Python `fc084ae29cd7`、LangGraph `b2926a0ff958`、
Inspect AI `1ea01a9e1b3c`、AutoGen `027ecf0a379b`、Temporal Python SDK
`646d69e12e1f`、Semantic Kernel `383d102346b7`、OpenHands software-agent-sdk
`abeb884cacac` 与 Agent Canvas `1708efc44608`；这些 clone 位于维护端临时只读参考目录，
未复制进生产源码或镜像。Claude Code 没有可审计的官方开源 runtime，因此只引用其官方文档，
不把第三方复刻实现当作 ground truth。

本轮不是只读项目介绍，而是核对了以下冻结源码控制点：Deep Agents 的
`middleware/filesystem.py`、`middleware/subagents.py`、`middleware/async_subagents.py`；
OpenClaw 的 `agents/tools/update-plan-tool.ts`、`claws/update-plan.ts`、
`claws/update-apply.ts`、`acp/event-ledger.ts`、`agents/subagent-registry.types.ts`、
`agents/sandbox/workspace-authority.ts`、`agents/sandbox/fs-bridge-path-safety.ts`、
`provider-runtime/operation-retry.ts` 与 SQLite transaction/state DB；Hermes 的
`agent/turn_retry_state.py`、`agent/subagent_lifecycle.py`、`agent/tool_executor.py`、
`gateway/delivery_ledger.py`、`tools/todo_tool.py`；Codex 的 `handlers/plan_spec.rs`、
`handlers/plan.rs`、`tools/orchestrator.rs`、`session/turn.rs`、`rollout/recorder.rs` 与
protocol permission models；LangGraph 的 `graph/state.py`、`pregel/_retry.py` 与 checkpoint
base；Pydantic AI 的 `tool_manager.py`、`exceptions.py` 与 Pydantic Graph builder；OpenAI
Agents 的 `run_steps.py`、`run_state.py`、`tool_execution.py`；Inspect AI 的
`_update_plan.py`、deepagent `agent_tool.py`、buffer `database.py` 与 recovery reconstruct。
这些实现共同支持“模型只写小型投影、运行时持有完整状态、校验前零 dispatch、分层 retry、
commit 后发布 terminal/event”的方向；没有任何一个现成库单独提供 ChatDS 所需的
content-addressed Skill + exact capability + workspace artifact receipt 全套语义。

针对终审的三个边界又做了源码级细化：PydanticAI `FunctionSchema` 与 OpenAI Agents `FuncSchema`
都从同一 typed model 同时生成 provider JSON Schema 和 runtime validator，采用其“canonical
contract 单一来源”；LangGraph 只有显式 `add_edge([A,B], C)` 才生成 `NamedBarrierValue`，两个
普通 `A→C`/`B→C` 不是 AND，采用其 exact barrier 语义，并结合 Pydantic Graph 的
`fork_run_id/task_id` 实例隔离；OpenClaw attachment manifest-last 与 Hermes artifact-preservation
transaction 只适合作为 artifact staging 参考，因为 Deep Agents/Codex/AutoGen/Inspect 等上游也
没有同时绑定 path/hash/size/current producer attempt/terminal 的完整 output receipt。本 Harness
因此保留自己的 verified workspace artifact receipt，不把 exit code、stdout 或最后一条 AI message
提升为完成证明。Claude Code 对这些内部机制没有公开源码，仍只记录官方公开契约，不作反推。

- Deep Agents：<https://github.com/langchain-ai/deepagents>、
  <https://docs.langchain.com/oss/python/deepagents/overview>、
  <https://docs.langchain.com/oss/python/deepagents/subagents>、
  <https://docs.langchain.com/oss/python/deepagents/sandboxes>
- Codex：<https://github.com/openai/codex/blob/main/codex-rs/protocol/src/prompts/base_instructions/default.md>、
  <https://github.com/openai/codex/blob/main/codex-rs/collaboration-mode-templates/templates/plan.md>、
  <https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md>
- OpenClaw：<https://github.com/openclaw/openclaw/blob/main/docs/gateway/security/index.md>、
  <https://github.com/openclaw/openclaw/blob/main/docs/tools/multi-agent-sandbox-tools.md>
- Hermes：<https://github.com/NousResearch/hermes-agent>、
  <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/api-server.md>、
  <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/agent-loop.md>
- Claude Code：<https://code.claude.com/docs/en/sub-agents>、
  <https://code.claude.com/docs/en/hooks>
- OpenAI Agents SDK：<https://openai.github.io/openai-agents-python/streaming/>、
  <https://openai.github.io/openai-agents-python/sessions/>
- Pydantic AI / LangGraph / Inspect AI / AutoGen：
  <https://ai.pydantic.dev/output/>、
  <https://docs.langchain.com/oss/python/langgraph/persistence>、
  <https://docs.langchain.com/oss/python/langgraph/fault-tolerance>、
  <https://inspect.aisi.org.uk/checkpointing.html>、
  <https://microsoft.github.io/autogen/stable/reference/python/autogen_agentchat.teams.html>
- Temporal / Semantic Kernel / OpenHands：
  <https://github.com/temporalio/sdk-python>、
  <https://docs.temporal.io/develop/python/failure-detection>、
  <https://github.com/microsoft/semantic-kernel>、
  <https://github.com/All-Hands-AI/agent-sdk>、
  <https://github.com/All-Hands-AI/agent-sandbox>

扩展源码核查没有改变保留当前 Harness 的结论，但进一步限定了实现边界：Temporal 的
deterministic Workflow/Activity 分离、精确 sequence/run handle、RetryPolicy 与不可变 history
close event 适合 **adopt/adapt** 为 `WorkflowIR + exact predecessor receipt + 分层 retry ledger +
唯一持久终态`；它自己的 Workflow sandbox 明确不是安全边界，且 process 结果仍主要是
stdout/stderr/exit code，不能替代 ChatDS 的 workspace artifact receipt。Semantic Kernel 的
`ProcessBuilder` step/event namespace 与 OpenAPI URL validator 可局部 **adapt**，但 Magentic 的
facts/plan/progress ledger 仍是模型 prose，local/Dapr process message 没有 attempt/revision/receipt
身份，也没有可重放 root terminal，不能作为权威图或 exact fan-in。OpenHands 的 immutable
event ID、action-to-observation linkage、append-only event store、显式 terminal enum 和可插拔
Docker workspace 值得 **adapt**；其文件事件与 state HEAD 并非同一事务，默认 Docker network/
volume 也不构成 session 安全或 egress policy，不能整体照搬。三者仍没有任何一个同时提供
任意规范 Skill 的确定性编译、当前 producer attempt 绑定的 path/hash/size/contract receipt、
session filesystem boundary 与统一出网策略。

整体决策仍是保留当前 Harness，而不是引入第二套 agent loop。现有系统已经有更具体的 Skill
content authority、session workspace、sandbox/effect ledger、artifact CAS 和 durable terminal
语义；成熟项目本轮提供的是清晰的边界模式。直接换栈会同时分叉这些控制面，不能自动解决这里的
projection/validation 问题。

### 通用实现、确定性验证与生产切换

- 新增 compact `workflow_plan` schema 与编译器。模型目录只包含 path/unit count、稳定 ID、kind、
  同文档位置和最多 64 字符 preview；不含完整 instruction text/runtime hashes。模型用 inclusive
  range 分组，runtime 绑定 source digest、展开 coverage，注入已选择的 `delegate_task`，派生
  result/output/policy/count/digest，并再次通过完整 IR validator。unknown/stale/cross-document/
  reversed/within-node overlap/heading-only/incomplete/cyclic/unselected 均 fail closed。
- 真实冻结 fixture 的零模型测量：肺癌 MDT 241 units 的完整 catalog 为 104,621 bytes，compact
  catalog 为 37,380 bytes（35.73%）；V2.3 primary 168 units 从 73,226 bytes 降至 25,266 bytes
  （34.5%），两边 exact catalog digest 保持一致。另有 80-section 非医疗卫星 Skill mutation
  验证 rename/domain independence。
- Provider-facing schema 只暴露 compact plan；legacy full IR 仅保留给内部/兼容直接调用者。accepted
  plan 的历史 projection 不含 full IR；三次 rejected plan 在真实 mock stream 中始终只暴露 plan
  tool，第三次发出 stable durable terminal，`delegate_task` 从未提前出现。
- handler-level accepted 之后若 profile/runtime preflight 拒绝，plan frontier 仍保持 required，执行
  工具不会提前暴露，debug/SSE/model history 均只看到同一个失败事实；runtime install controller
  立即给出唯一 durable failure，不消耗 semantic plan retry。确定性非医疗 Skill 分别覆盖
  handler→installer live-file TOCTOU、artifact projection fault、schema-invalid 零 dispatch 和
  amendment 中 replacement plan 先拒绝后成功。
- compact compiler 在接受前使用同一 bounded JSON-Schema validator，限制单 instruction fan-out，
  并把 aggregate 的所有必需后继闭包统一 lowering 到 aggregation stage，覆盖通用
  `retrieve -> aggregate -> synthesize/artifact` 拓扑，避免“计划通过、执行阶段才拒绝”。
- child/finalizer 的 8K/16K/32K 共享预算增加真实 `_run_child` 120→121 分边界测试；usage alias
  忽略负数/boolean 后取有效 cumulative max；成功与失败 authoritative terminal 都绑定 canonical
  artifact manifest。含两个真实 workspace artifact 的测试证明 terminal manifest 与外层 result
  完全一致。
- changed-path 组合在首轮终审前为 309/309；共享 schema/field validator、exact worker/aggregate
  predecessor 与 process artifact 回归合入后，包含受影响 workflow activation 和跨领域管线的
  扩展组合最终为 486/486 passed（1 项预期 skip）。隔离 workspace/data root 且包含 sibling
  `executor` module 的 full Harness 共枚举 1,906 项，唯一错误是 CommonJS/Node holdout 因
  Harness image 按设计不含 Node；把宿主 Node 22.23.1 注入同一隔离容器后，该 exact holdout
  单独通过。因此组合证据覆盖全部 1,906 项，无代码失败。
  `py_compile`、`git diff --check` 与 diff-only genericity 检查通过。
- 通用修复提交为
  `6657f3741ae0bb399333e5039dd2da994864e84b fix: compile generic skill workflows deterministically`。
  部署前连续两次确认 nonterminal AgentRun/root、enabled/running schedule 与 5173 established
  connection 均为 0，SQLite `quick_check=ok`、foreign-key violation 0。candidate 来自 clean
  archive `/tmp/chat_ds_deploy_6657f374.SuZrMf`，文件数与 tracked tree 完全一致；compileall/import
  通过，revision label 精确匹配完整提交。
- 仅替换生产 Harness，旧 image 保留 `rollback-pre-6657f374`。新 image 为
  `sha256:3fbcb23d2c26dbf70fd5469faea7a3418db02faa7d53428b83a392ac79ed5d8a`，healthy/restart 0；
  Backend/Frontend/四槽/Proxy/Browser/SearXNG 与数据卷均未重建。三个 Frontend 入口、Harness
  与 Backend→Harness health/models 全 200，两端 storage identity SHA-256 相同；45 个注册工具中
  planner/delegate/process/readback/HTTP/Python 必需集合完整，严重启动日志 0。部署后数据库仍为
  `quick_check=ok`、foreign-key violation 0、nonterminal root/run 与 schedule 均为 0。

Round 9 至此完成“两项独立 E2E terminal → exact 三源诊断 → 官方成熟实现对照 → 通用复现与修复
→ 完整回归 → local commit → clean-archive 生产切换”的闭环。生产当前空闲，可开始 Round 10；
新的两个 case 必须继续顺序运行并使用全新 conversation/root。

## Round 10：snapshot-local 计划选择器与可恢复 fan-in attempt

### 两个独立 case 与唯一终态

- V2.3 case 使用 Conversation `bc632e897c384f34bfec3433fd477bbe`、root
  `d66b7e4017234ff1853fa7f35dc9224f`。它达到唯一 durable `run.failed`，不是浏览器断线、
  根任务取消、网站统一不可达或普通 worker 未启动：前序 worker 均已成功，最终 I/E Criteria
  child 在读取 required predecessors 的 fan-in 阶段失败。旧 reducer 一次收到约 93,375 input
  tokens，却仍被固定为 8,192 output tokens 与 240 秒 step timeout；provider 实际运行约
  235.86 秒后以 `length` 结束。旧主循环还把内部 reducer 当成普通 Agent 回复，错误启动了
  artifact/report verifier，混淆了“内部 reduction attempt 终止”和“外层 Skill 产物可验收”。
- 肺癌 MDT case 使用 Conversation `cb7515fad602405da4b873ccc37a9ecc`、root
  `09b907e90e534e139bf81424220d3abb`。它在零 worker/tool/artifact dispatch 前耗尽三次
  capability-plan semantic budget：模型第一次复制 unknown opaque instruction ID，第二次形成
  overlapping range，第三次又幻觉出 `iu-211`。模型表达的角色/依赖意图总体合理；失败边界是
  provider schema 要求模型复制 Harness 内部 hash-like identity，而不是 Skill route、沙箱、网络
  或子 Agent 运行失败。
- 两个 case 都按持久化对话、当时 exact Skill/package/resource、workspace debug/AgentRun/tool/
  provider/artifact event 三源交叉取证；本轮没有把前端最后一条提示当根因，也没有把其中一个
  case 的业务名、疾病、文件名、session ID、worker 数或 93,375-token 样本写进生产策略。

### 模拟人工追问后的通用不变量

1. **模型可见选择器只能引用冻结 snapshot 内的短身份。** instruction range 的首选 wire contract
   改为 `{document_id,start_ordinal,end_ordinal}`；`document_id` 由冻结 document binding
   内容寻址生成，ordinal 只在该 snapshot 内有效。runtime 再精确 late-bind 到 canonical
   instruction ID。unknown/stale/fuzzy/mixed/reversed/cross-document 均 fail closed；同节点同文档
   相邻/重叠区间由编译器 canonicalize。旧 internal ID contract 只保留给兼容直接调用者，不再
   暴露给 provider planner。
2. **provider schema 必须精确约束当前 catalog generation。** model-facing schema 以 enum/const
   固定 `skill_name`、`body_sha256`、`catalog_sha256`、可选 document handle、候选 capability ID；
   无候选数组的 `maxItems=0`。纯 instruction/simple Skill 完全不暴露 `workflow_plan`，不制造无法
   收窄 authority 的空控制事务。任何 rename/content mutation 都产生新的 document handle，旧
   ordinal 不能静默重绑。
3. **fan-in attempt 与外层 Agent 生命周期分权。** 每个 reducer attempt 有独立 run ID、input/
   output budget、deadline、权威 failed/succeeded terminal；Backend 将其持久化为 nested
   `delegate_reducer`，不会覆盖 root primary。reducer 使用 `internal_bounded_text` lifecycle：
   tools/session/MCP/artifact authority 全关、ordinary artifact verifier 禁止。
4. **大 fan-in 的 deadline 必须覆盖 admission queue。** output cap 由 provider capacity 与 32 KiB
   byte contract 推导，不再固定 8K；absolute Schedule-to-Close 是唯一最终边界，排队不重置期限，
   单步 fair-share 不会提前杀死健康请求。weighted concurrency 与普通 provider admission 使用同一
   `ProviderAdmissionLimits + estimate_admission_tokens` 公式；超大请求保留 controller 语义并独占
   一个 slot，而不是在 fan-in 前新增特判拒绝。
5. **只有纯 output-contract failure 可做一次 complete replacement。** length、empty、byte
   oversize、raw tool protocol 和 structured coverage invalid 可在 tools-closed reducer 上做一次
   全量替换；rejected body 从不成为 continuation anchor。network/cancel/unknown 不重放，已完成
   predecessor 与外层副作用不重跑。acceptance validator 在 materialization 前后各验证一次。

### 成熟官方源码/文档对照

本轮扩展并复核的冻结官方 revisions 为 Deep Agents
`46ee772b45e1d80e65c26524b0ef05914a503533`、OpenClaw
`98c0d9deca5dff8346677c2a5ae5824301cb3516`、Hermes Agent
`a4a91610b05acc75b4d76c077a5cd89c1ee066ba`、Codex
`2b5bdcf67547860f2e5c5a605009a70026796b2b`；Claude Code runtime 没有官方完整开源实现，
因此只使用 2026-08-03 的官方文档，不把第三方复刻当源码 ground truth。

| 问题 | 官方实现证据 | 本轮决策 |
|---|---|---|
| 大 catalog 延迟披露 | OpenClaw `tool-search.ts` / `tool-search-catalog.ts` / `tool-search-runtime.ts` 使用 compact id、search/describe/call、exact-or-unique binding 和执行前后 schema validation；Codex `tool_search.rs`/`tools/context.rs` 把选中完整 schema 注入下一轮并最终绑定 raw MCP server/tool；Hermes `tools/tool_search.py` 做预算式 full→names→group disclosure 和 current-scope late binding；Claude Code Tool Search 文档声明 top 3–5 与按需 schema | **adopt/adapt** compact shortlist、constrained selection、canonical/raw 两段 binding；ChatDS 自行持久化 snapshot digest/epoch/receipt。拒绝让模型复制 hash suffix 或把 ordinal 当跨版本永久身份 |
| typed subagent 与大结果 | Deep Agents `middleware/subagents.py` 支持 `response_format`，`_message_eviction.py` 将大 tool result 落盘；Hermes `delegate_tool.py` 稳定排序、父余量÷N、完整 summary spill | **adapt** typed partial/artifact pointer；保留 ChatDS 自己的 path/size/SHA/current-attempt receipt，拒绝最后一条 AI prose 作为完成证明 |
| reducer deadline/terminal | OpenClaw `subagent-run-timeout.ts`、`subagent-registry-run-manager.ts` 与 completion capture 提供 absolute deadline、typed timeout/output-limit、execution/outcome/capture settle；Hermes `subagent_lifecycle.py` 有 immutable handle/result hash/typed terminal | **adopt** absolute deadline 和 attempt terminal；ChatDS 编译 exact all-of barrier 与局部 reducer recovery，拒绝 execution-ok 等同 reducer acceptance |
| barrier/checkpoint | Codex legacy `multi_agents/wait.rs` 是 first-final，不是 all-of；Claude Code agent teams 有 dependency/claim，但官方明确 status 可滞后且 in-process teammate 不可完整 resume；其 checkpoint 只覆盖直接文件编辑 | **reject** 把 `wait_agent`、task files 或 rewind 当 durable reducer checkpoint；继续使用 ChatDS WorkflowIR、predecessor receipt、durable event 与 exactly-one root terminal |
| 现成框架替换 | Deep Agents 顶层 `create_deep_agent` 仍把完整 tools 交给 agent，checkpointer 只是传给 LangGraph；上述实现都不自动生成 hierarchical reducer tree，也不同时提供 Skill content authority、session filesystem boundary、egress policy 与 artifact receipt | 继续组件级 **adapt behind existing authority/receipt contracts**，不整体换栈、不再闭门猜机制 |

官方入口：

- Deep Agents：<https://github.com/langchain-ai/deepagents>
- OpenClaw：<https://github.com/openclaw/openclaw>
- Hermes：<https://github.com/NousResearch/hermes-agent>
- Codex：<https://github.com/openai/codex>
- Claude Code Tool Search / Agent Teams / Checkpointing：
  <https://code.claude.com/docs/en/agent-sdk/tool-search>、
  <https://code.claude.com/docs/en/agent-teams>、
  <https://code.claude.com/docs/en/checkpointing>

### 回归、提交、模型兼容性与生产切换

- Round10 通用修复提交为
  `45e131e3422dbb611ea79b3578dda8d5ad65ae82 fix: bound generic planning and fan-in lifecycles`。
  生产代码 genericity scan 对业务/会话/固定样本字面量为 0；credential-like added line 为 0。
- Backend 全量为 `237 passed`。隔离 root Harness 在排除生产 Harness 镜像按设计不含 Node 的
  唯一 CommonJS 环境项后为 `1925 passed, 1 deselected, 800 subtests passed`；该 exact 项在
  宿主 Node 22.23.1 下 `1 passed`，组合覆盖全部 1,926 项。定向 planning/fan-in/schema/lifecycle
  组合、`py_compile`、Compose config 与 `git diff --check` 通过。
- 用户随后要求暂停下一轮双 Skill E2E，增加 Shaiengine 的 `glm-5.2` 与
  `deepseek-v4-pro`。两模型的 OpenAI nonstream/stream、强制 tool call、完整 JSON arguments、
  usage terminal 与 thinking enabled/disabled 均真实通过；启用时返回 `reasoning_content`，禁用时
  reasoning 为 0。Anthropic Messages 的 text/tool stream 也可用，但 tools turn 对
  `thinking.type=disabled` 仍发 `thinking_delta`，且 nonstream 不披露 thinking block，因此生产主
  transport 选择 OpenAI compatibility。wire projection 按 provider 显式区分
  `thinking_object` 与 vLLM `chat_template_kwargs`，不再用一个 capability bit 猜请求格式。
- 模型接入提交为
  `0108c664443665b5748f2c3933f420ac79f9190d feat: add compatible remote agent models`。
  新默认为 `shaiengine_glm_5_2 -> glm-5.2`；历史 `AgentModel` 永久保留
  `deepseek_v4_pro -> local AgentModel`，默认切换不会重绑旧会话。API key 只存在于权限 0600
  的生产 `.env`/受限 local secret，不在 Git、文档、日志或 debug 中。
- clean archive `/tmp/chat_ds_deploy_0108c664.BpAKFl` 与 Git tree 均为精确 22,452 files。
  Harness/Backend 候选 image 分别为
  `sha256:10d65e46efb53a7698a92d2c4835f149131e485bce5855276aff56cf6af457a8`、
  `sha256:1adb71c272df3b3f52cec172e4df7cbdac24d9b8c6d877e7fe9be841c5505b3d`，revision label
  都精确为 `0108c664...`。部署前连续两次确认 nonterminal root/run、running/enabled schedule、
  5173 established connection 均为 0，SQLite/FK 正常；只按 Harness→Backend force-recreate，
  旧镜像保留 `rollback-pre-0108c664`，其他服务/数据卷未重建。
- 部署后三入口 200、Backend→Harness health/default catalog 正常、storage identity 相同；两模型
  从生产 Harness 真实请求均为 200 并返回 reasoning，所有长期容器 restart 0，Harness/Backend
  严重日志 0，数据库仍 `quick_check=ok`、FK 0、nonterminal root/run 与 schedule 0。

Round10 至此完成两个 Skill terminal 的三源诊断、通用修复、成熟官方实现对照、完整回归、本地
提交、clean-archive 部署与生产 smoke。按用户最新要求在此暂停 campaign，不启动 Round11。
