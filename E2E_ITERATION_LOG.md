# 通用 Skill Harness 五轮 E2E 迭代日志

本文件记录 2026-07-31 获得用户明确授权的五轮 V2.3 复杂 Skill E2E campaign。
V2.3 只作为业务压力测试和结构/质量 oracle；任何生产修复都必须先重述为跨领域
Harness 不变量，并由通用合成测试与非临床或 mutation/rename holdout 证明。每轮只有在
新的 root run 到达 durable terminal 后才计数，同一 run 的重试不算新一轮。

自 2026-08-04 起，成熟 Harness 对照步骤的唯一实现参考改为本地独立仓库
`claude-code/`，每轮必须记录其 exact commit 和与问题相关的实际代码路径。只有相关代码为 stub、
调用链断裂或存在真实语义疑点时，才允许围绕该疑点进行最小化 Web 补证，并分别记录源码证据、
Web 补证和最终取舍；不恢复 OpenClaw、Hermes 等其他 Harness 的常规轮询。stub 先记为未知边界，
不能自行推断缺失实现。
以下历史轮次保留当时真实使用的官方/多框架对照，不追溯改写。

每轮自动模拟以下人工排障追问链：

1. 这个 session 在做什么、在哪里失败或异常？
2. 同时对比持久化对话、当时 exact Skill/package/resources、debug/AgentRun、工具调用、
   provider 思考/回复与 workspace artifacts，执行意图是否符合 Skill？
3. 逐个解释 delegate 的 succeeded/degraded/failed/cancelled，而不是只读前端终态。
4. 对每个问题先定义通用根因、可观察信号、确定性复现和彻底修复思路。
5. 对照冻结 `claude-code/` 中与问题相关的实际代码路径，明确 adopt/adapt/reject。
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

## Round 11：same-package planned-resource closure 与 child quality 单一权威页脚

### 两个独立 case、exact Skill 与唯一终态

- V2.3 使用 Conversation `49791ec4ef37449c84b7c1611e256a06`、root
  `b75a71b3dbdd48f58dd76ec31a4a3b46`，从 2026-08-03 03:42:27 到 03:57:04 UTC 达到唯一
  durable `run.failed/delegate_retry_exhausted`。持久化用户输入仍为 64 字符基线，实际冻结
  `healthsim-trialsim` primary 与 orchestration 声明了 intent、7 路 bootstrap、后续 worker DAG、
  11 模块和 `_FULL_REPORT.md` merge。debug/AgentRun 证明 intent 与
  ClinicalTrials/PubMed/ICH/FDA/EMA/Target Biology 六项来源均成功；最后的
  `competitive_intel` attempt `53a54038...` 因填充 `drug_name/drug_type/mechanism/target_genes/max_phase`
  却没有 evidence receipt 被正确拒绝。fresh retry `2d505060...` 已改为无事实漂白的降级内容，
  但模型同时输出两个各自严格合法的 `COMPLETION_QUALITY_JSON`，旧 exact-one parser 将整项拒绝。
  mandatory bootstrap barrier 因而 fail closed，worker/fan-in/final artifact 均未启动，Artifact 为 0。
- 肺癌 MDT 使用 Conversation `b830029d282447cf8abcce196c7d6b41`、root
  `941e09a080694159ac6d45c205b2d7e0`，从 03:58:32 到 04:02:35 UTC 达到唯一 durable
  `run.failed/skill_result_contract_invalid`。持久化输入 raw SHA 仍为
  `eefb885294e6849d1e5ab5ce9f6799a30dfff1b9520761bd403138b7f4b135b7`，exact User Skill
  `SKILL.md` SHA 为 `2955c00a...`、36-file tree digest 为 `200708f8...`。前两次 compact semantic
  plan 被 IR validator 拒绝，第三次已 accepted；随后在零 child/tool/artifact dispatch 前，runtime
  installer 报 `unconditional_capability_selector_unresolved`：`round0_data_gating` 的
  `SKILL.md` 与四个 `references/*.md` exact path 均真实存在于同一冻结 package，但旧 selected-resource
  closure 只收集 `local_resources`，没有把 accepted worker capability 中的 path-shaped selector
  转换为 run-owned resource authority。Artifact 为 0。
- 两项均按持久化对话、exact immutable Skill/package/resource、debug/AgentRun/tool/provider/artifact
  三源交叉取证。V2.3 不是共同网络或沙箱失败；肺癌 MDT 更在零执行 dispatch 前失败。前端文案、
  模型 prose 与网站动态状态均未被提升为控制面事实。

### 模拟人工追问后的跨领域不变量与逐 attempt 归因

1. **accepted plan 引用的同包精确资源必须在 install 前形成 authority closure。** 只有形如安全
   relative file path 的 ordinary worker selector、且能在同一 immutable package 中精确解析并绑定
   content digest，才可进入 selected resources。`skill:*`、command-like selector、目录、glob、
   traversal、symlink 或未选择文件均不得借此获权；加入所有来源后还要重新检查完整 256-resource
   上限。accepted plan 与 runtime install 必须使用同一冻结 generation。
2. **机器完成质量应只有一个 canonical authority，但多个严格合法的候选不应触发整工作流重放。**
   若每个可见候选都通过 exact schema，可在 typed boundary 收敛为一个 canonical ledger；状态冲突
   保守取 `degraded`。任一 malformed/oversize 候选都保持原样并由 strict parser fail closed；code
   fence 中的示例不算候选。canonicalization 不能越过 evidence receipt audit，也不能把 populated
   unverifiable facts 洗成合法降级结果。
3. **校验恢复与外部副作用重试分权。** 本轮 V2.3 attempt 1 的事实/证据冲突仍失败，attempt 2 的
   页脚重复只在 final typed-output transaction 内 canonicalize；不会重跑已成功的六项 bootstrap，
   也不会扩大工具、网络、文件或 Skill authority。肺癌 plan install 则必须在任何 worker dispatch
   前原子失败或成功，不能留下半安装能力。

逐 attempt 结论：V2.3 的 intent `d9ff5bb5...` 与六个 bootstrap child 均 succeeded；
`53a54038...` 是正确的 evidence-contract failure；`2d505060...` 是 provider 输出冗余但可确定性
收敛的 typed-footer failure。肺癌 MDT 没有 delegate attempt；其 122,386 input / 12,951 output
tokens 全用于 root planning，失败发生于 accepted semantic plan 的 runtime install boundary。

### 成熟官方实现对照与取舍

本轮沿用并复核冻结的官方源码 revisions：Deep Agents
`46ee772b45e1d80e65c26524b0ef05914a503533`、Codex
`2b5bdcf67547860f2e5c5a605009a70026796b2b`、Pydantic AI
`2375e5a3120d19b12bd6b2706815bb61dfbbf66e`、OpenAI Agents Python
`fc084ae29cd751b801c2779c9ebd23ff6bad1668`、Temporal Python SDK
`646d69e12e1f9a134f3abc1eb3a9c750e5ddfe32`，并继续以 OpenClaw/Hermes/LangGraph 的既有
冻结源码作为 durable event/retry 对照。

| 本轮问题 | 官方模式 | 决策 |
|---|---|---|
| plan path 与 runtime authority 脱节 | Deep Agents `FilesystemMiddleware`、`CompositeBackend`、`StateBackend` 将路径操作绑定当前 backend；Codex workspace-write policy 将可写/可读路径限制在 canonical roots | **adapt** exact same-package selector→content-digest authority；比普通 root containment 更严格，继续拒绝 directory/glob 隐式授权 |
| provider 产生重复 structured footer | Pydantic AI output validation/`ModelRetry` 与 OpenAI Agents `output_type`/`AgentOutputSchema` 在最终 typed boundary 验证模型输出 | **adapt** 在 final typed transaction 内做保守 canonicalization；不把 raw prose 或重复页脚本身升级为完整 child 重放理由 |
| completed predecessors 被局部校验问题牵连 | Temporal Activity `RetryPolicy` 与 durable activity state 把重试绑定当前 activity/attempt，不重跑已完成 predecessor | **adopt** 当前 child/output transaction 的 retry ownership；保留 ChatDS exact receipt、effect ledger 和 root terminal |
| 是否整体换栈 | 上述库没有同时提供 content-addressed arbitrary Skill compiler、session workspace、exact egress、evidence/artifact receipt 与 durable terminal | **reject** whole-stack replacement；继续在现有 authority/receipt contract 后组件级吸收 |

官方入口：Deep Agents filesystem/subagents
<https://github.com/langchain-ai/deepagents/blob/master/libs/deepagents/deepagents/middleware/filesystem.py>、
<https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py>；
Codex Python workspace sandbox
<https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md>；OpenAI Agents typed output
<https://openai.github.io/openai-agents-python/agents/>；Temporal retry
<https://docs.temporal.io/develop/python/failure-detection>；Pydantic AI output
<https://ai.pydantic.dev/output/>。

### 通用实现、确定性复现、回归与生产切换

- `harness/knowledge_gate_runtime.py` 从普通 worker 的同一 normalized capability surface 派生
  path-shaped resource selector；排除 `skill:*` 和 parenthesized command selector。
  `harness/agent_loop.py` 在 runtime compile 前将其加入 exact selected-resource closure，仍经过 lexical
  path、immutable package snapshot、digest lowering，并在 worker/bootstrap/aggregate 来源全部加入后
  再执行 256 项全量上限。
- `harness/tools/delegation.py` 只 canonicalize 多个严格合法的 completion-quality ledger，冲突状态
  保守选择 `degraded`，canonical ledger 位于 terminal `RESULT_FIELDS_JSON` 前。malformed duplicate
  不变、code-fenced fake 忽略；safe debug 只记录候选数、状态和是否 canonicalized，不持久化 reason。
  既有 evidence audit 仍先于成功终态。
- 确定性非医疗 `inventory-review` holdout 首先复现 accepted worker 同包 reference install failure，
  修复后证明 selected file 得到 digest authority、未选择文件不获权。Scripted/typed-output 测试证明
  complete+degraded 两页脚收敛为一个 degraded，malformed duplicate 继续失败，code-fenced fake 忽略，
  populated facts 无 receipt 仍失败。
- 受影响组合最终 `260 passed, 182 subtests passed`。默认宿主 full run 的 19 个失败全部为不可读生产
  NFS tombstone 的既有环境噪声；使用 tmpfs `/nfs/temp/chat_ds` 的隔离 Harness full run 为
  `1929 passed, 3 warnings`。clean candidate 组合为 `259 passed, 1 skipped`，skip 仅因 clean tracked
  archive 不包含未跟踪 V2.3 reference archive。`py_compile`、`git diff --check`、staged secret、
  protected-deletion 与 genericity scan 全部通过。
- 通用修复提交为
  `ca9f5eac235cb924d3860826482df032d2a542fb fix: bind planned resources and canonicalize child quality`。
  clean archive `/tmp/chat_ds_deploy_ca9f5eac.paRTS7` 与 tracked tree 均为 22,452 files；candidate image
  `sha256:c5b07eabae3e4a8af182965c9c0268558e4c37e87647e9e13d4131375b61282d` 通过 compile/import/test，
  revision label 精确匹配完整提交。
- 部署前连续两次确认 nonterminal root/run、schedule 与 5173 established connection 全 0，SQLite
  `quick_check=ok`、FK 0。仅 force-recreate Harness；旧 image 保留
  `rollback-pre-ca9f5eac`，Backend/Frontend/四槽/Proxy/Browser/SearXNG/Valkey/数据卷均未重建。
  部署后三入口、Harness、Backend→Harness health/models 全 200，storage identity 相同，Harness
  healthy/restart 0，严重日志 0，数据库仍健康空闲。

Round 11 至此完成双 Skill terminal、三源诊断、逐 attempt 解释、成熟官方实现对照、通用复现与
跨领域修复、完整回归、本地 commit、clean-archive 部署和生产 smoke。用户已明确授权继续
Round 12--15；每轮仍使用全新 conversation/root 且两项顺序运行。

## Round 12：真实 fan-in 元数据包络与 semantic root package authority

### 两个全新顺序 E2E 与三源诊断

- V2.3 使用 conversation `9bb4a0173fc44c5b94cb4258b2a17ab7`、root
  `f96df86c12744cc5bd4cafc176ec6a8f`、原始 64 字符 prompt 和默认
  `shaiengine_glm_5_2`。ZIP 安装仍正确呈现 1 个 primary + 18 个 supporting Skills。
  Intent 和 7 个 bootstrap 全部成功，包含 Round 11 曾失败的 `competitive_intel`；worker wave 中
  Safety、Termination、AE、Target、Competitive 和 Literature 全部成功。PICO 首次 attempt
  `12ca8512a61e48d49522febae47bb919` 与唯一 clean retry
  `685df30f0b744cbb820415c15bd1dc9f` 均为 0 input/0 output token，父级 lossless spill 保留同一
  retryable structured error：`internal fan-in planning error: final reduction artifact does not fit the
  final child budget`。最终 14 succeeded child attempts、2 failed attempts、288 completed tools、
  0 artifact；required barrier 正确 fail closed，没有伪造最终报告。
- 精确源码路径证明旧 `_safe_reduction_output_allowance` 用固定两个短 `immediate_input_ids`、固定
  dummy path/range 和 0 数字宽度估算，而真实单 reducer leaf 将同批全部长 result IDs 写入 metadata。
  output body 吃满旧虚拟 cap 后，真实 final batch 必然超限。错误发生于本地 deterministic planner，
  不是 provider、网络、sandbox、timeout、前端 disconnect 或 remote database。
