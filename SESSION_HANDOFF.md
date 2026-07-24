# ChatDS 当前会话交接（2026-07-24）

> 本文件是本仓库唯一的权威续接入口。新 Codex/Claude Code 会话必须先完整阅读本文件，再查看 Git、测试和生产状态。旧 `_SESSION_*.md`、`_HARNESS_*.md`、`_REMOTE_OPS.md` 只用于历史追溯。

## 1. 当前结论

- 工作目录：`/nfs/yangbb/codes/chat_ds`。
- 分支：`fix/generic-skill-harness-20260717`。
- 2026-07-24 最新功能提交：`e90415a0 feat: close generic skill workflow recovery gaps`。
- 前一轮 profile-aware sandbox 提交：`b0744a33 feat: add generic profile-aware skill sandboxes`。
- 本轮通用 Skill Harness、process protocol v2、profile-aware preflight、base/browser executor 和 browser egress proxy 已完成实现、全量回归与真实容器验收。
- `e90415a0` 已按 browser → Harness 顺序部署到生产，并通过真实私网浏览器和非模型 smoke。
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

## 5. 当前验证证据

2026-07-24 最终冻结源码已通过：

- Harness 全量：`1341 tests OK`；首次只挂 Harness 目录时唯一失败是测试环境缺根目录 `executor`，补齐根目录和固定 Node 后完整通过。
- 关键故障路径定向：`263 tests OK`；最后安全/浏览器增量定向：`135 tests OK`。
- Backend：`47 passed`。
- legacy browser sidecar：`8 tests OK`。
- Executor/browser/profile/topology/proxy：`86 passed, 1 skipped, 43 subtests passed`。
- 最终 Shell/profile 定向：`78 passed, 40 subtests passed`；独立 reviewer 的 23-case Bash 矩阵也通过。
- `py_compile`、`git diff --check`、默认 Compose config、`local-search` profile config 均通过。

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

- 生产主机：用户访问地址 `172.30.100.145`；历史管理地址/secret 文件名为 `10.10.130.178`。二者指向同一 ChatDS 主机。
- Compose project：`chat_ds`。
- 生产工作目录：`/nfs/yangbb/codes/chat_ds`。
- 前端：`http://172.30.100.145:5173`。
- 搜索机：`10.10.132.126`，Harness 使用 SearXNG `http://10.10.132.126:8088`。
- 只读部署预检已确认：
  - 新服务无公开端口、无现有数据卷迁移；
  - UID 65528/65529 当前无宿主进程冲突；
  - 根盘约 87 GiB 可用，内存约 26 GiB available；
  - Compose 2.32.4 支持当前声明。
- 2026-07-24 13:04（Asia/Shanghai）完成最新生产切换。固定使用 project `chat_ds` 和原 bind/data 路径，只重建 legacy browser 与 Harness，没有重建 Frontend、Backend、DB、search 或两个 Skill executor。
- `.env` 已原子生成独立 `EXECUTOR_V2_AUTH_TOKEN`，mode 为 0600；base/browser/Harness 三方值一致且长度合规，值未输出或写入 Git。
- Harness stream ceiling 为 2400 秒，Backend proxy deadline 为 3000 秒。

当前生产镜像：

| 服务 | Image ID | 状态 |
|---|---|---|
| `chat_acits_executor` | `sha256:a7afa67c6c2f0ffe08e27cd8b5b5101b08444e71e6008b85efacf9c6784ad14f` | healthy |
| `chat_acits_skill_egress_proxy` | `sha256:c5ee4fdc2ee785868f15036706f01d327b05b358f2b7812fcca8bfb7454f9c05` | healthy |
| `chat_acits_skill_browser_executor` | `sha256:76acea01fdf89f324fef6c48e44d6270841bbb8127887e8cf2e082cd76a84b90` | healthy |
| `chat_acits_browser` | `sha256:391260b06964c7cfbd2bb934501f35b47ed5d093ac6e1d51f769c41e3576087d` | healthy |
| `chat_acits_harness` | `sha256:e7546f176135d32dd4b15a4d4a289fa495cc2da5f0f1dd4b2a3a2eca9eae152d` | healthy |
| `chat_acits_backend` | `sha256:4da7df118bcd06f7b30e57191f0d0a275e238f27beb12ee10a0510e6d32134fb` | running / `/api/health` 200 |

生产 smoke 证据：

- browser worker 只有 loopback、无 route、UID/GID 65529、零 capabilities；直连 public/private/metadata 均失败。
- proxy public HTTPS 成功；loopback/private/metadata 均为 403；worker 无 controller/proxy UDS authority。
- 真实 `run_skill_process` 四路通过：base identity、`${SKILL_DIR}` Bash direct helper、headed Node Playwright、persistent Python BrowserProbe；snapshot digest 全匹配，cleanup retained=0。
- legacy CDP browser 真实打开 `https://example.com/` 并得到 `Example Domain`。
- Harness `/health` 和 `/v1/models` 正常；未鉴权 `/internal/*` 为 401，Backend 持有的正确 token 为 200。
- Frontend `/` 与 `/api/health` 均为 200。
- Harness `/health` 为 200；本轮启动日志中 browser/Harness 均无 traceback、fatal 或 unhealthy。
- legacy browser 对两个精确私网 origin 均成功跟随 302 到 OpenEMR login 页面并取得 DOM snapshot；未列入 allowlist 的私网 origin 和 metadata 地址仍被拦截。
- Chromium 进程含精确 SPKI exception flag，且不含全局 `--ignore-certificate-errors`。
- SearXNG 真实查询返回 46 条结果，前十条 provenance 包括 360search、Baidu、Mojeek、Sogou。
- worker UID 65528/65529 在 smoke 后宿主任务数均为 0；Harness 镜像内 baked runtime-data 文件数为 0。
- 切换前 DB 中存在 7 月 20 日遗留的 stale `running` 投影，但最近 30 分钟 AgentRun/TaskItem/ScheduledRun 均为 0，Harness/Backend 也无 established provider/SSE 连接。
- 本轮没有运行模型重型 V2.3 E2E。

回滚点：

- 原 executor/browser/Harness/Backend 镜像保留 tag `rollback-20260723-pre-process-v2`。
- 本轮切换前 browser/Harness 镜像另保留 tag `rollback-pre-e90415a0`；新镜像 tag 为 `deploy-e90415a0`。
- 可重建的旧 Harness 代码镜像：`chat_ds-harness:rollback-d224db33`，image `sha256:e7d16ee538fc69e638f20bb93035df90d76008721116ebfedb7d07ccb986abef`。
- 前轮 `b0744a33` 的部署 bundle 和构建日志位于 `/nfs/temp/chat_ds_deploy_b0744a33/`，不属于 Git；本轮直接从共享仓库的已提交 `e90415a0` 源码构建并保留 commit-tagged 镜像。

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
- session-wise 隔离是固定容器池内的 lease/root-run 隔离，不是每 chat 动态创建容器。
- 依赖 profile 固定且不可运行时安装；复杂动态 Bash/Node/Python 需 exact marker/manifest。
- browser Skill lane只允许公共 HTTP(S)，不支持 CAPTCHA、stealth、反爬绕过或未确认的重要操作。
- stdio MCP 已降权和隔离 ambient secret，但不是完整 mount/network namespace；仍只注册可信配置。
- 免费搜索引擎健康度、CAPTCHA、协议变化和上游站点 4xx/5xx 是动态外部条件，不能误归因为 Harness 回归。
