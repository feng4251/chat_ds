# Harness v2.3 Skill 修复上下文（2026-07-16）

> **历史演进记录**：本文保留 2026-07-16/17 的诊断。2026-07-21 的当前代码、部署、测试与续接步骤请以仓库根目录 `SESSION_HANDOFF.md` 为准。

## 2026-07-17 第二轮：通用 workflow runtime、图片短问答与生产部署

### 两个独立问题

1. `ecbc00c03a404e0a97ad892f0adf837a` 的 V2.3 失败来自可执行大参数在
   conversation history 压缩后被模型当成真实 `write_file.content` / `execute_code.code`
   重放，并且原 runtime 没有真正执行声明的 multi-agent DAG。
2. `a993d814d2bd41a2900b7d5f210c214b` 不是 Skill 或 multi-agent 反复查询。
   Debug JSONL 的真实统计是 60 次 `llm.request` / 60 次 `finish_reason=length`、
   0 tool、0 child。约 1,005,959 字符的 image data URL 被按普通文本估成约
   251k tokens，输出被错误钳到 512 tokens；旧 continuation 又只追加
   `continue`，没有携带 partial assistant，模型于是从图片开头重做 OCR 60 次。

### 本轮通用修复

- 新增共享 multimodal-aware token estimator：图片 transport/base64 字节不再按文本
  计数，caption/metadata 和普通长文本仍正常计数；preflight 不再凭空制造 512-token
  输出预算。
- length continuation 保留 partial assistant、有界为 4 次、去除全文/前缀重放；只有
  再次 `length`、无 tool calls 且无实质新增时才判 repetition。provider stream 已输出
  visible/reasoning delta 后禁止透明整轮 retry，避免重复持久化。
- provider 实报 context overflow 后保留其 token 真值；forced compression 必须让 payload
  和 token 估算产生可测下降，否则 fail/switch provider，不再用忽略图片 transport 的本地
  估值宣称“恢复”。Compressor 的 tail/prune 也复用图片感知估算。
- 普通聊天不会因为 session 中“存在 Skill”就自动获得 160 iterations、8K/120K 报告 gate
  或强制 Skill inspection；只有用户请求本身是复杂交付，或已激活的 compiled plan 声明
  full output，才进入复杂 workflow。Markdown 聊天格式不再等同于持久化 `.md` 文件请求。
- 复杂 session Skill 首轮只能 `skill_view`；随后按 inspection → intent → bootstrap → worker
  waves → ordered aggregation → artifact synthesis → mandatory merge 的 phase-specific capability
  policy fail-closed 执行。父级对编译好的确定性 phase 元数据执行 auto-dispatch，模型不再
  转录大段内部 JSON；非法批次在任何 side effect 前原子拒绝。
- 每个 delegate task 必须精确提供 Skill、step type、step ID（无重复/缺失/额外项）、worker
  file、prerequisite result paths 和显式工具 allowlist。Child 不再隐式继承全工具或 session
  MCP catalog；声明的 `WebSearch` 等别名映射到现有 canonical tool，Skill capability 通过
  `skill_view` / `run_skill_python` 执行。
- child 的 Skill/resource 审计只在同一 `tool_call_id` 收到 successful `tool.completed` 后记账；
  失败读取不再伪装成已满足。仅 intent 分类在 required output、最终行 JSON footer 和全部
  audit 已完成、且终止原因确属 length/budget 时，才允许保留为带 `runtime_warning` 的完成项。
- Intent footer 必须是最后一个非空行的完整 JSON；示例、代码围栏和尾随文本不再被当成
  dispatch 数据。用户显式给出的 `dimension=value` 只在 value 属于 Skill enum 时作为确定性
  hint，仍由 intent/resource step 验证并读取映射资源。
- `skill_view` 对声明为目录的资源返回有界文件列表，而不是抛 `IsADirectoryError`。

### 验证和部署

- 当前完整套件：宿主与生产依赖镜像均为 `131 tests OK`；生产镜像环境有 3 个外部条件
  用例按设计 skip。`py_compile` 和 `git diff --check -- harness` 通过。
- 原始 V2.3 包仍编译为：9 workers、17 routes、5 intent dimensions、7 bootstrap、
  6 ordered aggregation，compiler errors = 0。显式 full intent 确定性解析为
  `composite_full_protocol_design`：7 parallel + 1 sequential worker、full output=true。
- 生产只滚动替换 `chat_acits_harness`；backend/frontend/executor 未重启。镜像为
  `sha256:57e5dc2471f9f6e571006b5d926487af6405f13dae9129d515fcc691c231d619`，
  容器内 `http://127.0.0.1:8020/health` 返回 200。