- 肺癌 MDT 使用 conversation `265ffb56b04141fe99e1281ab2811e7d`、root
  `424100dd5ffd4d10afbc1224f1a7f877` 和与历史 user case 完全相同的 7,089 字符 prompt。完整
  `SKILL.md` digest 为 `2955c00a...`，semantic plan 一次 accepted，随后在首个 sequential worker
  `overview` 前以 `skill_result_contract_invalid` 终止：ordinary selector `SKILL.md` 没能 lower 为
  exact run-owned candidate，0 child/0 artifact。
- Debug 显示安装后的 `allowed_skill_resources` 已有 exact main，但 native-only selection 的
  `allowed_skill_package_digests` 为空；`_resource_candidate` 同时要求 main digest、complete package
  digest 和 live snapshot，因而正确拒绝。它不是 path validator 拒绝 main，也不是网络/模型问题。

### 成熟官方实现对照与取舍

| 对照 | 官方实现要点 | 本轮吸收 |
|---|---|---|
| Deep Agents FilesystemMiddleware | State/Store/Sandbox/Composite backend 明确分离，large tool result 和 conversation history 以 backend 路径 materialize | fan-in body 与 metadata 分账，persisted result 不回灌成无边界 prose |
| LangGraph durable execution | task/subgraph result checkpoint 后恢复，side-effect task 要求 deterministic/idempotent | deterministic planner bug 不做第二次相同 model retry；用本地复现修 planner |
| Temporal Python SDK | ApplicationError 可显式 non-retryable；错误分类决定 workflow/task retry | 将 0-token 相同 planner exception 认定为代码类 deterministic failure，而非 provider retry |
| OpenAI Agents SDK | RunContext 与 model-visible context 分离；Sandbox manifest/path grant 由 runtime 持有 | package snapshot/digest 保留在 runtime authority，不要求模型复制或自签根包身份 |

官方源码/文档：Deep Agents filesystem
<https://github.com/langchain-ai/deepagents/blob/master/libs/deepagents/deepagents/middleware/filesystem.py>；
LangGraph durable functional API
<https://docs.langchain.com/oss/javascript/langgraph/functional-api>；Temporal Python SDK
<https://github.com/temporalio/sdk-python>；OpenAI Agents context/sandbox
<https://openai.github.io/openai-agents-python/context/>、
<https://github.com/openai/openai-agents-python>。访问日期均为 2026-08-03。

### 通用实现、复现、回归和生产切换

- `harness/result_fan_in.py` 升级 planner v3/output policy v4。根据真实 source batches 构造固定宽
  placeholder plan 下的 exact leaf IDs/path/source range/immediate lineage，再构造相同的 stable
  ordered balanced-tree；按最终 artifact metadata 和每一对 merge input metadata 分别计算 token/byte
  body cap。数字字段使用 final budget 上界的位宽 envelope，实际值只能更短；placeholder/final hash
  字符串同为 ASCII 固定宽，因此两次 content-addressed 计算一致。没有扩大 provider context、删来源或
  捕获后继续。
- `harness/agent_loop.py` 在 accepted standard semantic plan 的 live-authority revalidation 后再次检查
  run-frozen root snapshot。安装 closure 无条件加入 exact `(skill, SKILL.md)` 与同一冻结 package
  digest；supporting files 仍只能由已选 candidate/route 精确加入，directory/glob/ambient package browse
  均未开放。
- 非医疗 `release-ledger` 8-source holdout 在修复前稳定重现 single-reducer final overflow，修复后生成
  1 source batch/1 reduction step/final artifact。通用 `portable-skill` native-only semantic plan 在修复
  前证明 main resource 存在但 package digest 为空，修复后 exact resource+package authority 同时存在。
  精确肺癌 package 的零模型 compiler probe 也解析为 content-addressed `skill_resource` candidate。
- 定向组合为 `133 passed, 22 subtests passed`，跨域 pipeline、execution compiler 和相关 AgentLoop
  组合通过。完整 clean tmpfs 容器为 `1929 passed, 1 failed, 800 subtests`，唯一失败是 Harness image
  按部署设计不含 Node 的 CommonJS executor test；同一项在宿主 Node 22.23.1 单独 `1 passed`，所以
  本轮覆盖的全部 1930 项逻辑均通过。候选 clean image 的 133+22、compileall/import 均通过。
- 代码提交为 `0406ab72ae48069f923304798f4b34003b82c107`。clean archive
  `/tmp/chat_ds_deploy_0406ab72.fclvYr` 精确包含 22,452 tracked files；candidate/production image
  `sha256:48dfa72457b2db76284a18f4bf11f241c354b218241825227f902f9e63cfcbad`。
- 部署前两次确认 nonterminal Agent/Schedule、5173 established connection 均为 0，SQLite
  `quick_check=ok`、FK 0。只 force-recreate Harness；Backend/Frontend/四槽/Proxy/Browser/
  SearXNG/Valkey/数据卷均未重建，旧 image 保留 `rollback-pre-0406ab72`。部署后 Harness
  healthy/restart 0/revision 精确，三入口均 200，容器内与 Backend→Harness health/models、storage
  identity、数据库 idle 和严重日志 smoke 全部通过。

Round 12 至此闭环。下一项是从生产 `0406ab72` 开始 Round 13，仍必须先 V2.3、后肺癌 MDT，使用
两个全新 conversation/root；若出现问题继续按 exact Skill + conversation + debug/tool/result 三源、
确定性复现、成熟官方方法对照、跨域 holdout、通用修复、完整回归、local commit 和 clean deploy 闭环。

## Round 13（进行中）：downstream-capacity fan-in 与 exact evidence handle

### V2.3 首个验收 run、三源诊断与逐 attempt 结论

- 本轮开始前生产已先后部署通用提交 `7031d8b3`（分离 reducer wire generation budget 与 accepted
  artifact contract）和 `b819f41f`（收敛多语言 fan-in token/byte 校验），Harness image 为
  `sha256:652362287696d4a2249fcbaf286178ab9b2cc775d85cd6ff09b9a781f16b2960`。V2.3 使用全新
  conversation `a1fb209ffa0f4e7d8135f2959242b1b1`、root
  `ac3e33dfb62b46ba8a8ee67bff3738c0`、历史 64 字符原始 prompt、默认
  `shaiengine_glm_5_2`，冻结 ZIP SHA-256 为
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`。exact route 只选中
  `healthsim-trialsim/composite_full_protocol_design`，没有错误激活 sibling Skill。
- run 从 09:14:24 到 10:27:46 UTC 运行约 73 分钟并达到唯一 durable
  `run.failed/delegate_step_failed`。Intent、7 路 bootstrap、PICO、Safety、Termination、AE 与
  Competitive 共 15 个 child/reducer attempt succeeded；Target Biology
  `945d95019cc746fb86a1058a64b10a3f` 是唯一 failed child（1,177,301 input / 32,046 output tokens，
  `required_capability_not_attempted`）。根任务正确 fail closed，workspace 没有业务 Markdown，results
  仍保留全部成功 child、HTTP body spill、execute receipt 和 fan-in 输入，因而不是前端断线、容器重启、
  总 timeout、sandbox 断网或“所有网站不可达”。
- PICO 的 8-source reducer 首次 attempt `e7fdac1e...` 返回 37,574 UTF-8 bytes / 9,440 conservative
  tokens。它小于真实 final consumer 的 676,388-byte / 10,330-token allowance，也小于 10,076-token
  semantic ceiling，却只因旧 32 KiB 常量扣除 coverage/footer 后形成 31,457-byte semantic ceiling 而
  被拒绝；唯一 complete replacement `6d11de85...` 压到 8,293 tokens 后 accepted。首次 body 没有被错误
  commit，source manifest/checksum 也未丢失，但这次重跑没有容量依据：31,457 来自历史静态 artifact
  byte contract，不是 GLM-5.2 的 300K context、provider max output 或 downstream consumer 容量。
- Target Biology 的 Knowledge Gate 共 7 个 activated group。debug 序列证明前六组已有机器回执；剩余
  mandatory frontier 时，模型第一次返回 prose，Harness 正确给一次 context-isolated non-call recovery。
  随后 `skill_http_get` 实际 HTTP 200，安全回执显示 `matched_skill=clinvar-database`，但公共 schema 只有
  URL、没有 pending candidate handle；同一 NCBI bridge/prefix 可对应多个 supporting Skill/candidate，
  maximum matching 因而把该成功 dispatch 重新记到已完成的
  `gate-group-42e93...`，唯一待完成的 `gate-group-c1bafa...` 不前进。下一次模型又只返回 prose，bounded
  recovery 耗尽后 child/root 才失败。这个故障不是最大匹配算法本身，也不是 HTTP 200 不算证据，而是
  model-visible call 无法表达“本次调用要满足哪个 exact pending candidate”，handler 又从多个 grant 中
  按顺序选择 `matched_skill`。
- 同一 run 还观察到 FDA 的完全相同请求得到两次确定性 404；UniProt/OpenTargets 的 400 属于上游请求/
  查询形状错误。它们与 Target 的 200 错误归属分别处理：不能把动态 429/5xx/transport 当永久失败，也
  不应为完全相同的稳定 4xx 再消耗一次真实网络 quota。

### 跨领域不变量、成熟实现对照与通用修复

1. **shared bridge 必须 late-bind exact capability handle。** 当前 pending Knowledge Gate 的 HTTP schema
   现在从 immutable frontier 动态投影 `candidate_id` enum 并设为 required；pre-dispatch 重新证明 handle
   属于仍待完成组且 URL/method 命中该候选，再把 call-local `ToolContext` 缩窄为唯一已有 grant。handle
   只能缩权，不能凭模型参数创设网络 authority；无 gate 的普通 HTTP 调用兼容，旧调用仅在其坐标唯一
   命中一个 activated candidate 时自动绑定。receipt matching 也只接受该 bound candidate，完成组 handle、
   无效 handle、坐标不匹配和共享坐标无 handle 均在发网前 typed reject。
2. **artifact acceptance envelope 必须由 provider 与 downstream capacity 联合推导。** reducer wire
   generation reserve 继续独立且给 JSON/多语言字节留传输余量；accepted semantic token ceiling 取
   downstream consumer 与 provider accepted capacity 的较小值，byte ceiling再由 token capacity、最终/
   merge byte budget 和真实 metadata envelope 推导。未知 provider 保留原保守 fallback；token、byte、
   coverage、manifest 与 materialization 前后双校验均未删除。37,574-byte body 在本次真实容量中会通过，
   不是把所有大小上限放开。
3. **确定性 HTTP client error 对 exact invocation 单调终止。** delegated run 内第一次真实 dispatch 得到
   request-sent 4xx 后，仅保存 tool + normalized-argument SHA + safe grant/status receipt；同参第二次在发网
   前拦截，并要求换 exact args/candidate 或输出 degraded gap。408/409/425/429、5xx、transport/DNS 与不同
   candidate/args 仍可按各自策略尝试，不把临时错误过度 quarantine。

本轮继续采用已冻结官方源码/文档中的组件级模式：OpenClaw Tool Search 与 Codex tool-search/context
都把 compact selection late-bind 到 canonical tool/schema；Temporal Activity retry 将 non-retryable
failure 绑定当前 exact activity/arguments；Deep Agents/LangGraph 将大结果 materialize 后按下游 state/
consumer 合同传递。决策仍是 **adapt exact handle + typed receipt + downstream capacity contract**，拒绝整体
换栈、grant-order 猜测、按疾病/数据库/文件名特判或把 provider context length 当 artifact byte cap。
官方入口与冻结 revisions 见 Round 10--12 的成熟实现表，本轮没有引入二手来源。

### 确定性复现、回归、提交与生产切换

- 新增非医疗 `portable-adapter` 复现：两个独立 OR-group 共享同一 HTTPS coordinate。无 handle 调用在
  dispatch 前以 `ambiguous_candidate_binding` 拒绝；`candidate-alpha` 成功后重复 handle 以
  `candidate_not_pending` 拒绝；`candidate-beta` 独立 dispatch 后两个 group 各有唯一 receipt。fake handler
  同时断言 call-local context 只有一个 exact grant。另一个 vendor-records 复现证明 404 同参 replay 不
  再进入 handler，但 changed URL 正常 dispatch；classifier 明确保留 408/409/425/429/5xx 可重试性。非临床
  warehouse fan-in holdout 证明 37,574 bytes 在 10,330-token/676,388-byte consumer 下可接受。
- 受影响首轮为 `120 passed, 77 subtests`；Knowledge Gate compiler/runtime、delegation、HTTP、workflow、
  convergence 与 fan-in 联合为 `399 passed, 209 subtests`。隔离 tmpfs 完整 Harness 为
  `1939 passed, 1 failed, 807 subtests`；唯一 failure 是生产 Harness image 按设计不含 Node 的 CommonJS
  interpreter 环境项，同一 exact test 在宿主 Node v22.23.1 下 `1 passed`，组合覆盖全部 1,940 项。
  clean candidate 联合为 `398 passed, 1 skipped, 205 subtests`，skip 仅因 tracked archive 不包含未跟踪
  V2.3 reference ZIP。`py_compile`、diff、credential-like added-line、genericity 与 protected-deletion
  检查均通过。
- 通用代码提交为
  `98882f0b18abed5b207c520b3b63ab852a93bc6d fix: bind exact evidence calls and fan-in capacity`。
  clean archive `/tmp/chat_ds_deploy_98882f0b.cU1tKE` 与 tracked tree 都为 22,452 files；candidate/production
  image 为 `sha256:5536a15f50658dec43090db9c6a7e8ef419f29095709d90e28e2a26c74b8ec14`。
  部署前两次确认 nonterminal run、schedule、5173 established connection 均为 0；只 force-recreate
  Harness，旧 image 保留 `rollback-pre-98882f0b`，Backend/Frontend/四槽/Proxy/Browser/Search/数据卷
  未重建。部署后三入口、Harness 与 Backend→Harness health/models 均 200，storage identity 相同，
  SQLite quick/FK 正常，healthy/restart 0、严重日志 0；生产 Harness 直接请求默认 GLM-5.2 为 200，
  thinking reasoning 非空且 visible response 以 `stop` 完成。

### Round 13 第二组顺序验收：typed output transaction 与显式 multi-agent workflow

- V2.3 使用新的 conversation `2ca049506d0249418815b64bab500ead`、root
  `5e635b2d7e4b4486bdeb37d88690d34b` 和 `shaiengine_deepseek_v4_pro`。run 从
  2026-08-04 09:05:11 到 09:45:52 UTC 达到唯一 durable
  `run.failed/delegate_step_failed`；Intent、7 路 bootstrap、一次竞争情报 clean retry 和 Termination
  worker 均到达受约束终态。PICO `39b816d7...` 与 Safety `295bfcc9...` 已完成证据读取/检索并生成
  substantive body，却都在独立 `submit_result_fields` 页脚投影中把 schema 声明为 object 的
  `pico_metadata` / `extraction_metadata` 等字段编码成 string。调用结构本身完整，旧 Harness 只给一次
  submission、不给 validator error，因此两个 child 分别以
  `delegated_result_footer_structured_repair_failed` 终止。根级没有 artifact，失败没有重放任何已完成
  evidence side effect；它不是网络、沙箱、context、总 timeout 或 corrupt stream。
- 肺癌 MDT 随后使用新的 conversation `7143d3304a6643c6aa3ff888d63a56d6`、root
  `01236e10499d43898c0a1ab96cbe4598`、同一 DeepSeek provider 和历史精确 7,089 字符 prompt。
  root 在约 15 分钟后 `succeeded/stop`，输入/输出为 971,079 / 26,639 tokens，并生成
  `mdt_report_TEST-LC-MDT-001.md`：75,337 UTF-8 bytes、1,576 行、SHA-256
  `b3f69235af270ea5a6c85b3c3128518d0ac3179db67fe1f28a9587c135b88472`。报告覆盖 16 个章节、11 个
  Round-1 角色区块、Round-2 交叉评论、冲突代码和可追溯表，但整个 root 有 0 child AgentRun、0
  `delegate_task`。模型在单一 primary context 中模拟了所有角色，违反 exact Skill 声明的 11 个独立
  specialist、并行 Round 1、Round 2 cross-review 和 coordinator consensus；所以 durable success 不等于
  procedure success。
- exact Skill、对话和 debug 的交叉证据显示肺癌 Skill 已被正确选中，动态工具面也含
  `delegate_task`，但 `direct_required_unsatisfied` 始终为空。生产配置的 progressive 路径绕过了已有
  semantic Workflow IR；同时动态 boundary 安装只替换 `exposure.tools`，没有原子替换同一 exposure 的
  `required_groups`/`missing_requirements`。因此 stop gate 无法证明显式委派步骤尚未执行。这不是模型
  自由选择“直接 chat 也可以”，而是 Harness 暴露能力却漏装 mandatory receipt obligation。

### 本轮跨领域不变量、`claude-code/` 对照与通用实现

1. **structured output 校验是独立、无副作用、可反馈的有界 transaction。** registry dispatch、证据读取
   和 artifact 写入不得因 JSON type mismatch 重放；失败 submission 本身不进入 durable tool history。
   validator 返回精确 path/type error，最多重新投影 5 次，成功只提交一个 harness-canonical footer，
   五次仍失败则确定性终止。
2. **显式 fan-out/fan-in 是 Skill 的执行语义，不是可选提示。** 普通 instruction-only Skill 继续走
   progressive disclosure；package-owned declarative contract 继续走 deterministic DAG；任何语言、领域、
   名称的 portable Skill 若结构上明确声明多角色独立/并行执行与汇总/共识，则必须进入已有
   content-addressed semantic Workflow IR。模型只映射 frozen instruction units，runtime 校验完整覆盖、
   lowering、dependency、receipt 和 terminal barrier。
3. **动态 capability boundary 必须原子安装 surface 与 obligations。** `tools`、`required_groups` 和
   `missing_requirements` 来自同一 immutable exposure 并一次替换；不允许“工具可见但 required receipt
   不存在”的半安装状态。

本轮唯一成熟 Harness 对照固定为本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`：

