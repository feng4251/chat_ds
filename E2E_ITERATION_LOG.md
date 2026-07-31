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

## Round 4

待执行。

## Round 5

待执行。
