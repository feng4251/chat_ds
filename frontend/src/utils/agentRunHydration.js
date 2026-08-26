const GENERIC_AGENT_NAME = /^(?:agent|delegate|worker|child)(?:[-_ ]?\d+)?$/i
const MAX_LIVE_RUN_EVENTS = 64
const MAX_LIVE_RUN_PREVIEW_CHARS = 4000
const MAX_LIVE_EVENT_KEYS = 256
const MAX_LIVE_TOOL_ATTEMPTS = 24
const MAX_LIVE_ARTIFACTS = 24
export const LIVE_RUN_LIFECYCLE_NOTICE = (
  '当前显示的是阶段性输出；原生任务与机器交付验收仍在执行。'
  + '刷新页面不会中断后台任务。'
)

function bounded(value, limit = 128) {
  const text = String(value || '').trim()
  if (text.length <= limit) return text
  return text.slice(0, Math.max(0, limit - 1)) + '…'
}

function hasExplicitRecovery(payload = {}) {
  if (payload.recovered === true || Number(payload.recovery_count || 0) > 0) {
    return true
  }
  return [
    payload.recovery_reason,
    payload.terminal_reason,
    payload.runtime_finish_reason,
    payload.finish_reason,
    payload.runtime_warning,
  ].some((value) => /recover|salvage/i.test(String(value || '')))
}

function unresolvedRetrievalAffectsCompletionQuality(value) {
  if (value === null || value === undefined || value === false) return false
  return !(
    value
    && typeof value === 'object'
    && value.quality_impact === 'advisory'
  )
}

function authoritativeTerminal(event = {}, payload = {}) {
  if (!['run.completed', 'run.failed', 'run.cancelled'].includes(event.event_type)) {
    return false
  }
  if (typeof event.authoritative === 'boolean') {
    return event.authoritative
  }
  if (typeof payload.authoritative === 'boolean') {
    return payload.authoritative
  }
  return payload.provisional_terminal !== true
}

function eventIdentity(event = {}) {
  const explicitId = event.event_id || event.id
  if (explicitId) return `id:${explicitId}`
  if (
    event.run_id
    && event.event_type
    && event.seq !== undefined
    && event.seq !== null
  ) {
    return `${event.run_id}:${event.event_type}:${event.seq}`
  }
  return ''
}

function appendBounded(items, item, limit) {
  const next = [...(items || []), item]
  return next.slice(-limit)
}

export function semanticAgentName(event = {}, currentName = '') {
  const payload = event.payload || {}
  const directCandidates = [
    event.display_name,
    event.agent_name,
    currentName,
  ].map((value) => String(value || '').trim())
  const meaningfulDirect = directCandidates.find(
    (value) => value && !GENERIC_AGENT_NAME.test(value)
  )
  if (meaningfulDirect) return bounded(meaningfulDirect)
  for (const value of [payload.role_hint, payload.worker_id, payload.step_id]) {
    if (String(value || '').trim()) return bounded(value)
  }
  if (String(payload.goal || '').trim()) return bounded(payload.goal)
  return directCandidates.find(Boolean) || String(event.agent_kind || 'agent')
}

/**
 * Track which conversation owns one live HTTP/SSE request.
 *
 * A request that creates a new conversation briefly spans the route transition
 * from `/chat` (null) to `/chat/:id`; every other route change revokes its UI
 * ownership. The Backend may keep executing after revocation, but stale
 * callbacks must never mutate the newly selected conversation.
 */
export function createConversationRequestScope(originConversationId = null) {
  const origin = originConversationId || null
  return {
    originConversationId: origin,
    resolvedConversationId: origin,
    resolvedRouteObserved: Boolean(origin),
    acceptedRunId: null,
    cancelled: false,
  }
}

export function bindConversationRequestScope(scope, conversationId) {
  if (!scope || scope.cancelled || !conversationId) return scope
  scope.resolvedConversationId = conversationId
  return scope
}

export function observeConversationRequestRoute(scope, conversationId) {
  if (
    scope
    && !scope.cancelled
    && scope.resolvedConversationId
    && (conversationId || null) === scope.resolvedConversationId
  ) {
    scope.resolvedRouteObserved = true
  }
  return scope
}

export function recordAcceptedRunReceipt(scope, runId) {
  if (!scope || scope.cancelled || !runId) return scope
  if (!scope.acceptedRunId) scope.acceptedRunId = String(runId)
  return scope
}

