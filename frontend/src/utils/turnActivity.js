import { updateAgentRuns } from './agentRunHydration.js'

const MAX_NODES = 2000

function activityIdentity(event = {}) {
  return String(event.event_id || `${event.root_run_id}:${event.seq}`)
}

function toolStatus(eventType) {
  if (eventType === 'tool.completed') return 'succeeded'
  if (eventType === 'tool.failed') return 'failed'
  return 'running'
}

function mergeLifecycleEvent(previous = {}, incoming = {}) {
  const merged = { ...previous, ...incoming }
  if (previous.payload || incoming.payload) {
    merged.payload = {
      ...(previous.payload || {}),
      ...(incoming.payload || {}),
    }
  }
  return merged
}

function compactAdjacentStreamNodes(nodes = []) {
  const compacted = []
  for (const node of nodes) {
    const previous = compacted[compacted.length - 1]
    if (
      previous
      && (node.kind === 'content' || node.kind === 'reasoning')
      && previous.kind === node.kind
    ) {
      compacted[compacted.length - 1] = {
        ...previous,
        lastSeq: Math.max(
          Number(previous.lastSeq || 0),
          Number(node.lastSeq || 0),
        ),
        eventIds: [...new Set([
          ...(previous.eventIds || []),
          ...(node.eventIds || []),
        ])].slice(-256),
        text: `${previous.text || ''}${node.text || ''}`,
        status: node.status || previous.status,
        payload: { ...(previous.payload || {}), ...(node.payload || {}) },
      }
      continue
    }
    compacted.push(node)
  }
  return compacted
}

function settleOpenToolsAtRootTerminal(nodes = []) {
  const workflow = nodes.find((node) => node.kind === 'workflow')
  const rootStatus = workflow?.status
  if (!['succeeded', 'success', 'completed', 'failed', 'cancelled'].includes(rootStatus)) {
    return nodes
  }
  const interruptedStatus = rootStatus === 'failed' ? 'failed' : 'cancelled'
  return nodes.map((node) => (
    node.kind === 'tool' && node.status === 'running'
      ? { ...node, status: interruptedStatus }
      : node
  ))
}

export function applyTurnActivity(nodes, event) {
  if (!event?.node_id || !Number.isSafeInteger(Number(event.seq))) {
    return nodes || []
  }
  const next = [...(nodes || [])]
  const identity = activityIdentity(event)
  if (next.some((node) => (node.eventIds || []).includes(identity))) {
    return nodes || []
  }
  let index = next.findIndex((node) => node.nodeId === event.node_id)
  if (index < 0) {
    next.push({
      nodeId: event.node_id,
      kind: event.kind,
      anchorSeq: Number(event.seq),
      lastSeq: 0,
      eventIds: [],
      text: '',
      runs: [],
      status: 'running',
      payload: {},
    })
    index = next.length - 1
  }
  const current = next[index]
  if (
    current.eventIds.includes(identity)
    || Number(event.seq) <= Number(current.lastSeq || 0)
  ) return nodes || []
  const payload = event.payload || {}
  const node = {
    ...current,
    kind: event.kind || current.kind,
    lastSeq: Number(event.seq),
    eventIds: [...current.eventIds, identity].slice(-256),
  }
  if (event.kind === 'content' || event.kind === 'reasoning') {
    node.text = `${current.text || ''}${payload.text || ''}`
  } else if (event.kind === 'progress') {
    node.text = String(payload.text || '')
    node.payload = { ...current.payload, ...payload }
    node.status = 'succeeded'
  } else if (event.kind === 'workflow') {
    node.runs = updateAgentRuns(current.runs || [], payload.event || {})
    const root = node.runs.find((run) => run.id === event.root_run_id)
    node.status = root?.lifecycle_status || root?.status || 'running'
  } else if (event.kind === 'tool') {
    const lifecycle = mergeLifecycleEvent(
      current.payload?.event || {},
      payload.event || {},
    )
    node.payload = { ...current.payload, event: lifecycle }
    node.status = toolStatus(lifecycle.event_type)
    // The tool card is the chronological surface, while the workflow card is
    // the aggregate. Fold the same immutable event into that aggregate instead
    // of inventing a second protocol event or a second source of truth.
    const workflowIndex = next.findIndex(
      (candidate) => candidate.kind === 'workflow'
        && candidate.nodeId === `workflow:${event.root_run_id}`,
    )
    if (workflowIndex >= 0) {
      const workflow = next[workflowIndex]
      next[workflowIndex] = {
        ...workflow,
        runs: updateAgentRuns(workflow.runs || [], lifecycle),
      }
    }
  } else if (event.kind === 'approval') {
    const merged = { ...current.payload, ...payload }
    // Preserve request_seq from the pending event if the update omits it;
    // the decision endpoint requires it and the update may not re-send it.
    if (merged.request_seq == null && current.payload.request_seq != null) {
      merged.request_seq = current.payload.request_seq
    }
    node.payload = merged
    node.status = payload.status || current.status
  }
  next[index] = node
  return settleOpenToolsAtRootTerminal(compactAdjacentStreamNodes(
    next.sort((a, b) => a.anchorSeq - b.anchorSeq),
  ))
    .slice(-MAX_NODES)
}

export function reduceTurnActivities(events = []) {
  return [...events]
    .sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0))
    .reduce(applyTurnActivity, [])
}

export function attachTurnActivities(messages = [], events = [], options = {}) {
  const byRun = new Map()
  const committed = new Set(
    events
      .filter((event) => (
        event.kind === 'projection'
        && event.payload?.status === 'committed'
      ))
      .map((event) => event.root_run_id),
  )
  for (const event of events) {
    const key = event.root_run_id
    if (!key || event.kind === 'projection') continue
    byRun.set(key, applyTurnActivity(byRun.get(key) || [], event))
  }
  return messages.map((message) => {
    const key = message.run_id || (
      message.durableRunPlaceholder ? message.rootRunId : null
    )
    const replayable = Boolean(
      key
      && byRun.has(key)
      && (committed.has(key) || message.runActive)
    )
    return (
    message.role === 'assistant' && replayable
      ? {
          ...message,
          rootRunId: key,
          activityNodes: byRun.get(key),
          activityTruncated: options.truncated === true || undefined,
        }
      : message
    )
  })
}

function messageActivityRoot(message = {}) {
  return message.run_id || message.rootRunId || null
}

export function turnActivityHighWater(messages = [], rootRunId) {
  const message = messages.find((candidate) => (
    candidate.role === 'assistant'
    && messageActivityRoot(candidate) === rootRunId
  ))
  return Math.max(
    0,
    ...(message?.activityNodes || []).map((node) => Number(node.lastSeq || 0)),
  )
}

export function mergeTurnActivities(messages = [], events = []) {
  const byRun = new Map()
  for (const event of events) {
    if (!event.root_run_id || event.kind === 'projection') continue
    const bucket = byRun.get(event.root_run_id) || []
    bucket.push(event)
    byRun.set(event.root_run_id, bucket)
  }
  return messages.map((message) => {
    const root = messageActivityRoot(message)
    if (message.role !== 'assistant' || !root || !byRun.has(root)) {
      return message
    }
    return {
      ...message,
      rootRunId: root,
      activityNodes: byRun.get(root).reduce(
        applyTurnActivity,
        message.activityNodes || [],
      ),
    }
  })
}