| 问题 | 实际源码模式 | ChatDS 取舍 |
|---|---|---|
| schema-valid tool call但字段类型错误 | `src/tools/SyntheticOutputTool/SyntheticOutputTool.ts` 用 AJV 对 synthetic structured-output tool 做 exact schema validation；`src/QueryEngine.ts` 以 `MAX_STRUCTURED_OUTPUT_RETRIES` 默认 5 次形成独立有界重试 | **adapt** 为 delegated result footer 的无副作用 control-plane transaction；保留 ChatDS exact evidence/receipt 外审计，不把模型提交当普通可执行工具 |
| Skill 明确要求并行 Agent，却由 primary 模拟 | `src/tools/AgentTool/prompt.ts` 要求并行请求在同一 assistant batch 发出多个 Agent calls，并强调给 child 完整任务；`src/skills/bundled/skillify.ts` 将每步 execution 明确区分 Direct / Task agent / Teammate | **adapt** 执行类型进入 typed semantic Workflow IR 与 receipt barrier；不依赖提示词自觉，也不整体复制其 UI/runtime |
| 是否切换整套 Harness | 参考仓库没有 ChatDS 的 package digest、Knowledge Gate、session workspace、egress 和 artifact contract | **reject** whole-stack replacement；只吸收上述通用 control-plane pattern |

生产实现提交为
`d23c7e4387d43709086e07d7b3f52bc33bcaaf57 fix: validate structured results and explicit agent workflows`：

- `harness/agent_loop.py` 增加最多 5 次的 validator-feedback typed submission；错误 submission 原子丢弃，
  既不进入 registry，也不重放 evidence/tool side effect。object-as-string 的精确非医疗复现先失败后纠正；
  连续五次错误严格失败且 registry dispatch 为 0；wrong executable tool fragment 仍立即 fail closed。
- 同文件将已有 `_standard_skill_declares_delegated_workflow` 的通用结构分类真正接入 engine selection：
  显式 multi-agent portable Skill 在 progressive/legacy 配置下都走 semantic plan。动态 progressive boundary
  同时安装 `required_groups` 与 `missing_requirements`。非医疗 portable-note holdout 证明模型提前 stop 时
  required tool frontier 会阻止伪成功并要求实际写文件。
- 受影响组合为 `444 passed, 134 subtests passed`；NFS tombstone 相关的 12 项在隔离 root 全部通过。
  完整 bubblewrap/tmpfs 回归为 `1970 passed, 1 skipped, 810 subtests passed`，仅两项因 user namespace
  映射 root-owned `/usr/bin/prlimit` 而触发 trusted-launcher 环境校验；这两项在真实宿主 namespace 单独
  `2 passed`，即全部 1,972 项逻辑覆盖通过。`py_compile`、diff、secret、genericity 和 protected-deletion
  staging 检查均通过。
- clean archive `/tmp/chat_ds_deploy_d23c7e43.bCAsME` 与 Git tree 均为 22,456 files；生产 Harness image
  为 `sha256:c2713d3c08056d549e0d7b5080de561c4d431e12322269a34763a71c60e53ed6`，revision label 精确匹配
  `d23c7e43...`，旧镜像保留 `rollback-pre-d23c7e43`。部署前 nonterminal run/root/schedule 与 5173
  established connection 均为 0；只 force-recreate Harness。部署后三入口、Harness 内部与
  Backend→Harness health/models 均 200，storage identity 一致，SQLite quick/FK 正常，healthy/restart 0、
  严重日志 0。

Round 13 至此完成两个新 case 的 durable terminal、exact Skill/对话/debug/tool/result/artifact 三源诊断、
逐 attempt 归因、冻结 `claude-code/` 实现对照、通用复现与跨领域修复、完整回归、本地代码 commit、
clean-archive 部署和生产 smoke。Round 14 从生产 `d23c7e43` 开始，仍先 V2.3、后肺癌 MDT，必须使用
两个全新 conversation/root。

## Round 14：tools-closed provider 幻觉与 typed plan 纠错预算

### 两个顺序 E2E 与逐节点归因

- V2.3 使用全新 conversation `ad60a1cd11fc448e844c8198080d2ccc`、root
  `9f4747b4fbe348ef8d5b61d0a923e589`、`shaiengine_deepseek_v4_pro`，上传 ZIP SHA-256
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`。从
  2026-08-04 11:10:59 到 11:25:46 UTC 达到唯一 durable
  `failed/delegate_step_failed`，根级 0 业务 artifact。Intent `fd111030...`、ClinicalTrials
  `8bd891...`、PubMed `0e1a856...`、ICH `415817...`、FDA `5a2008...`、EMA `dce30f...`
  均 succeeded；唯一失败是 Target Biology `c420143...`，其 10 个 evidence attempt 中 6 个已有
  成功、实际派发并提交的 HTTP receipt。
- Target Biology 最后一个模型 turn 已进入 runtime-owned final synthesis：`tool_schema_count=0`、
  `workflow_forced_tools=[]`、`delegate_tools_closed_terminal_turn=true`，仍有 1 个现有 iteration。
  Provider 却在 318 字符正文后发送 44 个 tool fragment，拼出一个未暴露的 foreign tool name 与完整
  JSON 参数。accumulator 正确标记 `tool_name_conflict/tool_name_unrecognized`，该 turn 派发数为 0；
  但旧分支因 `no_closed_tool_schema` 直接 `provider_tool_stream_corrupt_after_content`，没有接上已有
  post-dispatch tools-closed synthesis。故障不是远端网站、沙箱、context 或总 timeout，也不存在本轮
  副作用不确定性；先前 receipt 可以安全综合，当前幻觉调用不可执行。
- 肺癌 MDT 随后使用全新 conversation `2ad4efc9047748558006dd1026832d28`、root
  `80ab4ffa71a34f008c9932c4bd0f319a`、同一 provider、历史精确 7,089 字符 prompt（SHA-256
  `eefb885294e6849d1e5ab5ce9f6799a30dfff1b9520761bd403138b7f4b135b7`）和 exact User Skill
  `SKILL.md` SHA-256 `2955c00a456f7ca4215e27091c55ceeca6c84d170e4af99560adb54e0d5b4d42`。
  11:28:33--11:32:03 UTC 达到 durable `failed/capability_plan_validation_exhausted`，输入/输出
  122,302 / 12,750 tokens，0 child、0 artifact；失败发生在执行 grant 之前，故没有 worker 被错误派发。
- semantic capability compiler 确实运行而非回退 direct chat。三次 submission 依次为：required/optional
  重复 candidate；`workflow_plan.nodes[0].round=0` 的 schema error；覆盖完整、含 11 个 Round-1 Agent、
  Round-2、coordinator 和 final artifact 的近完成计划，但仅余一个 required instruction unit
  `coverage.iu-6f0d78b0b2b9b0287328842e` 未映射。旧统一上限 3 把前两次机械/schema 纠错与语义 compiler
  纠错共同耗尽，导致第三次第一次接近合法的计划无法收到一次 exact feedback。校验器没有误判，缺陷在
  typed control-plane transaction 的反馈预算，而不是应当猜测或自动补写该 instruction。

### 通用不变量、成熟实现对照与取舍

1. **工具关闭是 authority 边界，不是 provider 行为假设。** 未暴露/非法调用必须整批丢弃且绝不执行；
   delegated run 若已有提交 receipt、无 pending mandatory frontier 且还有预算，应只允许一次无工具综合，
   不重开 schema、不重放请求、不保存当前坏批次的正文、reasoning 或参数 fragment。无历史 receipt 或无预算
   仍 fail closed。
2. **结构化计划的 schema 与语义纠错属于同一个有限 transaction。** 每次错误都返回 exact path/type/compiler
   feedback；上限 5 次，成功才原子安装 grant，连续五次失败仍确定性终止。Harness 不静默去重 required/
   optional、不替模型映射缺失 instruction，也不放松 coverage/DAG/capability validator。

唯一成熟参考仍冻结为本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`：

