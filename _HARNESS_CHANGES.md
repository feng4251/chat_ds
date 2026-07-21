---
name: harness-fixes-2026-07-07
description: Phase 0-7 通用 harness 修复核心变更（已部署）
metadata:
  node_type: memory
  type: project
  originSessionId: b9335375-0822-4c4e-b007-603a72b57b97
---

> **历史文档（2026-07-07/08）**：当前实现已经显著演进，最新状态请以 `SESSION_HANDOFF.md` 为准。

## 核心问题

原始 web harness 不能完整执行 session skill：
1. Tool 参数解析不稳健（`_raw_args` 泄漏、malformed JSON 直接送 handler）
2. Skill 工作流没有资源发现（模型盲目 web_search，不查看 skill 深层文件）
3. 生成质量难以达成（缺少 orchestrator/worker/reference 阅读，导致输出不完整）

## Phase 0: Git 基线

在 `/nfs/yangbb/codes/chat_ds` 初始化本地 git（不 push）。
- 便于 diff 审核、worktree 隔离、commit 追踪
- 避免提交凭据、大日志、tmp 文件

## Phase 1-3: Skill 发现与运行状态

**harness/agent_loop.py** 核心变更：
```python
@dataclass
class HarnessRunState:
    tool_call_count: int = 0
    tool_error_count: int = 0
    parse_failure_count: int = 0
    schema_failure_count: int = 0
    successful_write_sizes: list[int] = field(default_factory=list)
    viewed_skill_names: set[str] = field(default_factory=set)
    viewed_skill_files: set[str] = field(default_factory=set)
    viewed_skill_categories: set[str] = field(default_factory=set)
    # ...
    def record_skill_view(self, args: dict, result_data: dict|None):
        """记录 skill_view 调用的资源图信息"""
        # 提取 linked_files 和 resource_graph 的关键数据

    def needs_more_skill_workflow(self) -> tuple[bool, str]:
        """判断是否需要继续查看 skill 深层资源"""
        # 首先要求 __manifest__，然后 orchestration/workers，最后 supporting
```

**Skill workflow 完成度 gate：**
- 在 `finish_reason=="stop"` 和 tool 执行后检查
- 若请求复杂 artifact（中文标记：完整/综合/计划 + 英文：clinical/trial/phase 等），强制必要的 skill 资源查看
- 通过 `queue_skill_workflow_continuation` 追加用户角色消息，明确下一步应查看的资源
- 最多进行 4 次 continuation

**harness/skills/loader.py** 核心变更：
```python
_WORKFLOW_DIRS = [
    "orchestration", "workers", "workflows",
    "references", "templates", "formats", "protocols",
    "scripts", "examples", "evaluation", "assets"
]

def _discover_resource_graph(skill_dir):
    """扫描 skill 目录，提取资源分类和建议文件"""
    # 返回 {skill_root, categories{name:{count,sample}},
    #       important_categories, suggested_files[:40], hint}
```

**harness/tools/skills.py** 和 **harness/skills/manager.py** 核心变更：
- `skill_view(name)` → 返回 SKILL.md + resource_graph 摘要
- `skill_view(name, file_path="__manifest__")` → 返回完整资源图（linked_files, categories, suggested_files, next_steps）
- `skill_view(name, file_path="path/to/file")` → 返回该文件内容

## Phase 4-5: Tool 协议加固

**harness/tools/registry.py** 核心变更：
```python
def _strip_context_owned_args(entry, args):
    """移除 user_id/session_id/enabled_user_skills（由 harness 注入）"""
    # 防止用户侧模型意外修改上下文参数

def dispatch(tool_name, args, context):
    # 1. 移除 context-owned args
    # 2. Schema validation: required/type/additionalProperties
    # 3. 拒绝 _raw_args 和 __tool_arg_parse_error（内部保留字）
    # 4. 注入 context 参数，调用 handler

# Tool 元数据
@dataclass
class ToolEntry:
    is_read_only: bool  # 不修改系统
    is_destructive: bool  # 可能造成不可逆损失
    parallel_safe: bool  # 可与其他 tool 并发
    path_scoped: bool  # 受路径隔离约束
```

**harness/agent_loop.py** 参数解析：
```python
def _safe_parse_args(raw: str) -> dict:
    """安全解析 tool arguments"""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 尝试提取最外层 JSON object
    obj = _extract_json_object(raw)
    if obj is not None:
        return obj
    # 无法修复：返回可恢复的 tool_error 信息
    return {
        "__tool_arg_parse_error": "...",
        "__raw_args_preview": raw[:500]
    }

# Main loop
if "__tool_arg_parse_error" in args:
    run_state.parse_failure_count += 1
    # 构造 tool_error，要求模型重试
else:
    result = registry.dispatch(tc.name, args, context)
```

## Phase 6-7: 执行隔离 & 可观察性

**harness/tools/code_execution.py** 保持不变（生产 socket 隔离）：
- Unix socket: `/run/chat-ds-executor/executor.sock`
- 网络禁用、BLAS=1、RLIMIT_AS 2GB
- 已验证通过 smoke test

**Runtime state 追踪：**
- 每轮输出 HarnessRunState 摘要
- 记录：tool 统计、skill 资源查看情况、artifact 大小、continuation 原因
- 便于评估和调试

## 文件变更概览

| 文件 | 变更 |
|------|------|
| harness/agent_loop.py | HarnessRunState, skill gate, continuation, _safe_parse_args |
| harness/tools/registry.py | schema validation, _raw_args rejection, tool metadata |
| harness/skills/manager.py | __manifest__ support, resource_graph |
| harness/skills/loader.py | _discover_resource_graph, workflow dirs |
| harness/tools/skills.py | skill_view description update |
| harness/prompt/builder.py | skill usage contract |
| harness/main.py | max_tokens param plumbing |
| harness/tools/__init__.py | tool metadata registration |
| .gitignore | data/skills/harnessprobe/ |

## 部署确认

**远端版本**: ac8093fe (2026-07-07)
- 热修补方式（复用容器，不重建）
- 所有修改已编译、已验证 syntax
- executor socket 已 smoke test 通过
- 安全隔离边界保持不变

## 待验证（重启后 #146-#148）

1. 新 session 4588be7c... 是否触发了 MCP 调用（vs 只有 web_search）
2. 生成 MD 是否覆盖了 skill 的关键资源主题
3. 若差距存在，确认是 harness 能力不足还是 skill 声明缺失
