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