| 问题 | 实际源码路径与模式 | ChatDS 决策 |
|---|---|---|
| tools-closed turn 出现未知调用 | `src/services/tools/StreamingToolExecutor.ts` 对 streaming fallback 提供 `discard()`，unknown tool 形成 synthetic error 而不执行；`src/services/tools/toolExecution.ts` 将 unknown tool/input validation error 返回模型 | **adapt** discard-and-continue 原则，但 ChatDS 更窄：只从本 run 已提交 receipt 做一次 tools-closed synthesis，不把 foreign call 注册为普通工具结果 |
| typed plan 前两次机械错误耗尽三次预算 | `src/tools/SyntheticOutputTool/SyntheticOutputTool.ts` 用 AJV 做 exact structured validation；`src/QueryEngine.ts` 的 `MAX_STRUCTURED_OUTPUT_RETRIES` 默认 5 | **adapt** 为 capability-plan schema+semantic 的统一五次 transaction，保留 ChatDS content-addressed Workflow IR、coverage 与 grant 安装边界 |
| 自动补齐缺失 instruction 或放宽 validator | 参考实现没有 ChatDS 的 instruction-unit/package-digest authority | **reject** 推测性修补；继续把 exact compiler feedback 交给模型，五次后 fail closed |

### 确定性复现、回归、提交与生产切换

- 新增非医疗 delegated evidence 复现：第一次 `read_file` receipt 已提交；第二次进入 tools-closed final
  synthesis 时 provider 在正文后幻觉 `provider_foreign_tool`。Harness 证明 foreign call 派发 0，坏正文/
  reasoning/argument 不进入下一请求，第三 turn 无工具并从 `evidence.md` 完成。原有无预算、无 receipt 用例
  继续精确 terminal。
- portable Skill 计划测试证明连续三次 schema-invalid 后第四次合法计划可安装并只开放所选 capability；另两项
  分别证明 workflow-semantic 与 predispatch-schema 连续五次错误时在第 5 次精确
  `capability_plan_semantic_validation_exhausted`，registry side effect 为 0。
- 聚焦 5 项通过；stream/capability/Workflow IR/delegation/Knowledge Gate/side-effect retry 联合为
  `556 passed, 155 subtests passed`。宿主 full 为 `1963 passed, 1 skipped, 801 subtests passed`，19 个
  failure 全部是当前用户无法读取生产 tombstone；隔离复跑对应 `13 passed, 9 subtests passed`。完整
  bubblewrap/tmpfs 为 `1971 passed, 810 subtests passed`，仅两个 trusted launcher 和一个 user-namespace
  `setgroups` 环境项失败；真实宿主为 `2 passed, 1 skipped`。即本次新增后的 1,973 项可执行逻辑全部
  通过，skip 仍是环境条件。`py_compile`、diff、secret/genericity 和 protected-deletion staging 检查通过。
- 通用代码提交为
  `cfc0e09d62ff98c2d831dbf0895c9b358fd01a60 fix: recover typed workflows across provider faults`。
  clean archive `/tmp/chat_ds_deploy_cfc0e09d.1cXR1g` 与 Git tree 均为 22,456 files；candidate/deploy image
  `sha256:d05f6f92ae094e0a7f4fc43d2f09bd175316a7484a1b9d8846c8640462b2397d`，revision label 精确匹配
  完整 commit，旧镜像保留 `rollback-pre-cfc0e09d`。部署前连续两次确认 nonterminal run/root、schedule、
  `:5173` established connection 均为 0；仅 force-recreate Harness。部署后三入口、Harness 内部与
  Backend→Harness health/models 均 200，storage identity 一致，SQLite quick/FK 正常，healthy/restart 0、
  严重日志 0。

Round 14 至此闭环；代码没有 V2.3、疾病、Skill/session/worker、固定角色数、route 或报告文件名特判。
Round 15 从生产 `cfc0e09d` 开始，仍须先 V2.3、后肺癌 MDT，使用两个全新 conversation/root。

## Round 15：子任务终态语义事务与一源多角色预加载

### 两个顺序 E2E 与逐节点归因

- V2.3 使用全新 conversation `9f83f64f7f4f4b87b6e057f6891cd780`、root
  `159c979c17564922a0d735a02def3f74`、`shaiengine_deepseek_v4_pro`，上传 ZIP SHA-256
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`，安装 19 个 Skill 且
  attachment 已复制到 session workspace。2026-08-04 12:02:34--12:20:11 UTC 达到唯一 durable
  `failed/delegate_retry_exhausted`，0 业务 artifact。
- Intent `b7a078f1...`、PubMed `4ab1fc87...`、ClinicalTrials.gov `f3819052...`、ICH
  `f4995242...`、Target Biology `5f24be3d...`、FDA `91943f4f...`、EMA `fe18e08a...` 均
  succeeded；只有 Competitive Landscape 的首次 `50346e05...` 与精确父级 retry
  `e1c03a63...` failed。两个 attempt 都在 tools-closed 终态只返回“让我继续搜索/下一步比较/随后给出
  结果”的过程叙述。已有 typed footer 投影只能投影字段，不能把非结果正文变成结果；旧 Harness 要到
  child 返回外层后才做同一 semantic rejection，于是父级只能昂贵地重跑整个 child。不是网络、沙箱、
  timeout、context、tool-call corruption 或网站失败。
- 肺癌 MDT 随后使用全新 conversation `369e8a816594454598fd9c8c9a5c1f8a`、root
  `2fa0eb88bb454203877a424f6bafe9ce`、同一 provider、历史精确 7,089 字符 prompt（SHA-256
  `eefb885294e6849d1e5ab5ce9f6799a30dfff1b9520761bd403138b7f4b135b7`）和 exact User Skill
  `SKILL.md` SHA-256 `2955c00a456f7ca4215e27091c55ceeca6c84d170e4af99560adb54e0d5b4d42`。
  12:21:10--12:37:34 UTC 达到 durable `failed/delegate_step_failed`；Round 14 的五次计划 transaction
  生效，模型第三次 submission 已成功安装执行 grant，故障已前移到首个真实 worker，而非再次卡在计划。
- Coordinator Round 0 `c4042d82...` 已成功调用 `execute_code` 并生成实质结果，随后却被外层以
  `Delegated worker did not inspect its exact Skill contract with skill_view: SKILL.md` 拒绝。该 worker 的
  `worker_file` 与 `required_capability_skills` main 恰为同一个 `(skill_view, lung-cancer-mdt, SKILL.md)`。
  deterministic preload 实际已在首个模型 turn 前成功读取一次，但旧代码用互斥 `elif` 只把 receipt 计入
  capability ledger，未同时计入 worker-contract ledger，并且 preload list 还为同一 immutable coordinate
  构造了重复请求。故障不是模型漏调工具，也不是 Skill 要求 Playwright/Selenium、网络或执行器缺失。

### `claude-code/` 源码与真实 CLI 对照

唯一成熟参考仍冻结为本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`。除静态源码外，本轮在隔离目录用已安装
Claude Code 2.1.152 做了两个黑盒实验：

| 问题 | 源码/运行证据 | ChatDS 决策 |
|---|---|---|
| agent 的 Skill/定义是否依赖模型首轮自行读工具 | `src/tools/AgentTool/loadAgentsDir.ts`、`AgentTool.tsx`、`runAgent.ts` 在 query 前构造 agent system prompt，并将 declared Skills 并发预载为 initial messages；工具池另行解析。自定义 agent 的 tool allowlist 为空仍在首轮准确返回只存在于 agent definition 的随机 nonce | **adapt**：compiled authority 在模型首轮前确定性加载；是否把 `skill_view` 暴露给模型是另一条 capability 边界 |
| 同一 authority resource 承担多个角色 | `runAgent.ts` 按 source identity 装配初始上下文，而不是要求模型为每个消费角色重复读取 | **adapt**：按 exact `(tool, skill, path)` 去重 preload；一次成功 receipt 独立满足 compiler 赋予该 source 的 worker/capability 多个 ledger role |
| schema-valid 但语义无效的终态是否应退出子查询再重跑 | `SyntheticOutputTool.ts` 用 AJV 返回 exact errors；`QueryEngine.ts` 在同一 query transaction 内保留 structured-output retry state，默认 `MAX_STRUCTURED_OUTPUT_RETRIES || 5`。黑盒中首次 StructuredOutput 缺字段/长度错误后，validator error 作为 tool result 回到同一 query，第二次提交成功 | **adapt**：把现有 process-narration semantic validator 前移到 child 内的 bounded tools-closed correction；丢弃 rejected prose，保留任务、预加载 authority 和已提交 receipts，不重开工具或重放副作用 |
| 由 Harness 自动替模型搜索或补结果 | 参考实现纠正结构化提交但不伪造业务结果 | **reject**：连续无效仍 fail closed；不放松 outer contract，不为特定 Skill 生成内容 |

### 通用修复、不变量与回归

- `delegated_result_contract.py` 现在拥有内外层共享的 process-narration predicate 和“剥离
  RESULT_FIELDS/quality/gap/receipt 机器账本后的实质正文”投影。合法 `degraded` 审计词不能掩盖正文仍只是
  future-action narration；inner transaction 与 outer commit boundary 不再有两个语义版本。
- delegated child 在 terminal audit 命中该语义错误时，使用已有唯一 bounded output-contract correction：
  工具关闭、拒绝正文/推理/参数不进入下一请求、保留 original task/preloaded resources/structured tool receipts，
  给出 exact validator feedback 后完整重生成。typed footer projector 只用于实质正文已成立、仅 footer 非法的
  情况；第二次仍非法则按既有边界 fail closed。
- deterministic preload 按 exact resource coordinate 去重。成功 `skill_view` receipt 分别检查每个已编译角色，
  因此同一 `SKILL.md` 可同时满足 worker inspection 与 required capability main；读取次数仍为一，模型有效工具
  列表保持为空，authority 没有扩大。
- 两项新确定性测试分别覆盖“process narration + 合法 degraded/typed machine ledgers 在同一 child transaction
  内纠正，零工具派发”和“一个 immutable Skill main 一次 preload 同时满足两个角色”。高风险联合为
  `454 passed`。宿主 full 为 `1913 passed, 2 failed, 18 errors, 1 skipped`，20 个红灯均在被测逻辑前
  命中不可读生产 NFS tombstone；bubblewrap clean tmpfs 为 `1973 passed, 2 errors, 1 skipped`，两项只因
  namespace 改变 trusted executor identity，回到真实宿主为 `2 passed`。即新增后的 1,975 项可执行逻辑
  全部通过，唯一 skip 仍为权限转换环境条件。`py_compile`、diff、secret/genericity 和受保护删除 staging
  检查均通过。
- 通用代码提交为
  `86609068727337b88b0af564b935c85daba6a88e fix: make delegated contracts transactional`。
  clean archive `/tmp/chat_ds_deploy_86609068.pOBTG0` 与 Git tree 均为 22,456 files；candidate/deploy image
  `sha256:7af086170febee367a1c8ca42b6e0f0e763b699f53e15ff934777fff2e19130d`，revision label 精确匹配
  完整 commit，旧镜像保留 `rollback-pre-86609068`。部署前两次 nonterminal run/root/schedule 和
  `:5173` established connection 均为 0；仅 force-recreate Harness。部署后 healthy/restart 0，三入口、
  Harness 内部、Backend→Harness health/models、storage identity、SQLite quick/FK、idle run 与严重日志
  smoke 全通过。

Round 15 至此闭环；代码没有 V2.3、疾病、Skill/session/worker、固定角色数、route 或报告名特判。
Round 16 从生产 `86609068` 开始，仍须先 V2.3、后肺癌 MDT，使用两个全新 conversation/root。

## Round 16：证据型终态事务与可操作的编译反馈

### 两个顺序 E2E 与逐节点归因

- V2.3 使用全新 conversation `8bdd202c6b854c07b21e61100723a977`、root
  `3fef4aeefbd74600866712c02ecb3853`、`shaiengine_deepseek_v4_pro`，达到唯一 durable
  `failed/delegate_retry_exhausted`。Intent、ClinicalTrials.gov、PubMed、ICH、FDA、EMA 和 Target
  Biology child 均 succeeded；只有 Competitive Landscape 首次 attempt `1a185b3...` 与精确 retry
  `290af569...` failed。
- 两个 Competitive attempt 都不是网络、沙箱、provider stream 或工具调用损坏。它们在 tools-closed
  terminal submission 中返回了 schema-valid、非空的 DrugBank 字段，却没有任何 runtime-owned 成功
  evidence receipt。外层 delegated-result contract 正确拒绝“无 receipt 的填充值”，但这个 semantic
  validator 只存在于 child commit boundary 之外；因此模型在同一 structured-output transaction 中收不到
  精确错误，父级只能重跑整个 child。该 supporting Skill 的 DrugBank 路径又需要 credential/cache，当前
  frozen authority 没有可执行 candidate，所以正确收敛应是同一 child 把字段改为 `null` 并明确 degraded，
  而不是伪造事实，也不是无意义地重跑全部推理。
- 肺癌 MDT 使用全新 conversation `7f8382b53003479b9c38d5f7d43d1c15`、root
  `129194592ba943b4842d7cc610902fe5`、同一 provider 和 exact User Skill，达到 durable
  `failed/capability_plan_validation_exhausted`，0 child、0 artifact。五次 plan submission 依次收到：
  duplicate selection、`workflow_plan.nodes[0].round=0` schema error、unselected capability、Workflow IR
  invalid，以及 Workflow IR 缺少 `coverage.iu-6f0d...`。五次 bounded transaction 和 fail-closed 均按
  Round 14 设计工作，没有错误派发 worker。