export function markAcceptedLiveRun(message = {}, runId = '') {
  if (!runId) return message
  return {
    ...message,
    rootRunId: String(runId),
    runActive: true,
    lifecycleNotice: LIVE_RUN_LIFECYCLE_NOTICE,
  }
}

export function settleAcceptedLiveRun(message = {}) {
  return {
    ...message,
    runActive: false,
    lifecycleNotice: undefined,
  }
}

export function conversationRequestWasAccepted(scope) {
  return Boolean(scope?.acceptedRunId)
}

export function conversationRequestOwnsRoute(scope, conversationId) {
  if (!scope || scope.cancelled) return false
  const current = conversationId || null
  if (scope.resolvedRouteObserved) {
    return current === scope.resolvedConversationId
  }
  if (scope.resolvedConversationId) {
    return (
      current === scope.originConversationId
      || current === scope.resolvedConversationId
    )
  }
  return current === scope.originConversationId
}

function lifecycleStatus(eventType, payload, prior = 'running') {
  if (eventType === 'run.failed') return 'failed'
  if (eventType === 'run.cancelled') return 'cancelled'
  if (eventType !== 'run.completed') return prior
  if (
    String(payload.completion_quality || '').toLowerCase() === 'degraded'
    || unresolvedRetrievalAffectsCompletionQuality(
      payload.unresolved_retrieval,
    )
  ) {
    return 'degraded'
  }
  return hasExplicitRecovery(payload) ? 'recovered' : 'succeeded'
}