- 独立 tmpfs workspace 的真模型 V2.3 smoke 已证明父级按合同读取主 Skill、orchestrator、
  9 worker contracts、format 和 CNS 资源，并由 harness 自动进入 intent child；该长跑验收
  与生产会话/数据库隔离，继续记录后续 bootstrap/worker/aggregation/artifact 结果。

## 2026-07-17 通用化修复与生产部署状态

用户在 session `ecbc00c03a404e0a97ad892f0adf837a` 的 debug 中再次复现大量
`write_file` / `execute_code` compacted-history placeholder 重放。该次运行共
160 iterations、189 次工具调用、49 次错误；其中 44 次直接来自压缩参数占位符
被模型重新提交，且整轮没有执行 `delegate_task`，最终报告只有 67,437 bytes / 1,188
lines，低于 Skill 声明的 153,600 bytes / 2,000 lines。

本轮没有按 V2.3 文件名或 GAL3 主题硬编码，而是补齐了通用复杂 Skill runtime：

- 编译器保真 route 的 default/full/output/resource 元数据，并对非法路径、缺失资源、
  invalid intent default、required dimension 无 enum、依赖环等执行 fail-closed lint。
- manifest/orchestrator/intent source 先加载；intent 未解决前不加载全量 worker/format，
  不允许依靠“complete/full report”等用户措辞或 worker 覆盖比例猜 full route。
- intent 支持 required/optional/default/nullable/on_missing；worker mapping 优先复用 exact
  或唯一最小超集 route 的声明 waves，歧义时澄清/阻断。
- intent resource mapping 对所有选中 route 生效；选中的本地 Skill 资源必须由 child 的
  structured tool audit 证明已用 `skill_view` 读取。
- parallel wave 即使拆成 `6 + 1` 尾批，尾批仍保持只读隔离；worker 只继承 intent、
  bootstrap 和声明依赖的 result paths，不继承同波 sibling 输出。
- child 必须读取 exact worker file 和全部 prerequisite result paths；父级 ledger 会再次
  独立校验，不能通过伪造 completed result 绕过。
- 长工具参数执行后从模型可见历史移除，完整结果只通过持久化 `results/...` 路径传播；
  malformed JSON 与 compacted placeholder 均保留真实错误并拒绝执行。
- 最终质量 gate 按 Skill 自身合同验证模块/总行数、marker 实值、checklist 行和 section
  映射、README 索引、exact artifact set、mandatory merge receipt 与首尾边界。

验证：

- 本地：`107 passed, 20 subtests passed`，`git diff --check` 通过。
- 原始 V2.3 zip：9 workers、17 routes、5 intent dimensions、7 bootstrap sources、
  6 aggregation steps；compiler errors = 0。
- full design route：7 parallel + 1 sequential，11 modular / 14 total artifacts，
  minimum 153,600 bytes / 2,000 lines。
- safety review：intent 后严格缩窄为 2 parallel workers，`requires_full_output=false`。
- 生产：2026-07-17 已只重建并替换 `chat_acits_harness`；新镜像
  `sha256:ca497aae63f19d33e323291dd292eccf726dd15f6fa47...`，`/health` 返回 200；
  backend/frontend/executor 未重启，原 session URL 返回 HTTP 200。
- 生产新镜像直接挂载原始 V2.3 zip 重放，上述 full/safety 路由和合同指标一致。

注意：生产 runtime 镜像不安装 pytest；镜像内已执行 `py_compile`，完整 pytest 套件在
宿主开发环境执行。后续仍应以一个全新 session 做真实模型端到端验收，旧 session 的历史
占位符和失败重试不会被回写清除。

## 当前目标

用户要让 ChatDS harness 在执行复杂 skill，尤其是 v2.3 `healthsim-trialsim` 时，严格按 skill 自己声明的 workflow 完整执行，最终产物应尽量接近 v2.3 ground truth md。

核心原则：
- **skill 是执行权威**：最终执行路径、worker 顺序、模块文件、merge、post-merge checks、输出契约都必须来自 skill 文件。
- harness 不应自创 GAL3 专属路径、不应提前满足、不应因为某个 report 文件存在就结束。
- v2.3 skill 要求所有 worker 内容完成后，再统一写入/合并报告；当前重点是保证 `write_file` / `execute_code` 能稳定写完整 worker 内容。
- 不保存明文密码、token、API key；远端访问凭据只临时用环境变量/本地 secret 文件。

## 最近用户反馈

新部署后用户在新会话测试：

- URL: `http://10.10.130.178:5173/chat/82bd2865c6f646069a77b69736ec5de3`
- 问题：工具错误更多，尤其是：
  - `write_file` 把 `{"_chatds_argument_omitted": true, ...}` 这类压缩摘要当内容写入；
  - `execute_code` 把 `{"_chatds_argument_omitted": true, ...}` 当 Python 执行；
  - malformed tool JSON 被误报为缺少 `code`/`filepath`，诱导模型重复错误；
  - `execute_code` 对 `GAL3_AD_CDP/...` 相对文件路径跑到临时 executor 目录，导致 `FileNotFoundError`；
  - 最终仍耗尽 160 iteration：`Agent iteration budget exhausted after 160 iterations`。