- 最后一个 internal instruction-unit 实际可从 frozen source 唯一反查到 `SKILL.md` ordinal 3、paragraph
  line 10：`本 Skill 提供肺癌多学科诊疗（MDT）的完整决策支持框架，通过多轮讨论机制实现高质量的协作决策`。
  但 provider-facing schema 只允许模型提交 `document_id + ordinal`，旧 compiler feedback 却只返回内部
  `coverage.iu-*` hash；这是不可写、不可行动的错误坐标。validator 的 complete-coverage 要求本身正确，
  缺陷是内部身份没有安全投影回模型已知的 source coordinate，而不是应放松 coverage 或由 Harness 猜补节点。

### 通用不变量、`claude-code/` 对照与取舍

1. **证据型 structured output 的语义校验必须留在原 transaction。** parent compiler 已声明某 child 的
   non-null 字段需要 evidence acquisition 时，child 终态只有真实 handler/Knowledge Gate/standard-candidate
   成功 receipt 才能支撑填充值。零 receipt 加填充值必须把 exact validator error 返回同一 child；模型可在
   既有最多五次 transaction 内改为 `null/degraded`。不得重开工具、重放副作用、重跑整个 child，也不得把
   preloaded Skill、模型自述或失败调用算作 evidence receipt。
2. **内部 content-addressed identity 与模型可写坐标必须双轨保存。** validator/debug 保留稳定 `iu-*` hash；
   model-facing feedback 必须投影成 frozen input schema 允许提交的 exact `document_id + ordinal`，并可附
   bounded source line/已披露 preview 帮助定位。projection 不修改 coverage、selection、authority 或 DAG，
   只让反馈可操作。

唯一成熟参考仍冻结为本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`：

| 问题 | 实际源码路径与模式 | ChatDS 决策 |
|---|---|---|
| schema-valid 但语义上无证据的 typed terminal | `src/tools/SyntheticOutputTool/SyntheticOutputTool.ts` 把 StructuredOutput 实现为 read-only、non-open-world synthetic tool，并用 AJV 返回 exact error；`src/QueryEngine.ts` 在同一 query 中保留最多 5 次 structured-output retry | **adapt** 为 child 内同一无副作用 terminal transaction，并叠加 ChatDS handler-owned evidence ledger；不把模型自述当 receipt |
| compiler 返回模型无法提交的内部 hash | StructuredOutput 的错误必须围绕输入 schema 的 exact path/value 返回，才能由同一 query 修正 | **adapt** 为 internal hash + actionable frozen coordinate 双轨；保留 ChatDS content-addressed identity 和 strict complete coverage |
| 自动补值或放松 evidence/coverage | 参考路径提供反馈重试，不替模型伪造业务字段 | **reject**；连续错误仍在既有上限确定性 fail closed |

本轮还用已安装 Claude Code CLI 做过同类黑盒校验：首次 StructuredOutput 不合法时，exact validator error
回到同一 query，随后可在同一 query 成功提交；这与上述源码路径一致。ChatDS 只独立实现该事务不变量，
不依赖或复制参考仓库 runtime。

### 通用实现、确定性复现与回归

- `delegated_result_contract.py` 的字段审计现在同时返回 `present/degraded/missing/null_fields`；schema-valid
  `null` 在结构上存在，但不会被误计为 evidence claim。inner 与 outer audit 共用同一投影。
- `tools/delegation.py` 把 parent-compiled `evidence_acquisition_contract` 明确传入 delegated child。
  `agent_loop.py` 在 child terminal structured-output transaction 中只统计真实成功的 handler-boundary、
  Knowledge Gate 或 exact standard-candidate receipt。若 contract 要求 evidence、receipt 为 0 且字段非空，
  exact error 进入已有最多五次的 `submit_result_fields` transaction；rejected submission 不进入 registry，
  tools remain closed，重复无证据 submission 最终仍 fail closed。
- `skill_capability_plan.py` 将 `coverage.iu-*` 转为 exact `document_id`、ordinal、source lines 和 bounded
  disclosed preview；debug 同时保留 internal path，并只记录 preview hash。selection、coverage、capability
  authority 和 atomic install 规则未改变。
- 新增/调整的跨领域复现使用任意非医疗 `record_id`：第一次无 receipt 提交填充值，收到 exact error 后再次
  错误提交，第三次改为 null/degraded；全部发生在同一 child、3 次 provider request、0 registry dispatch。
  portable workflow omission 测试证明反馈包含 writable document/ordinal coordinate，同时 internal hash
  仍稳定保留。无 V2.3、疾病、数据库、固定 worker、route 或报告文件名参与判断。
- 聚焦与直接受影响 4 模块为 `268 passed`；扩展 stream/delegation/compiler/Knowledge Gate 高风险组合为
  `543 passed`。生产 Harness image 内 `unittest discover` 为
  `Ran 1937 tests; errors=2, skipped=5`，两项 error 是测试环境只挂载 Harness 导致缺 `/executor` 和缺
  Backend workspace-lock parity 文件，而非断言失败。按真实服务布局挂载 whole repo 后 workspace-lock
  项通过，isolated executor 44 项中 43 项通过；唯一 CommonJS 项只因 Harness image 不预装 Node，精确
  挂载生产宿主 `/usr/bin/node` 后通过。`py_compile`、`git diff --check`、secret/genericity 与 protected
  deletion staging 检查均通过。

### 提交与生产切换

- 通用代码提交为
  `8097db3ca14d9341cffcf5d4253c5c8c51133728 fix: keep skill validation corrections transactional`。
  clean archive `/tmp/chat_ds_deploy_8097db3c.ueKBN9` 与 Git tree 均为 22,456 files；candidate/deploy
  image 为 `sha256:75aa609858a9c8d24dd447b1d8565dbdccaf05378cb3123c8c377aa3ba655b9b`，revision
  label 精确匹配完整提交，旧镜像保留 `rollback-pre-8097db3c`。
- 部署前两次确认 active/nonterminal AgentRun 与 `:5173` established connection 均为 0；只
  force-recreate Harness。Backend、Frontend、四个 Executor、Browser、skill-egress proxy、SearXNG/Valkey
  和数据卷均未重建。部署后 Harness healthy/restart 0；`127.0.0.1`、`10.10.132.126`、
  `172.30.100.126` 三入口 root 与 `/api/health` 均 200；Harness 内部和 Backend→Harness health/models
  均 200，四模型目录正常；host/container storage identity 一致，SQLite quick/FK 正常，active run 和
  严重日志均为 0。

Round 16 至此闭环。Round 17 从生产 `8097db3c` 开始，仍须先 V2.3、后肺癌 MDT，使用两个全新
conversation/root；当前用户授权硬上限为 Round 18。

## Round 17：原生双引擎迁移后的 V2.3 新鲜生产验收（通过）

### 冻结输入、三源证据与完整时间线

- 本轮不是重放或解释旧 run，而是独立新建生产 conversation
  `00e4881af558441595ab4e0bdba05992` 和 root `2d0a6b1cef46411f87b1c60bed8053b7`。
  Engine 为未修改 Claude Code 2.1.152，模型从 `deepseek_v4_pro` 精确绑定到 API `AgentModel`，
  permission preset 为 `session_full`。持久 user message 为 64 字符，SHA-256
  `2f042f8dec9eaf2f79994e634a81da9ab11408e53dbd42952a41be049f43787c`；assistant message 为
  2,839 字符并精确关联 root run。
- 输入包为 `skills_and_refs/xClinicalTrial-Design-V2.3.zip`，SHA-256
  `78b890eab57ff516c20a39a565631caa5d784f839b42f6ad9efbdbdd951eb0a0`。transactional install
  得到 19 个 Skill，primary 为 `healthsim-trialsim`。不可变 Skill view digest 为
  `9aee2a0596eff2e78c48c615332e70ed3d82abc9539b701663ea7076af96d7b8`，manifest 编译出 9 个
  worker、17 条 route、1 个 artifact contract，compiler diagnostic 为 0。对应设计参考
  `xClinicalTrial_Design_V2.3.html` 的 SHA-256 为 `2d4c772b...`。
- compiler 从声明数据选择 `composite_full_protocol_design`，route SHA-256
  `b7ff3beb155882eebe6b34381c3af540e515d35867f9458f9a3682132e2fd921`。phase 0 启动 7 个并行
  mandatory worker：PICO、safety extraction、termination、AE adjudication、target biology、
  competitive landscape、literature synthesis；phase 1 在前序 fan-in 后启动 I/E criteria worker。
  8 个 worker 都收到原生 task notification 与 `native_subagent_end_turn` checkpoint，并在数据库形成
  8 个 depth=1、`succeeded/stop` AgentRun；root 为 `succeeded/end_turn`。
- root 从 `2026-08-20 19:32:35.708210` 运行至 `2026-08-20 21:19:51.430514`，耗时约 1 小时
  47 分。lossless native ledger 是 31,685 条合法 JSON：31,087 `stream_event`、253 `system`、
  160 `assistant`、154 `user`、8 `chatds.workflow.worker-settled`、17 artifact、各 1 条 workflow contract、
  artifact contract、native result 和 supervisor terminal。后台 debug stream 另有精确的 terminated 与
  downstream-final 两条投影证据。
- 原生工具层共有 145 次 started、133 次 completed、12 次 failed。12 次 failed 均为 Bash 的可选检索/
  分析尝试：7 timeout、3 command nonzero、1 missing file、1 script runtime error；没有
  `content_omitted/code_omitted` placeholder、malformed JSON、write retry storm、重复审批或 controller
  synthesized result。它们没有满足也没有替代任何 mandatory receipt；8 个声明 worker 仍全部完成，
  mandatory frontier 精确推进到 2。

### Artifact fan-in、终态与 ground truth 对照

- durable workspace 位于
  `/nfs/temp/chat_ds/7f44dcdcc18445779ec23dd6d8302c01/00e4881af558441595ab4e0bdba05992/workspace/gal3_ad_cdp`。
  交付合同的 14 个 Markdown 文件全部存在：11 个顺序模块、README、`_checklist.md` 和
  `GAL3_AD_FULL_REPORT.md`；另有 3 个 simulation source artifact。canonical full report 是 11 个模块
  的 byte-for-byte 顺序 concat，168,549 bytes、2,735 lines，SHA-256
  `7f305bd828e47a7ce1cf4ed4569f6d05acf0d389ad813b9f15c4590947557507`。
- workflow receipt 为 `passed`、frontier 2、finding 0；artifact receipt 为 `passed`、activated contract 1、
  finding 0。Supervisor seq 31,685 记录唯一 authoritative terminal：`succeeded`、exit 0、result count 1、
  pending plan/native task 都为 0、`error/error_code/error_stage` 均为空，termination source 为
  `upstream_claude_code_completed`。因此本轮完整 durable result，而不是仅凭模型结束语判定成功。
- 与顶层 GLM-5.2 ground truth（200,094 bytes/3,383 lines，SHA-256 `0b7a30eb...`）相比，新鲜报告
  byte ratio 0.842349、line ratio 0.808454、normalized char-trigram cosine 0.819621、token cosine
  0.907600。与 modular Claude Code ground truth（193,110 bytes/2,360 lines，SHA-256
  `7cfed42c...`）相比，byte ratio 0.872813、line ratio 1.158898、char-trigram cosine 0.761083、
  token cosine 0.888361。三者均有相同 11 个核心 H1 业务模块与 14 文件合同，但措辞、标题细化和试验参数
  并非逐字相同；两份 ground truth 相互之间同样不是同一文本。
- 接受标准据此保持为 Skill-declared workflow、mandatory evidence、结构、文件、顺序 merge、规模和
  artifact validation 的业务等价；不把随机模型输出的 byte equality 嵌入 Harness。需要逐字复刻时应另建
  确定性 template/reference-copy 产品合同，否则会违反通用 Skill 执行和禁止 fixture overfit 的约束。

### 成熟实现对照、修复问题链与本轮决策

- 成熟源码参考精确冻结为 `claude-code/` commit
  `6f6f12b37f529488b10e53928dd5508bb93535c7` 和 `deepseek-harness-clean/` commit
  `47f943859bef60e4160492346772ded9b24f765a`，均为本地独立、未修改仓库，无需 Web 搜索。
- Claude `src/cli/print.ts` 在原生 query 内流式发出 task progress、在 background local agent/workflow 未结算
  时 hold back result，并在最终 result 前 drain pending SDK events；`structuredIO.ts` 与
  `controlSchemas.ts` 维护 typed request/response 和 pending identity。ChatDS **adopt/adapt** 这些原生
  event/permission/result 语义到 authenticated Web receipt、workspace artifact audit 和 exactly-one outer
  terminal 后，不复制 query loop。
- DSH `packages/jobs/jobs/src/types.ts` 声明 session owner fencing、`running -> stopping -> exactly one
  terminal`、idempotent cancel、settled resource cleanup 和 read-only snapshot；Session event log 与 sandbox
  package提供原生 session/workspace 边界。ChatDS **adapt** 为经 peer-credential 校验的 event socket、exact
  tool identity、controller receipt 和 terminal projection；**reject** 粗粒度 plugin group 扩权、第二套
  agent loop、从 prose 推断 terminal，以及修改任一上游内核。
- 本轮终态为通过，故自动 repair question chain 的结论是：没有新的 mandatory workflow、artifact、authority、
  recovery 或 lifecycle defect需要生产修复。12 个 optional tool failure 已被正确隔离且未污染 mandatory
  frontier；不得为了让 acceptance round“看起来有修复”而制造 fixture-specific change。Round 17 记录为
  passing acceptance round，不计作新的 repair iteration。

### 部署与最终回归

- 本轮前已从当前源码重建并切换 Backend `sha256:862dd156...`、Frontend `sha256:5f1dd9bd...`、
  Claude Supervisor `sha256:e70054b3...`、DeepSeek Supervisor `sha256:f24a9ab...`、egress proxy
  `sha256:b434765...`；生产没有 Legacy 容器。SearXNG/Valkey healthy，limiter 恢复精确规则；Claude 三档
  permission E2E、DSH real AgentModel turn 和 exact-one-call Web search E2E 都通过。
- 最终回归为 Backend `371 passed, 116 warnings, 2 subtests`；Claude/DeepSeek Runner/Supervisor 与
  browser topology `131 passed, 6 skipped, 19 subtests`；DeepSeek Node `15/15`；Frontend `49/49`；
  Vite production build、Python `compileall`、`git diff --check` 均通过。warnings 是既有 passlib/
  `datetime.utcnow()` deprecation，不是断言失败。

Round 17 至此闭环。下一轮不应重复消费 V2.3 来证明同一事实；只有新的用户驱动独立 E2E 或新的通用不变量
失败，才进入 Round 18 repair loop。

## 2026-08-24 既有原生双引擎 Session 缺陷闭环（不计 Round 18）

本节是对既有 `a36e8cfb770143fea0c726abf9ccac1b`（DeepSeek Harness）与
`3b92e02e644c41e3a705940c199a7b0f`（Claude Code）的三源诊断和通用修复，不是一次新的独立模型
E2E。没有新 conversation/root、没有重放同一 run、也没有把派生数据修复解释成新 round；Round 18 仍保留
给用户驱动的全新验收。

### 冻结证据、准确分类与终态

- `a36...` root `73cbb06140824813948d14bf7a1a6b68` 的持久对话、冻结 V2.3 Skill view 与
  243,577 行 native/debug ledger 已逐项相关。模型调用了一次原生 `execute_skill_workflow({})`；参数为空是
  工具 schema 的正确形态。工具返回 `SCRIPT_PARSE`，因为 ChatDS workflow program 的单引号 JavaScript
  字符串包含未转义真实换行。Supervisor seq 243577 已唯一 sealed
  `failed/workflow_contract_failed`；旧 `error_stage=egress_bridge_seal` 是 adapter 误归因。117 MiB ledger 的
  whole-file read 导致 Supervisor OOM，Backend 又缺 DSH terminal recovery，使数据库/前端长期失真。
- `3b92...` root `a4cb1266b41142bf9c117d99383a3537` 的持久对话、同一 Skill snapshot 与 Claude
  native/debug receipt 显示首个 403 来自 ChatDS egress proxy 的 64 MiB cumulative budget，而 Claude bridge
  自身统计尚略低于阈值。分类为 policy/adapter，不是 Claude Code/模型/Skill 内容。两条旧 root 最终都保留
 真实 failed；没有补写模型正文、重试外部副作用或制造 success。

### 先定义的不变量、成熟实现对照与决策

1. 任意 Skill 编译出的原生 workflow program 必须预先可解析；数据与通用 topology code 分离，修复不得引用
   Skill/session/route/worker/file fixture。
2. native terminal 与大型 ledger 必须有界、可恢复且 exactly identified；Backend 重启后的派生 AgentRun、child、
   TurnActivity 必须最终与 Supervisor receipt 一致。
3. 一个原生 tool call identity 只能对应一张 Web card；不可见聚合事件不能切断 streamed reasoning/content；
   root terminal 后不能留下视觉上的 running child/tool。
4. 长 provider exchange 的默认预算应容纳声明的长上下文，但 exact egress authority、method/path/origin、次数、
   单响应与 deadline 仍必须 fail closed。

唯一成熟参考冻结为本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`。原生 query 在 result 前 drain pending events、stdio
structured I/O 保持 request identity、Session/terminal 留在原生 loop；本轮 **adapt** 其 durable pending/
identity/terminal pattern 到 ChatDS 现有 receipt 和 UI projection，**reject** 第二套 agent loop、模型 prose
terminal、业务 fixture workaround。DeepSeek 上游仓库仅作为不可修改边界固定在
`47f943859bef60e4160492346772ded9b24f765a`；没有以推测补全其私有行为，也没有修改 native harness。