export function updateAgentRuns(runs, event) {
  if (!event?.run_id) return runs || []
  const payload = (
    event.payload && typeof event.payload === 'object'
      ? event.payload
      : {}
  )
  const id = event.run_id
  const next = [...(runs || [])]
  let idx = next.findIndex((run) => run.id === id)
  if (idx < 0) {
    next.push({
      id,
      parent_run_id: event.parent_run_id || null,
      root_run_id: event.root_run_id || id,
      agent_kind: event.agent_kind || 'agent',
      agent_name: event.agent_name || event.agent_kind || 'agent',
      display_name: semanticAgentName(event),
      depth: event.depth || 0,
      workspace_scope: event.workspace_scope || 'shared_session',
      status: 'running',
      lifecycle_status: 'running',
      preview: '',
      tools: [],
      artifacts: [],
      tool_attempt_count: 0,
      tool_attempts_truncated: false,
      artifact_count: 0,
      artifacts_truncated: false,
      verifier: null,
      usage: null,
      events: [],
      _seen_event_keys: [],
      _event_seq_high_water: {},
    })
    idx = next.length - 1
  }
  const sequence = Number(event.seq)
  const hasSequence = (
    Boolean(event.event_type)
    &&
    event.seq !== undefined
    && event.seq !== null
    && Number.isSafeInteger(sequence)
    && sequence >= 0
  )
  const priorHighWater = next[idx]._event_seq_high_water || {}
  if (
    hasSequence
    && priorHighWater[event.event_type] !== undefined
    && sequence <= priorHighWater[event.event_type]
  ) {
    return runs || []
  }
  const identity = eventIdentity(event)
  const priorSeenEventKeys = next[idx]._seen_event_keys || []
  if (!hasSequence && identity && priorSeenEventKeys.includes(identity)) {
    return runs || []
  }
  const priorEvents = next[idx].events || []
  const retainEvent = ![
    'agent.delta',
    'agent.reasoning_delta',
  ].includes(event.event_type)
  const run = {
    ...next[idx],
    _seen_event_keys: !hasSequence && identity
      ? appendBounded(
        priorSeenEventKeys,
        identity,
        MAX_LIVE_EVENT_KEYS,
      )
      : priorSeenEventKeys,
    _event_seq_high_water: hasSequence
      ? {
        ...priorHighWater,
        [event.event_type]: sequence,
      }
      : priorHighWater,
    events: retainEvent
      ? [...priorEvents, event].slice(-MAX_LIVE_RUN_EVENTS)
      : priorEvents,
  }
  run.parent_run_id = event.parent_run_id || run.parent_run_id
  run.root_run_id = event.root_run_id || run.root_run_id
  run.agent_kind = event.agent_kind || run.agent_kind
  run.agent_name = event.agent_name || run.agent_name
  run.display_name = semanticAgentName(event, run.display_name || run.agent_name)
  run.depth = event.depth ?? run.depth
  run.workspace_scope = event.workspace_scope || run.workspace_scope
  for (const key of [
    'worker_id',
    'workflow_stage',
    'step_type',
    'step_id',
    'delegation_batch_id',
    'delegation_slot',
    'delegation_batch_size',
  ]) {
    if (payload[key] !== undefined && payload[key] !== null && payload[key] !== '') {
      run[key] = payload[key]
    }
  }
  if (
    (event.event_type === 'agent.spawned' || event.event_type === 'run.started')
    && !run.authoritative_terminal
  ) {
    run.status = 'running'
    run.lifecycle_status = 'running'
  }
  if (event.event_type === 'agent.delta') {
    run.preview = (
      (run.preview || '') + (payload.content || '')
    ).slice(-MAX_LIVE_RUN_PREVIEW_CHARS)
  }
  if (event.event_type === 'tool.started') {
    const toolName = payload.tool_name || event.tool_name || 'tool'
    const toolCallId = (
      payload.tool_call_id
      || event.tool_call_id
      || `${toolName}:${event.seq ?? run.tools.length}`
    )
    const tools = [...run.tools]
    const index = tools.findIndex((tool) => tool.tool_call_id === toolCallId)
    if (index >= 0) {
      tools[index] = (
        tools[index].status === 'running'
          ? tools[index]
          : { ...tools[index], terminal_conflict: true }
      )
    } else {
      const attemptCount = Number(
        run.tool_attempt_count ?? tools.length
      ) + 1
      const attemptIndex = tools
        .filter((tool) => tool.name === toolName)
        .reduce(
          (maximum, tool) => Math.max(
            maximum,
            Number(tool.attempt_index || 0),
          ),
          0,
        ) + 1
      tools.push({
        name: toolName,
        tool_call_id: toolCallId,
        attempt_index: attemptIndex,
        status: 'running',
        later_success_same_tool: false,
      })
      run.tool_attempt_count = attemptCount
    }
    run.tools = tools.slice(-MAX_LIVE_TOOL_ATTEMPTS)
    run.tool_attempts_truncated = (
      Boolean(run.tool_attempts_truncated)
      || Number(run.tool_attempt_count || 0) > run.tools.length
    )
  }
  if (event.event_type === 'tool.completed' || event.event_type === 'tool.failed') {
    const toolName = payload.tool_name || event.tool_name || 'tool'
    const toolCallId = payload.tool_call_id || event.tool_call_id || ''
    const tools = [...run.tools]
    let index = toolCallId
      ? tools.findIndex((tool) => tool.tool_call_id === toolCallId)
      : -1
    if (index < 0) {
      for (let candidate = tools.length - 1; candidate >= 0; candidate -= 1) {
        if (tools[candidate].name === toolName && tools[candidate].status === 'running') {
          index = candidate
          break
        }
      }
    }
    if (index < 0) {
      const attemptCount = Number(
        run.tool_attempt_count ?? tools.length
      ) + 1
      const attemptIndex = tools
        .filter((tool) => tool.name === toolName)
        .reduce(
          (maximum, tool) => Math.max(
            maximum,
            Number(tool.attempt_index || 0),
          ),
          0,
        ) + 1
      tools.push({
        name: toolName,
        tool_call_id: toolCallId || `${toolName}:${event.seq ?? tools.length}`,
        attempt_index: attemptIndex,
        status: 'running',
        later_success_same_tool: false,
      })
      run.tool_attempt_count = attemptCount
      index = tools.length - 1
    }
    const previous = tools[index]
    const rejected = (
      payload.actual_dispatch_attempted === false
      || payload.actual_dispatch === false
      || ['rejected', 'preflight_rejected', 'not_dispatched'].includes(
        String(payload.outcome || '').toLowerCase()
      )
    )
    const status = event.event_type === 'tool.completed'
      ? 'success'
      : (rejected ? 'rejected' : 'failed')
    const acceptedTerminal = previous.status === 'running'
    if (acceptedTerminal) {
      tools[index] = {
        ...previous,
        name: toolName,
        status,
        actual_dispatch_attempted: payload.actual_dispatch_attempted,
        detail: (
          event.event_type === 'tool.failed'
            ? bounded(
              payload.detail || payload.error || payload.reason || '',
              1000,
            )
            : previous.detail || ''
        ),
      }
    } else {
      // A call ID identifies one immutable attempt. Keep its first terminal
      // and surface any later disagreement without rewriting history.
      tools[index] = {
        ...previous,
        terminal_conflict: true,
      }
    }
    if (event.event_type === 'tool.completed' && acceptedTerminal) {
      for (let candidate = 0; candidate < tools.length; candidate += 1) {
        if (
          candidate !== index
          && tools[candidate].name === toolName
          && ['failed', 'rejected'].includes(tools[candidate].status)
        ) {
          tools[candidate] = {
            ...tools[candidate],
            later_success_same_tool: true,
          }
        }
      }
    }
    run.tools = tools.slice(-MAX_LIVE_TOOL_ATTEMPTS)
    run.tool_attempts_truncated = (
      Boolean(run.tool_attempts_truncated)
      || Number(run.tool_attempt_count || 0) > run.tools.length
    )
  }
  if (event.event_type === 'usage.updated') run.usage = payload
  if (event.event_type === 'artifact.created') {
    const artifact = {
      id: payload.artifact_id || payload.id || null,
      title: bounded(payload.title || payload.path || 'artifact', 256),
      path: bounded(payload.path || '', 1024),
      kind: bounded(payload.kind || 'file', 32),
      size_bytes: payload.size_bytes || payload.size || 0,
    }
    run.artifact_count = Number(
      run.artifact_count || (run.artifacts || []).length
    ) + 1
    run.artifacts = appendBounded(
      run.artifacts,
      artifact,
      MAX_LIVE_ARTIFACTS,
    )
    run.artifacts_truncated = (
      Boolean(run.artifacts_truncated)
      || run.artifact_count > run.artifacts.length
    )
  }
  if (event.event_type === 'verifier.completed' || event.event_type === 'verifier.failed') {
    run.verifier = {
      status: event.event_type === 'verifier.failed'
        ? 'failed'
        : (payload.verdict || 'inconclusive'),
      reason: bounded(payload.reason || payload.error || '', 1000),
    }
  }
  if (['run.completed', 'run.failed', 'run.cancelled'].includes(event.event_type)) {
    if (!authoritativeTerminal(event, payload)) {
      run.provisional_terminal_observed = true
    } else if (run.authoritative_terminal) {
      run.terminal_conflict = true
    } else {
      run.authoritative_terminal = {
        event_type: event.event_type,
        seq: event.seq ?? null,
      }
      if (event.event_type === 'run.completed') {
        run.status = 'succeeded'
        run.lifecycle_status = lifecycleStatus(
          event.event_type,
          payload,
          run.lifecycle_status,
        )
        run.completion_quality = payload.completion_quality || null
        run.recovered = hasExplicitRecovery(payload)
        run.recovery_reason = (
          payload.recovery_reason
          || payload.terminal_reason
          || payload.runtime_finish_reason
          || null
        )
        run.status_reason = (
          payload.terminal_reason || payload.finish_reason || null
        )
        run.usage = payload.usage || run.usage
      } else if (event.event_type === 'run.failed') {
        run.status = 'failed'
        run.lifecycle_status = 'failed'
        run.error = bounded(payload.error || 'Unknown error', 2000)
        run.failure_class = payload.failure_class || null
        run.retryable = (
          typeof payload.retryable === 'boolean'
            ? payload.retryable
            : null
        )
        run.status_reason = (
          payload.terminal_reason || payload.finish_reason || null
        )
      } else {
        run.status = 'cancelled'
        run.lifecycle_status = 'cancelled'
        run.error = null
        run.cancellation_source = payload.cancellation_source || null
        run.status_reason = (
          payload.terminal_reason
          || payload.cancellation_reason
          || payload.cancellation_source
          || payload.finish_reason
          || null
        )
      }
    }
  }
  next[idx] = run
  return next
}