- 用户纠正：不要提前满足；v2.3 要所有 worker 内容完整后统一写入。

## 已定位的根因

### 1. 历史压缩摘要可复制，污染 `write_file` / `execute_code`

旧逻辑 `_compact_tool_call_arguments()` 会把大 `write_file.content` 压缩成：

```json
{"content": {"_chatds_argument_omitted": true, "kind": "large_file_content", ...}}
```

或把大 `execute_code.code` 压缩成：

```json
{"code": {"_chatds_argument_omitted": true, "kind": "large_argument", ...}}
```

模型会从历史里复制这些结构，导致：
- `write_file` 成功写入一个 JSON 占位对象字符串，而不是真实 worker 内容；
- `execute_code` 将 JSON 元数据当 Python 代码执行并报错。

### 2. omission guard 只识别旧式文本占位符

`tools/omission_guard.py` 原来只识别：
- `__CHATDS_OMITTED_TOOL_ARGUMENT_...`
- `[large argument omitted: ...]`
- `__CHATDS_OMITTED_TOOL_CONTENT_...`

但不识别结构化摘要：
- dict: `{"_chatds_argument_omitted": true}`
- JSON string: `"{\"_chatds_argument_omitted\": true, ...}"`

### 3. registry 把 parse error 剥掉后误报 schema error

`tools/registry.py` 在 validate 前会 `_strip_unexpected_args()`，导致 `__tool_arg_parse_error` 被剥掉，最终错误变成：
- `Tool execute_code missing required field 'code'`
- `Tool write_file missing required field 'filepath'`

这会误导模型继续补字段，而不是修复 malformed JSON。

### 4. `execute_code` 相对 workspace 文件操作有时进临时 executor

`execute_code` 只在检测到有限文件操作或绝对 workspace 路径时进入 managed session runtime。很多相对操作如：

```python
os.listdir("GAL3_AD_CDP")
os.path.getsize("GAL3_AD_CDP/01_executive_summary.md")
open("GAL3_AD_CDP/...")
```

会跑在 `/tmp/exec_*` 临时目录，找不到 session workspace 文件。

## 已完成的本地修复

修改文件：

- `harness/agent_loop.py`
- `harness/tools/omission_guard.py`
- `harness/tools/registry.py`
- `harness/tools/code_execution.py`
- `harness/tests/test_tool_argument_and_report_quality.py`

### 修复 1：历史压缩不再保留 copyable 的 `content` / `code` 字段

`agent_loop.py`：
- 新增 `_omitted_argument_summary(kind, chars)`。
- `_compact_tool_argument_value()` 对大 `write_file.content` / `patch_file` 内容 / `execute_code.code` 返回 metadata。
- `_compact_tool_call_arguments()` 对：
  - `write_file`: 删除 `content`，改为 `content_omitted`；
  - `execute_code`: 删除 `code`，改为 `code_omitted`。

这样模型历史里不再出现可直接复制为工具参数的占位 `content` / `code`。

### 修复 2：结构化 omitted metadata 统一拦截

`tools/omission_guard.py`：
- 增加 JSON 解析；
- 对 dict/list 递归检测；
- 识别：
  - `_chatds_argument_omitted: true`
  - `_chatds_arguments_omitted: true`

即使模型从旧历史复制 JSON string，也会被 `write_file` / `execute_code` / `run_skill_python` 拒绝，不会再写入成成功文件或执行。

### 修复 3：malformed JSON 优先报真实 parse error

`tools/registry.py`：
- 在 `_strip_unexpected_args()` 前先检查 `_validate_args()` 是否为 `__tool_arg_parse_error`；
- 如果是 parse error，直接返回 malformed JSON 错误。

避免误导为缺少 `code` / `filepath`。

### 修复 4：相对文件操作统一进入 managed session workspace

`tools/code_execution.py`：
- 扩大 `_WORKSPACE_FILE_OP_RE`，识别：
  - `open(`
  - `Path(` / `pathlib.Path(`
  - `os.listdir/scandir/stat/walk/chdir`
  - `os.path.getsize/exists/isfile/isdir/join`
  - `glob.glob`
  - `.read_text/read_bytes/stat/exists/glob/rglob`
- 命中后走 `run_managed_python_code()`，cwd 为 session workspace。

这样 `execute_code` 里读写/统计 `GAL3_AD_CDP/...` 相对路径会在正确 workspace 下执行。

## 已增加测试

`harness/tests/test_tool_argument_and_report_quality.py` 新增/更新：