### 通用修复与可重复证明

- workflow program 换行转义修复配有 museum/warehouse rename 测试：程序必须通过 `AsyncFunction` parse，
  sequential/parallel barrier 正确，恢复只重试失败 member。错误阶段现在由失败 frontier 归因，不再总落到
  egress seal。
- DSH Supervisor 改为有界 JSONL terminal scan/replay，并提供 exact identity terminal recovery；Backend 启动
  先恢复 Supervisor terminal，随后终态化 descendants，child engine identity 继承 root。安全派生 migration
  从 immutable raw event 恢复嵌套 tool call ID，并从 durable AgentRunEvent 校正 stale/missing workflow
  terminal；两者幂等且不改 raw ledger。
- live projector 支持 DSH 顶层、`message.source`、`message.content[]` 三种 result identity；TurnActivity 在
  payload/top-level 安全提升 ID/name。Frontend 合并相邻历史 text stream、同 ID 生命周期原位替换，并在 root
  terminal 时收口无 result 的 open card。
- Claude/DeepSeek/egress proxy 默认 cumulative outbound budget 调到 1 GiB 绝对上限，其他签名策略不变。
- 确定性证明为 Backend `135 passed, 52 warnings, 2 subtests`、Claude config `6 passed, 3 subtests`、
  DSH workflow Node `4/4`、Frontend `52/52`、生产 build/syntax/diff 全通过；runner candidate image 在
  `network=none/read-only/tmpfs` 的 warehouse workflow integration 通过。未启动模型重型 V2.3。

### 生产验收

- 当前生产核心镜像为 Backend `sha256:0054251e...`、Frontend `sha256:498c5326...`、Claude Supervisor
  `sha256:11952220...`、DeepSeek Supervisor `sha256:e69a056b...`、proxy `sha256:8bfa4a42...`；restart 均为
  0，health 正常，Frontend entry `/assets/index-BoKpCOIW.js`。
- SearXNG 已恢复项目 limiter/config mount；从 egress proxy 实际查询返回 24 条结果。部署时必须用宿主同一
  绝对路径运行 nested Compose，不能把 relative bind 解析成 daemon `/repo/*`。
- `a36...` 的生产 durable activity 用实际 frontend reducer 回放为 12 content、41 reasoning、201 tool card，
  其中 191 succeeded、10 failed、0 running；workflow root failed、9 children cancelled。tool repair 首次修复
  519 条，terminal repair 首次修复 70 条，独立进程复查剩余 0。`3b92...` 保持 failed。该闭环计作通用
  compiler/lifecycle/evidence/UI repair iteration，但不消费 Round 18 的独立 E2E 名额。

## 2026-08-24 四会话原生适配压力闭环（修复前冻结）

本节以 `ce2348...`、`05bd42...` 两个已终止会话为主要失败样本，并以 `54bd48...`、`b58f7f...`
两个仍在运行的会话作为传输与投影压力参照；不重放模型任务，不计作新的 V2.3 E2E round。四者绑定同一个
不可变 Skill view `9aee2a0596eff2e78c48c615332e70ed3d82abc9539b701663ea7076af96d7b8`，因此归因同时冻结了
持久对话、exact Skill/route、AgentRun/debug 与原生 runner ledger，而不是从网页错误字符串反推。

### 通用不变量与确定性复现目标

1. exactly-one selected primary Skill 必须在新鲜原生 Session 的第一条用户输入中通过上游公开的原生
   invocation gesture 加载；resume 只能发送本轮原文，不能重放 gesture 或注入第二套控制 prompt。
2. token delta 是高频瞬时传输，不是持久业务节点。ChatDS 保留 lossless native ledger，但安全网页投影只在
   完整 semantic block、完整 child assistant step、稳定 tool call identity 和 authoritative terminal 处落盘；
   中途断流不得把未闭合 block 伪装成 durable content。
3. 模型结束语、成功 Write 或“自检通过”均不能结算整个任务；workflow/artifact machine receipt 与 supervisor
   terminal 才是控制面事实。工具 started/completed 必须以同一 call identity 原位更新。
4. 大型 Session 的首次网页恢复必须是一个有界的最新窗口，并明确标注更早记录被折叠；增量恢复继续使用
   root-scoped high-water。不能从 offset 0 反复读取数万行而阻塞 terminal 投影。

唯一成熟实现参考冻结为本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`。`src/services/api/claude.ts` 在
`content_block_stop` 形成完整 AssistantMessage；`src/utils/sessionStorage.ts` 将 per-chunk Bash/MCP progress
明确视为 ephemeral、排除出 durable parent chain；tool result 使用原 `tool_use_id`，terminal 前保留原生 drain。
本轮 **adapt** 这些 semantic checkpoint、stable identity 和 bounded replay 模式到 ChatDS 既有 raw ledger、
TurnActivity 与 receipt authority；**reject** 修改 Claude Code/DeepSeek Harness 原生 loop、复制重试/compaction、
从模型 prose 推断 terminal 或加入任何 Skill/疾病/报告文件特判。

### 四会话三源归因

- `ce2348...` 的 Claude root `34b75d42792f44e9b14b7672f0e0a23d` 已唯一终止为
  `failed/artifact_contract_failed`。最后一次 `Write(08_safety_risk_management.md)` 有原生成功 result；其后
  SHAiEngine provider 请求连续 10 次 Claude-owned retry，最终 `ECONNRESET/Unable to connect to API`。
  Workspace 只有 01–08 等草稿，缺 09–11、checklist 和 full report。首因是外部 provider transport reset，
  artifact failure 是正确的后置 machine gate；没有 Write/Bash 沙箱拒绝、Skill 解析或 Claude core 缺陷。
- `05bd42...` 的 DSH root `2c6a98c1aa204cfeac7701334bdde65e` 原生账本第 413,936 条已唯一 sealed
  `failed/artifact_contract_failed`，原生容器已退出且 `container_id=null`。8 个声明 worker 均成功；但 fresh
  root 输入只有 64 字符用户原文，没有选中 primary Skill 的原生 invocation，于是 root 先合成一个 59,582-byte
  单文件并假称完成。artifact gate 拒绝后它才调用 `skill` 修复为 11 模块、README、checklist 和
  `GAL3_AD_FULL_REPORT.md`；最终 131,589 bytes/2,006 lines，仍低于编译合同 153,600 bytes，receipt 精确报
  `artifact_min_bytes_not_met`。模型自述“>100 KB PASS”不能覆盖 machine contract。
- `54bd48...` 的运行中快照 root `fcc892d2ff814f7095b0273a2c0b9e45` 共有 12 个 child attempt：2 completed、
  5 次 local AgentModel SSE transport error、2 次模型生成 malformed tool JSON，另 3 个 attempt 尚未结算；
  child provider retry 49 次。`10.10.132.2:1025` 当时 3 个 engine running、0 waiting、0 preemption，说明 endpoint
  可达但长流不稳定；不存在 ChatDS sandbox 或 native workflow loop 被拒绝的证据。
- `b58f7f...` 的运行中快照 root `216cac379072486db9987f4fe9461987` 已启动 14 个 child attempt，13 个均因
  `10.10.132.126:1025` stream transport error 结束，1 个仍在运行；child retry 97 次、root retry 1 次。
  同期 vLLM 有 3 running、1 capacity waiting，累计 error 47，分类为 provider capacity/stream 边界而非 Skill、
  Workspace 或 DSH core。
- 压力缺陷是 ChatDS 派生面：`05bd42...` 的 lossless native ledger 413,936 条，而 DB index 在原生终止后仍仅约
  7.8 万条并错误显示 running；`54bd48...`、`b58f7f...` 也分别存在约 1.2 万与 2.2 万条投影差距。旧 projector
  把每个 child token delta 变成 `run.progress`，activity path 又 `force=True` 每条 commit，Frontend 首次打开再从
  offset 0 分页最多 50,000 条；这三处共同造成网页碎片化、终态迟到和“像提前中断”。

### 仅 ChatDS-owned 的通用修复

- DeepSeek Supervisor adapter 对 exactly-one selected primary Skill 只在 `initial_prompt` 前 lowering 原生
  `/{skill-name}` gesture；native Session driver 只在 fresh state 选择该字段，resume 的 `turn_prompt` 保持用户原文。
  没有第二套 agent prompt/loop，也未修改 nested DSH。
- DSH projector 忽略 presentation 层 token delta，在 native `block-end` 投影 root 完整 reasoning/text block，在
  child `assistant/message` 投影一次 bounded step preview；raw envelope 仍全部进入 lossless ledger/index。raw index
  改为 500-event/4-MiB 有界 batch，并以 monotonic native seq 去重；terminal 强制 barrier。activity 不再对每个
  progress 强制 transaction，只对 authoritative terminal 等边界立即 flush。
- Session-wide activity 初始恢复改为一次最多 5,000 条的 newest tail window，按原时间序恢复并返回
  `has_earlier`；active root 的后续增量仍使用 exact root high-water。Frontend 明确提示历史窗口折叠。
- Live run 一收到 `run_id` 就标明“阶段性输出/机器验收仍执行”；failed terminal 使用红色机器终态提示。
  同一 tool call 的 completed 仍原位替换 started，后续 provider failure 不会把已成功 Write 改写为失败；但
  root workflow 会独立显示失败。

通用回归使用 warehouse/museum rename 和 10,000-token failure injection，不引用 V2.3、疾病、会话、route、
worker 数或报告名：未闭合 delta 产生 0 个 durable presentation 节点，完整 block/assistant step 各产生 1 个；
fresh Skill gesture 不重复，resume 不受影响；tail 只返回 newest bounded window；provider reset 后既有成功 tool
保持 succeeded、root 独立 failed。完整 Backend 为 `387 passed, 119 warnings, 2 subtests`，完整 Frontend 为
`56 passed`；Vite production build 成功，修改文件的 ESLint 全通过。warnings 是既有 passlib/
`datetime.utcnow()` deprecation；完整 Frontend lint 仍只有既有 `ModelSelector.jsx`、`SkillLibrary.jsx` 两处
effect 内同步 setState 错误，与本轮 diff 无关。尚未在两个参照 run 活动期间重启或切换生产服务。

### 生产部署与线上复验

- 用户随后明确放弃等待两个参照 run，并授权立即部署。ChatDS 先经原生 Supervisor cancel API 收口：
  `54bd48...` root `fcc892d2...` 在 seq 83,287 写入唯一 `cancelled` terminal，`b58f7f...` root
  `216cac37...` 在 seq 37,722 写入唯一 `cancelled` terminal；两容器均退出，未强杀、未制造 success。
- 切换前保留 Backend、Frontend、DeepSeek Supervisor 的
  `rollback-pre-7d9f12f9` tags。生产只切换这三个 ChatDS-owned 组件；Claude Supervisor、Claude Turn
  image、DeepSeek Harness Turn image、SearXNG、egress proxy、数据库卷和 Session workspace 均未重建。
- 生产 Backend 为 `sha256:ac828fa2...`、Frontend 为 `sha256:6e9c1d45...`、DeepSeek Supervisor 为
  `sha256:a5af4870...`；三者 revision 均为 `7d9f12f9`、restart 0，Backend/Supervisor healthy。
  `127.0.0.1`、`10.10.132.126`、`172.30.100.126` 三入口 `/` 与 `/api/health` 均为 200，Frontend
  build entry 均为 `/assets/index-l8V9nsyn.js`。
- Backend startup 从 Supervisor receipts 恢复了旧投影：生产数据库 nonterminal AgentRun 为 0；
  `ce2348...`、`05bd42...` 为 failed，`54bd48...`、`b58f7f...` 为 cancelled；SQLite
  `quick_check=ok`、外键违规 0。四个 Session 的线上 activity tail 均以一个请求返回最新 10 条、
  `has_earlier=true`、`has_more=false`，未再从 offset 0 扫描完整历史。
- 项目内部网络的 SearXNG 实际 JSON 查询为 200/10 results。无头 Chromium 精确打开
  `/chat/05bd42...`：`#root` 非空、未回登录页、0 Runtime exception、0 console error、0 个“执行中”标签；
  新 Frontend 首屏与大历史 Session hydration 可用。没有自动启动新的模型重型 V2.3；用户将用新 Session
  重新执行。外部 Provider reset/malformed output 仍可能发生，本轮保证的是正确恢复、终态和呈现，
  不把上游可用性虚报成已被 Harness 消除。