export function runStatusPresentation(run = {}) {
  const status = run.lifecycle_status || run.status || 'running'
  const presentations = {
    running: { label: '运行中', tone: 'text-amber-600' },
    succeeded: { label: '已完成', tone: 'text-green-600' },
    recovered: { label: '恢复后完成', tone: 'text-sky-600' },
    degraded: { label: '降级完成', tone: 'text-amber-700' },
    failed: { label: '失败', tone: 'text-red-600' },
    cancelled: { label: '已取消', tone: 'text-slate-500' },
  }
  return presentations[status] || {
    label: status,
    tone: 'text-slate-500',
  }
}

export function toolStatusPresentation(status = '') {
  const labels = {
    running: '运行中',
    success: '成功',
    recovered: '已明确恢复',
    rejected: '调用前已拒绝',
    failed: '失败',
  }
  return labels[status] || status
}

function lifecycleNotice(root) {
  if (root.active) return '任务仍在后台执行；以下状态已持久化，刷新页面不会丢失。'
  const labels = {
    succeeded: '任务已完成；以下为持久化的执行记录。',
    recovered: '任务在自动恢复后完成；以下为持久化的执行记录。',
    degraded: '任务以降级状态完成；以下为持久化的执行记录。',
    failed: '任务执行失败；以下为持久化的执行记录。',
    cancelled: '任务已取消；以下为持久化的执行记录。',
  }
  return labels[root.status] || '以下为持久化的执行记录。'
}