- `test_compacted_write_file_history_does_not_emit_copyable_placeholder`
- `test_compacted_write_file_history_moves_omitted_content_out_of_content_field`
- `test_compacted_execute_code_history_moves_omitted_code_out_of_code_field`
- `test_omission_guard_rejects_structured_metadata_strings`
- `test_relative_workspace_file_operations_use_managed_runtime`

并保留之前的：
- workspace snapshot regression；
- duplicate heading scope regression；
- workflow warning 不作为 blocker regression。

## 验证状态

本地：

```bash
PYTHONPYCACHEPREFIX=/tmp/chatds_pycache PYTHONDONTWRITEBYTECODE=1 \
python3 -m py_compile agent_loop.py tools/omission_guard.py tools/registry.py tools/code_execution.py tests/test_tool_argument_and_report_quality.py

PYTHONPYCACHEPREFIX=/tmp/chatds_pycache PYTHONDONTWRITEBYTECODE=1 \
python3 -m unittest tests.test_tool_argument_and_report_quality tests.test_skill_contract_parsing tests.test_workspace_artifact_reconcile tests.test_no_contract_fallback
```

结果：`17 tests OK`。

远端：

- 已同步到 `root@10.10.130.178:/nfs/yangbb/codes/chat_ds/`
- 已重建 `chat_ds-harness` 镜像
- 已重启 `chat_acits_harness`
- 远端容器内测试：`17 tests OK`
- 已确认宿主机源码 hash 与容器 `/app` 内代码 hash 一致

远端服务：
- `chat_acits_harness` 运行最新镜像；
- `chat_acits_executor` healthy；
- 前端仍在 `10.10.130.178:5173`。

## 下一轮用户测试应重点观察

新开 v2.3 skill 测试时，重点看这些错误是否消失或大幅减少：

1. `write_file` 不应再成功写入：
   - `{"_chatds_argument_omitted": true, ...}`
2. `execute_code` 不应再执行：
   - `{"_chatds_argument_omitted": true, ...}`
3. malformed JSON 不应再被误报为：
   - `missing required field 'code'`
   - `missing required field 'filepath'`
4. `execute_code` 中相对路径不应再因临时 executor cwd 报：
   - `FileNotFoundError: GAL3_AD_CDP/...`
5. 如果仍失败，应继续看是否是：
   - 模型没有按 v2.3 worker workflow 收集全量 worker 输出；
   - skill workflow gate 对 worker 完成度的判断还不够强；
   - final report 内容质量仍低于 ground truth，需要增强 worker-output completeness verifier。

## 不要误判完成

不要因为 `GAL3_AD_CDP/GAL3_AD_FULL_REPORT.md` 或某个 `*_FULL_REPORT.md` 存在就说完成。

必须确认：
- v2.3 skill 的 worker workflow 完整执行；
- 所有声明 worker 内容完成；
- modular files 完整；
- final merge 符合 skill 声明；
- post-merge checks 满足；
- 没有 omitted metadata / placeholder 污染；
- 内容质量和结构接近 ground truth。

## 远端部署命令参考

使用已有本地 secret 文件，不要写入或打印密码：

```bash
set -a
source /nfs/yangbb/codes/chat_ds/.local_secrets/remote_10.10.130.178.env
set +a
export SSHPASS="$CHATDS_REMOTE_PASSWORD"
```

同步文件并重建 harness：

```bash
for f in \
  harness/agent_loop.py \
  harness/tools/omission_guard.py \
  harness/tools/registry.py \
  harness/tools/code_execution.py \
  harness/tests/test_tool_argument_and_report_quality.py; do
  sshpass -e scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "/nfs/yangbb/codes/chat_ds/$f" \
    "$CHATDS_REMOTE_USER@$CHATDS_REMOTE_HOST:/nfs/yangbb/codes/chat_ds/$f"
done

sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  "$CHATDS_REMOTE_USER@$CHATDS_REMOTE_HOST" \
  'cd /nfs/yangbb/codes/chat_ds && docker compose build harness && docker compose up -d harness'
```

验证远端：

```bash
docker exec chat_acits_harness sh -lc '
cd /app &&
PYTHONPYCACHEPREFIX=/tmp/chatds_pycache PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  tests.test_tool_argument_and_report_quality \
  tests.test_skill_contract_parsing \
  tests.test_workspace_artifact_reconcile \
  tests.test_no_contract_fallback
'
```

## 注意事项

- 不要保存明文 root/cc 密码到 memory、repo、markdown、shell history。
- 不要 `git add -A`，repo 内有大量 runtime/session/cache/skills 数据。
- 用户倾向系统性修复 harness，不接受 GAL3 case-wise hack。
- 用户明确要求：v2.3 skill 要 robust，输出接近 ground truth md。
