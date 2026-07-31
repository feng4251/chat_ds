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

## Round 2

Round 1 已完成回归、commit 与生产切换；下一步创建全新 conversation/root，使用同一
历史业务输入开始 Round 2。

## Round 3

待执行。

## Round 4

待执行。

## Round 5

待执行。