## 2026-08-24 Round 18：同输入原生双引擎新鲜 E2E 与 Claude lifecycle 修复

本轮是用户新建的两个独立生产 E2E，不是重放旧 run：Claude Code conversation
`db051b53fe7b4824a92eb27dc3d52f95`、root `db8fd6a7be1e45f7ad7eb1fc695769b5`；DeepSeek Harness
conversation `681e814526b54e2295c009207508f2b8`、root `0417d05f48b14ef4a68444d2666b601f`。两者使用同一用户、
同一 64 字符业务输入（SHA-256 `2f042f8dec9eaf2f79994e634a81da9ab11408e53dbd42952a41be049f43787c`）、
`session_full`、同一 immutable Skill view
`9aee2a0596eff2e78c48c615332e70ed3d82abc9539b701663ea7076af96d7b8` 与同一 manifest
`5b536a43b336d17007c8f38dd898165506475da756895fd71aaa22da9653bbae`。编译后 primary Skill 是
`healthsim-trialsim`，route 是 `composite_full_protocol_design`，route SHA-256
`b7ff3beb155882eebe6b34381c3af540e515d35867f9458f9a3682132e2fd921`；workflow 为 phase 0 的 7 个并行
mandatory worker，再串行 1 个 I/E worker。Artifact 合同要求 11 个模块、20 节、final 至少 153,600 bytes/
2,000 lines。三源冻结同时包含持久 conversation、完整 Skill/worker/orchestration 资源和原生 ledger/AgentRun，
没有从页面文案反推结论。

### DeepSeek Harness：权威成功

- Engine/model 为 `deepseek_harness + shaiengine_deepseek_v4_pro`，fresh root 输入正确 lower 为原生
  `/healthsim-trialsim` gesture 加用户原文。8 个声明 worker 都是 attempt 1 `succeeded/completed`，phase barrier
  顺序正确；6 次 provider retry 全部恢复，没有 mandatory tool failure。
- lossless ledger terminal seq 405,163；workflow receipt `passed/frontier=2/finding=0`，artifact receipt
  `passed/activated=1/finding=0`，Supervisor 唯一 terminal 为 `succeeded/stop`、exit 0。最终产物
  `gal301_cdp_report/GAL301_FULL_REPORT.md` 为 153,967 bytes/2,627 lines、SHA-256
  `237c13179cf98cee354cd84c0797a1b7656f4e315443b3e3c241a85d9b778a53`，与 11 模块按声明顺序 concat
  byte-for-byte 相同。模型第一次仅生成 121,437 bytes 并按自拟 100 KB 阈值声称通过；machine artifact gate
  返回精确 `artifact_min_bytes_not_met expected=153600` 后，原生 loop 扩写并重新 merge，证明控制面反馈可恢复。
- 对 GLM ground truth 的 byte/line ratio 为 0.769473/0.776530，normalized char-trigram cosine 0.662161、
  token cosine 0.876885；对 modular Claude ground truth 分别为 0.797302/1.113136、0.703700、0.912254。
  最终报告有相同 11 个核心 H1 业务模块，但标题更描述化；继续采用结构、workflow、evidence、artifact 合同与
  业务覆盖等价，不制造随机文本 byte equality。

### Claude Code：准确失败与完整故障链

- Engine/model 为 `claude_code + shaiengine_glm_5_2`，fresh native command lowering 正确。模型先启动一个
  `general-purpose` agent，随后在 mandatory receipts 之前直接编写 11 模块和 final。旧 PreToolUse 门禁共拒绝
  69 次 frontier 0 artifact synthesis；模型最终一次原生 response 并行调用精确 7 个 phase 0 agent，7 个 receipt
  全部成功。OpenAlex 持续 429 使 competitive worker 自行执行长重试，最终明确以 PubMed 证据降级完成；这是
  upstream/model 收敛问题，不是 workspace、网络策略或 adapter 断流。
- 模型把早期 generic agent 的内容保存成 `worker_D_ie_criteria.md`，但从未调用 phase 1 声明的精确
  `chatds-session-skills:healthsim-trialsim:worker-ie-criteria`。机器回执正确保持 frontier 1，并又拒绝 8 次 final
  synthesis；然而旧反馈只返回 `frontier=1`，没有把当前阶段精确 `native_agent_type/status` 带回模型。
- 模型随后通过“先写临时 `G_full.md`，再在多行 Python 中 remove/rename”的方式绕过基于 Bash 文本的静态
  final-path 识别，提前替换 final。它最终在 prose 中声称所有 phases 成功，但原生 ledger 只有 8 个 Agent 调用：
  1 个 generic + 7 个 phase 0，phase 1 attempt count 精确为 0。`result/success` 不能覆盖控制面事实。
- ledger 共 127,793 条/45,358,103 bytes，与数据库 raw index 在 terminal barrier 完全一致；8 次 Claude-owned
  provider retry 均恢复，egress `budget_rejections=0/exhausted=false`。Supervisor seq 127,793 写入唯一 terminal：
  `failed/workflow_contract_failed`、stage `workflow_contract_audit`，finding 仅
  `workflow_worker_missing phase_index=1 worker-ie-criteria`；artifact audit 正确 deferred。草稿 final 为
  147,529 bytes/2,520 lines、11 个 H1、SHA-256
  `d1cf1025d788633634fc95e6894a11c2244da88ad11e2671961035ecb89e6615`，等于 11 模块 concat，但同时低于
  153,600-byte 硬合同，不能作为成功交付。

### 跨 Skill 不变量、成熟实现对照与通用修复

本轮缺陷先重述为两个与 fixture 无关的不变量：

1. mandatory frontier 未通过时，root 不得用 optional/generic subagent 替代声明 worker；任何被阻止的 fan-in
   操作都必须反馈当前 phase 每个 worker 的精确原生类型和机器状态，并要求调用精确 `subagent_type` 或等待
   running receipt，不能只给无动作信息的 phase 编号。
2. shell/path 文本识别只能提供早期反馈，不能证明副作用时序。workflow 首次 `passed` 必须形成 controller-owned、
   durable synthesis checkpoint；declared final 必须在该 checkpoint 后发生内容变化，预生成、rename、原地 touch、
   metadata mutation 或任意脚本语言绕过都不能冒充 post-frontier fan-in。Workflow、artifact 与唯一 terminal 仍按
   单调顺序结算。