function projectionTruncationKeys(payload = {}) {
  const projection = payload.projection_truncated
  if (!projection || typeof projection !== 'object') return []
  return Object.entries(projection)
    .filter(([, truncated]) => truncated === true)
    .map(([key]) => key)
}

function durablePlaceholder(root, projectionTruncated = []) {
  return {
    id: `durable-run-${root.root_run_id}`,
    role: 'assistant',
    content: '',
    reasoning: '',
    streaming: false,
    runActive: Boolean(root.active),
    runStatus: root.status,
    lifecycleNotice: lifecycleNotice(root),
    durableRunPlaceholder: true,
    rootRunId: root.root_run_id,
    agentRuns: root.runs || [],
    projectionTruncated: projectionTruncated.length
      ? projectionTruncated
      : undefined,
  }
}

/**
 * Attach a persisted run tree only to a Backend-proven assistant turn.
 *
 * If the Backend cannot prove a unique assistant message, a lifecycle-only
 * placeholder is inserted after the exact trigger (or appended when even that
 * mapping is unavailable). This deliberately avoids timestamp-nearest guesses.
 */
export function hydrateAgentRunCards(messages = [], payload = {}) {
  const priorPlaceholders = new Map(
    messages
      .filter((message) => (
        message.durableRunPlaceholder
        && message.rootRunId
      ))
      .map((message) => [message.rootRunId, message]),
  )
  const next = messages
    .filter((message) => !message.durableRunPlaceholder)
    .map((message) => ({ ...message }))
  const roots = [...(payload.roots || [])].reverse()
  const projectionTruncated = projectionTruncationKeys(payload)
  const deferred = []

  for (const root of roots) {
    let assistantIndex = root.assistant_message_id
      ? next.findIndex(
        (message) => message.id === root.assistant_message_id && message.role === 'assistant'
      )
      : -1
    if (assistantIndex < 0) {
      assistantIndex = next.findIndex(
        (message) => (
          message.role === 'assistant'
          && message.rootRunId === root.root_run_id
        )
      )
    }
    if (assistantIndex >= 0) {
      const existing = next[assistantIndex].agentRuns || []
      const rootIds = new Set((root.runs || []).map((run) => run.id))
      next[assistantIndex] = {
        ...next[assistantIndex],
        agentRuns: [
          ...existing.filter((run) => !rootIds.has(run.id)),
          ...(root.runs || []),
        ],
        rootRunId: root.root_run_id,
        runActive: Boolean(root.active),
        runStatus: root.status,
        lifecycleNotice: (
          root.active || root.status !== 'succeeded'
            ? lifecycleNotice(root)
            : undefined
        ),
        projectionTruncated: projectionTruncated.length
          ? projectionTruncated
          : undefined,
      }
      continue
    }

    // A normal success without a Backend-proven assistant mapping remains
    // available in the Tasks/run audit projection. It must not manufacture an
    // empty chat turn whose only content says that a task completed. Active or
    // exceptional terminals still need an in-conversation lifecycle marker.
    if (!root.active && root.status === 'succeeded') continue

    const priorPlaceholder = priorPlaceholders.get(root.root_run_id)
    const placeholder = {
      ...durablePlaceholder(root, projectionTruncated),
      ...(
        priorPlaceholder?.activityNodes
          ? { activityNodes: priorPlaceholder.activityNodes }
          : {}
      ),
      ...(
        priorPlaceholder?.activityTruncated
          ? { activityTruncated: true }
          : {}
      ),
    }
    const triggerIndex = root.trigger_message_id
      ? next.findIndex(
        (message) => message.id === root.trigger_message_id && message.role === 'user'
      )
      : -1
    if (triggerIndex >= 0 && root.mapping_status === 'exact_no_assistant') {
      next.splice(triggerIndex + 1, 0, placeholder)
    } else {
      deferred.push(placeholder)
    }
  }
  return [...next, ...deferred]
}
