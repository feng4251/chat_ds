# ChatDS 当前会话交接（2026-07-23）

> 本文件是本仓库唯一的权威续接入口。新 Codex/Claude Code 会话必须先完整阅读本文件，再查看 Git、测试和生产状态。旧 `_SESSION_*.md`、`_HARNESS_*.md`、`_REMOTE_OPS.md` 只用于历史追溯。

## 1. 当前结论

- 工作目录：`/nfs/yangbb/codes/chat_ds`。
- 分支：`fix/generic-skill-harness-20260717`。
- 本轮开始时 HEAD：`d224db33 feat: recover corrupt tool streams generically`。
- 本轮通用 Skill Harness、process protocol v2、profile-aware preflight、base/browser executor 和 browser egress proxy 已完成实现、全量回归与真实容器验收。
- 独立 reviewer 已给出代码/测试 `GO`；当前正在做选择性本地 commit 和分阶段生产部署。
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

## 5. 当前验证证据

最终冻结源码已通过：

- Harness 全量：`1331 passed, 1 skipped, 526 subtests passed`。
- Backend：`47 passed`。
- Executor/browser/profile/topology/proxy：`86 passed, 1 skipped, 43 subtests passed`。
- 最终 Shell/profile 定向：`78 passed, 40 subtests passed`；独立 reviewer 的 23-case Bash 矩阵也通过。
- `py_compile`、`git diff --check`、默认 Compose config、`local-search` profile config 均通过。

真实容器验收镜像（尚未代表生产部署状态）：

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
- 当前代码/测试已获部署 `GO`，但本节写入时新双-executor 拓扑尚未切入生产。部署完成后必须更新本节和镜像/smoke 证据。

部署必须分阶段：

1. 固定 project `chat_ds` 和原工作目录/bind mounts，不使用新 release 目录指向空 data。
2. 原子确保 `.env` 中存在独立高熵 `EXECUTOR_V2_AUTH_TOKEN`，并验证 Backend deadline 大于 Harness stream ceiling；不输出值。
3. 保留旧镜像/compose 回滚点；加载或构建与验收一致的 final executor/browser/proxy 镜像。
4. 先启动 socket init、proxy、browser executor canary；再切 base executor 和 legacy browser。
5. 确认无活跃 AgentRun/SSE 后切 Harness，最后切 Backend；不重建 Frontend/DB/search。
6. 运行非模型 smoke：service health、双 UDS capabilities/reap、真实 base/browser `run_skill_process`、public proxy/private deny、legacy browser、Harness 内部鉴权、Backend/Frontend HTTP。
7. 不运行 V2.3 模型 E2E。

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