唯一成熟参考冻结为 clean 的本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`。其
`src/services/tools/toolExecution.ts` 在副作用前运行 PreToolUse，在完成后运行 PostToolUse；
`toolHooks.ts` 的 Post feedback 不能回滚已发生的文件写入。本轮 **adopt** 原生 PreToolUse 精确可执行反馈，
**adapt** durable passed-frontier checkpoint 到 ChatDS 已有 workflow/artifact authority 与 receipt，**reject**
依赖 Post hook 回滚、修改 Claude binary/core、复制第二套 tool loop 或从模型 prose 推导状态。DeepSeek 上游边界
继续 clean 于 `47f943859bef60e4160492346772ded9b24f765a`，本轮没有修改。

实现只落在 ChatDS Claude adapter：mandatory phase 前拒绝非声明 root Agent；frontier feedback 以有界 JSON
携带精确 `native_agent_type/status`；workflow 首次通过时以 root ownership、atomic replace、directory fsync 写入
workspace identity 与 declared-final SHA-256；Stop hook 和 terminal audit 共同验证该 checkpoint。文件哈希使用
`O_NOFOLLOW + fstat/lstat` 的稳定 identity，损坏、缺失或身份不匹配一律 fail closed。Stop 的 native bounded
correction 仍只允许一次，未增加 agent/retry/compaction loop。

确定性 failure injection 使用非医疗 warehouse/museum rename：inline Python 先绕过静态 recognizer 生成 final，
workflow pass 后不改内容或只 touch metadata 都必须报 `artifact_final_not_committed_after_workflow`，真正 rewrite
才通过；另有 optional generic agent pre-frontier、精确反馈、corrupt checkpoint 的回归。当前源码的 Runner
contract `56/56`、Supervisor lifecycle `38/38` 通过，`git diff --check`、Python AST 与上游仓库 clean 检查通过。
Round 18 因此计作一次通用 lifecycle/control-plane repair iteration；DeepSeek 的 passing E2E 是同轮 acceptance，
不制造额外代码变化。

### 提交、部署与生产复验

- 功能与证据提交为 `fbc87906`（`fix: enforce post-workflow artifact synthesis`）。镜像从该提交的 clean
  `git archive` 构建，不包含 dirty worktree、runtime 目录或两项用户自有删除；构建期与切换后均通过真实
  `chatds.claude-runner-image-self-test.v1`，4 个 MCP entrypoint 和 4 个 compatibility entrypoint 全部正常。
- 生产 `chat_ds-claude-runner:2.1.152` 当前解析到
  `sha256:c67c9faccf0272617622e21671901b97edafcc30e91b928d6c70e535b6e59602`，OCI revision 为
  `fbc87906`；旧镜像 `sha256:c2a6d2dd...` 保留为 `rollback-pre-fbc87906`。只重建 inert、network-none
  `claude-runner-image` anchor；Claude Supervisor、Backend、Frontend、DeepSeek、原生 CLI 与任何 workspace
  均未重启。Anchor 为 read-only、network none、pids 16、64 MiB、restart 0；Supervisor healthy/restart 0，
  仍以 `CLAUDE_RUNNER_IMAGE=chat_ds-claude-runner:2.1.152` 动态创建后续 Turn。
- `127.0.0.1`、`10.10.132.126`、`172.30.100.126` 三入口 `/api/health` 均为 200 且 storage identity 一致；
  Backend、Frontend、两个 Supervisor、SearXNG 与 egress proxy 状态不变。没有自动再发起模型重型 V2.3；
  DeepSeek 本轮已完成 acceptance，Claude 修复后的业务 E2E 由用户下一次新建 Session 验收。

## 2026-08-25 Shaiengine 401、GLM-5.3 与原生活动投影闭环

本节诊断用户新建的 Claude Code conversation `57211a178c9640c5a9cf8edaa4a9967f`、root
`7fbe534e82954d34be3e5fabda00f72d`，以及 DeepSeek Harness conversation
`25b87a66246a4e3795b2fa1f6e2f66c3`、root `3fe9779303184b5693c090262d192e2e`。两者已经各自到唯一
durable failed terminal，不重放原任务，也不计作新的 V2.3 round。诊断同时冻结持久 conversation、exact
Skill/package/resource 和 debug/AgentRun/native ledger：共同用户输入 SHA-256 为
`2f042f8dec9eaf2f79994e634a81da9ab11408e53dbd42952a41be049f43787c`，Skill view 为
`9aee2a0596eff2e78c48c615332e70ed3d82abc9539b701663ea7076af96d7b8`，manifest 为
`5b536a43b336d17007c8f38dd898165506475da756895fd71aaa22da9653bbae`，route 是
`composite_full_protocol_design`，route SHA-256 为
`b7ff3beb155882eebe6b34381c3af540e515d35867f9458f9a3682132e2fd921`。声明 workflow 仍为 phase 0
七个 mandatory 并行 worker，再串行精确 I/E worker；交付合同是 11 模块和至少 153,600 bytes/2,000 lines。

### 三源故障链与分类

- Claude 的六个 phase-0 worker 已成功；target worker 在 Claude 原生 10 次 provider retry 后以 401 结束，期间
  混有一次 502。原生 stdout 随 task-notification queue drain 产生两个不同 UUID 的 error result，二者都携带
  `api_error_status=401`。旧 controller 先按 result count 选择 `native_result_duplicated`，遮蔽了更早且更强的
  provider receipt。phase 1 未启动，workflow/artifact 失败是后置结果，不是首因。
- DeepSeek 的四个 worker 已完成；target、competitive 和 literature worker 首次与一次 Harness-owned retry 均在
  第一条模型请求收到结构化 `turn/end reason.kind=error/status=401`，原生进程 exit 1，phase barrier 正确阻止后续
  I/E worker。旧唯一 terminal 是 `workflow_contract_failed`，同样把 provider 首因误显示成后置 gate。
- 同输入、同 Skill、同 route、同 provider 的历史 DeepSeek conversation `681e814526b54e2295c009207508f2b8`
  曾完整成功，排除固定 Skill/compiler/workspace 缺陷。生产旧 credential 的 `/v1/models`、OpenAI chat 与
  Anthropic messages 三条无泄漏探针均为 401“令牌状态不可用”；替换 credential 后 `/v1/models`、OpenAI
  `deepseek-v4-pro`、Anthropic `glm-5.2`、OpenAI/Anthropic `glm-5.3` 都返回 200。分类是 provider credential/
  upstream availability 加 ChatDS terminal/UI 投影缺陷；没有 Claude Code 或 DeepSeek Harness 原生核心缺陷。
- Claude 页面碎片化来自 ChatDS Web 投影：同一 `task_id/tool_use_id` 的 358 个 task-progress、32 个 status 和
  20 个 retry 更新曾各自产生新 activity node；同一个原生 tool start 又同时生成 progress row 与 tool card。
  这解释“乱跳”和完成时另起一张卡，不能归因于模型思考本身。

### 跨 Skill 不变量、成熟实现对照与修复

1. machine-owned provider HTTP terminal 必须优先于 result multiplicity、workflow 和 artifact 等后置失败；模型正文和
   stderr 文本不得成为 lifecycle authority。DeepSeek 仅在 native exit 非零时用结构化 provider error 精化首因，
   已被后续重试恢复的历史 429 不能推翻 clean exit。
2. 一个原生 task/tool identity 只能拥有一个稳定 Web node；progress 必须原位 merge，root terminal 后不能残留
   running。带 `tool_use_id` 的原生 progress 合并进同一 tool card，completed/failed 继续用原 ID 结算；缺 ID 的
   状态才作为独立有界 progress surface。
3. progress 是 ephemeral presentation update，不应人为切碎连续 reasoning/content；raw native ledger 仍完整保留。
   子任务 `<details>` 的用户展开状态也不能因 lifecycle rerender 被强制折叠。
4. 新模型 route 必须新增 identity；历史 `shaiengine_glm_5_2` 永久绑定 wire `glm-5.2`，新 Session 默认选择
   `shaiengine_glm_5_3 -> glm-5.3`，不得静默重绑旧 conversation。

唯一成熟参考再次冻结为 clean 的本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`。其 `src/QueryEngine.ts`/SDK schema 为 tool progress 保留
`tool_use_id`，`src/cli/print.ts` 在 background task-notification drain 期间允许多个原生 result/notification 边界，
`src/query.ts` 继续由原生 loop 持有 pending task 与 retry。这里 **adopt** stable native identity，**adapt** provider
receipt precedence 和 bounded projection 到 ChatDS 既有 terminal/activity ledger，**reject** 修改 Claude binary、
复制 agent/retry/compaction loop，或按 Skill/session/model prose 特判。DeepSeek 上游仍 clean 固定为
`47f943859bef60e4160492346772ded9b24f765a`。

实现提交 `be8850a5` 只改 ChatDS adapter、Backend model binding 和 Frontend。确定性回归使用
warehouse/museum/factory rename、两个不同 task identity、10,000 次同 ID progress、结构化 429 与 malformed status；
生产代码不含临床、V2.3、session、route、worker 数或报告名。clean archive 回归为 Backend `394 passed,
119 warnings, 2 subtests`，Claude Runner `125 passed, 1 skipped, 19 subtests`，Frontend `59 passed`；Vite build、
修改文件 ESLint、Compose、AST/diff/genericity 与两个 native image self-test 全通过。skip 是既有环境条件测试；warnings
仍是 passlib/`datetime.utcnow()` deprecation。

### Credential、部署与线上证明

- replacement credential 仅写入权限受限、Git ignored 的部署 secret 文件和 `.env`，未进入源码、文档、日志或
  image layer；因为 credential 曾在对话中明文暴露，当前可用不等于长期安全，后续仍应再次轮换。
- 六个镜像均从 clean commit `be8850a5421ed68405aaa47d9b49ed0c973466eb` 构建：Backend
  `sha256:cf6446c4...`、Frontend `sha256:62b9a5c4...`、Claude Supervisor `sha256:fbd37c39...`、Claude Turn
  `sha256:73c32a6b...`、DeepSeek Supervisor `sha256:031e03d0...`、DeepSeek Turn
  `sha256:ab0ad450...`。六个旧镜像均保留 `rollback-pre-be8850a5`；DeepSeek image 的 upstream revision label
  仍为 `47f943...`，另用 ChatDS revision label 记录本提交。
- 切换前 active AgentRun 和动态 Turn container 都为 0，SQLite `quick_check=ok`、外键违规 0。部署只重建六个
  ChatDS 组件/anchor；数据库卷、所有 Session workspace、SearXNG、Valkey、egress proxy 与 native source 未动。
  切换后六组件 restart 0，Backend/两个 Supervisor healthy；三个 Frontend 入口 `/` 与 `/api/health` 均为 200，
  entry 一致为 `/assets/index-FoxG4oKM.js`，storage identity 一致。SearXNG 容器内真实查询返回 7 条结果。
- 实际旧账本在候选 wrapper 中离线重放分别得到 Claude `provider_http_401`（两个 result）和 DeepSeek
  `provider_http_401`，证明新 causal attribution 精确命中本次故障。生产专用测试账户随后创建 Claude conversation
  `700997619a0c44fe95486c84081a63f1`/root `dcc3890b23a74fc08180018e8c130ea9` 和 DeepSeek conversation
  `9f525785eff0487d90b439cf21a4ab17`/root `4dd6a0680af140f8933286a7c627c43c`；两者均用
  `shaiengine_glm_5_3` 权威 succeeded，分别 `end_turn` 与 `stop`，无 error。无头 Chromium 打开前者时
  `#root` 有内容、GLM-5.3/SMOKE_OK 可见、未跳登录、0 Runtime/console/Log error、0 个残留“执行中”。
- 本次只运行两条低成本无 Skill smoke，没有自动启动模型重型 V2.3。新的复杂业务 E2E 仍由用户创建新 Session
  验收；旧 failed conversation 的历史 terminal 不会被篡改成 success。
- GitHub 目标分支在 push 前回读为远端落后本地两个提交且无并发更新；但当前 Codex 环境没有 HTTPS credential、
  SSH key/agent 或已连接 GitHub plugin，生产主机也没有可复用认证。HTTPS、SSH 22/443 均在写入前安全退出，
  因此 `be8850a5` 与本节文档提交当前只在本地，待用户提供非交互 GitHub 授权后再 push；绝不把 token 放进
  remote URL、命令行、Git 配置或仓库。

## 2026-08-26 原生双引擎长响应与 Web 增量投影修复

本节关联三个独立生产 Session：Claude Code `6d3b593d2722473cb17cbaa14a79146d`、DeepSeek Harness
`ae7d9ad9499147f2863637a7cb4d9003` 和 `dceefaa22243421da40dfc6e9496b75c`。三者的持久 conversation、
AgentRun/debug/native event ledger 与 immutable Skill view 已分别冻结并交叉核对：用户输入、Skill view
`9aee2a0596eff2e78c48c615332e70ed3d82abc9539b701663ea7076af96d7b8`、19 个 Skills、9 个 agents、17 条
compiled routes 和交付合同一致，因而不能把不同终态归因于 Skill/compiler 漂移。

- Claude root `4b3a44f33ee840fcbee44bf80619b073` 已权威 succeeded，8 个声明 worker 全部 succeeded。
- Shaiengine DeepSeek root `53a71fcd142d457cb048824b2e501b3a` 的 8 个 worker 和 workflow 均成功，最终 Provider 返回结构化
  HTTP 403 precharge 拒绝；机器终态是 `provider_http_403`。当时草稿 142,676 bytes，尚未满足 153,600-byte
  artifact 合同。首因是外部账户余额/额度/模型权限，artifact 不足只是未获后续模型回合的结果。
- 本地 DeepSeek root `30839b09578045f588e0e3c1296ebb98` 在本节初次冻结时仍为 running。14 个 child attempt 已因
  transport 失败结算；native engine 的 stream idle watchdog 为 7,200 秒，但 ChatDS 共享 egress response relay
  仍使用全局 30 秒 idle，且 session loopback bridge 为 660 秒。Provider 在负载下超过 30 秒无下一字节时由
  ChatDS 边界提前关闭连接，DSH 只负责把该 transport 失败按其原生流程重试。该旧 run 的最终终态将在部署记录
  中补记，不能作为修复后验收。

页面“乱跳”另有独立、可确定复现的 ChatDS 投影原因：refresh hydration 每次丢弃 durable placeholder 的既有
`activityNodes`，bounded tail 又用移动窗口替换完整时间线；空 activity poll 仍触发 state write，live durable
update 反复执行 smooth scroll。原生事件 seq 自身单调。worker 显示匿名则是 child `turn/start` 可能先于 root
`tool-workflow/agent-start`，旧 projector 没有在同一稳定 child run identity 上回填后到的原生 label。

### 跨领域不变量、成熟实现对照与通用修复

1. 共享传输层不得比 native engine 的 provider stream watchdog 更早结束合法的精确 Provider exchange；较长
   idle budget 只能作为 v3 signed authority 的 exact-query POST rule 数据，不能扩大 Skill、MCP、public-read、
   origin、method 或 path 权限。普通读取继续使用短全局 idle。
2. Web projection 对同一 root/tool/worker identity 必须 append/merge-in-place。bounded replay 只能补充或结算已知
   node，不能删除早期 reasoning/tool/content；空 poll 是 no-op，live follow 使用非动画滚动。
3. 后到的机器身份元数据只能更新既有 child run，不得新建第二个 worker。机器 terminal code 保持权威，安全的
   用户提示由结构化 HTTP code 映射，不能从模型 prose 猜测因果。

唯一成熟参考冻结为 clean 的本地 `claude-code/` commit
`6f6f12b37f529488b10e53928dd5508bb93535c7`。`src/services/api/claude.ts` 由 native stream watchdog 持有
provider stall/retry；`src/cli/transports/ccrClient.ts`、`SerialBatchEventUploader.ts` 与 `HybridTransport.ts` 使用稳定
identity、ordered pending、retry/flush 来保持增量顺序。本轮 **adopt** 精确路由 transport budget 与稳定 ID，
**adapt** append-only hydration 到现有 ChatDS receipt/projection contract，**reject** 在 adapter 中复制 provider
retry、compaction 或 agent loop。DeepSeek 上游仍 clean 固定于
`47f943859bef60e4160492346772ded9b24f765a`；两个 native source tree 都没有修改。

确定性回归使用 warehouse、power-grid、energy-grid 等非 V2.3 rename/mutation：31 秒 delayed byte 在普通 30 秒
route 必须结束，在 exact Provider 60 秒 signed route 必须通过；GET 或 prefix POST 不得携带该字段；移动的 5,000-event
tail 不得删除既有 reasoning/tool/content；10,000-update 既有稳定 ID 行为继续通过；child 先 start、descriptor 后到时
run ID 不变。当前复验为 Frontend `63/63`、Vite production build、proxy `84/84`、runner/bridge `44/44`、
Supervisor lifecycle `38/38`、Python AST 14 文件和 diff/Compose 静态检查通过。生产切换仍等待上述 active root
落终态，未在本节记录时重启任何服务。
