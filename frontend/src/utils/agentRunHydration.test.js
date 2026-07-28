import assert from 'node:assert/strict'
import test from 'node:test'

import {
  bindConversationRequestScope,
  conversationRequestOwnsRoute,
  conversationRequestWasAccepted,
  createConversationRequestScope,
  hydrateAgentRunCards,
  observeConversationRequestRoute,
  recordAcceptedRunReceipt,
  runStatusPresentation,
  semanticAgentName,
  updateAgentRuns,
} from './agentRunHydration.js'

test('semantic delegate name prefers persisted workflow identity over slot name', () => {
  assert.equal(
    semanticAgentName({
      agent_name: 'delegate-2',
      agent_kind: 'delegate',
      payload: {
        worker_id: 'worker-safety-extraction',
        goal: 'Extract safety evidence',
      },
    }),
    'worker-safety-extraction',
  )
})

test('refresh hydration attaches only an exact Backend-mapped assistant', () => {
  const messages = [
    { id: 'user-1', role: 'user', content: 'do work' },
    { id: 'assistant-1', role: 'assistant', content: 'done' },
  ]
  const hydrated = hydrateAgentRunCards(messages, {
    roots: [{
      root_run_id: 'root',
      assistant_message_id: 'assistant-1',
      trigger_message_id: 'user-1',
      mapping_status: 'exact',
      active: false,
      status: 'succeeded',
      runs: [{ id: 'child', agent_kind: 'delegate' }],
    }],
  })
  assert.equal(hydrated.length, 2)
  assert.deepEqual(hydrated[1].agentRuns.map((run) => run.id), ['child'])
  assert.equal(hydrated.some((message) => message.durableRunPlaceholder), false)
})

test('active exact turn without assistant receives durable placeholder', () => {
  const hydrated = hydrateAgentRunCards(
    [{ id: 'user-1', role: 'user', content: 'do work' }],
    {
      roots: [{
        root_run_id: 'root',
        assistant_message_id: null,
        trigger_message_id: 'user-1',
        mapping_status: 'exact_no_assistant',
        active: true,
        status: 'running',
        runs: [{ id: 'child', lifecycle_status: 'running' }],
      }],
    },
  )
  assert.equal(hydrated.length, 2)
  assert.equal(hydrated[1].durableRunPlaceholder, true)
  assert.equal(hydrated[1].runActive, true)
  assert.match(hydrated[1].lifecycleNotice, /刷新页面不会丢失/)
})

test('live SSE root identity safely reuses the local partial assistant', () => {
  const hydrated = hydrateAgentRunCards(
    [
      { id: 'local-user', role: 'user', content: 'do work' },
      {
        id: 'local-assistant',
        role: 'assistant',
        content: 'partial output',
        rootRunId: 'root',
      },
    ],
    {
      roots: [{
        root_run_id: 'root',
        assistant_message_id: null,
        trigger_message_id: 'server-user',
        mapping_status: 'exact_no_assistant',
        active: true,
        status: 'running',
        runs: [{ id: 'child', lifecycle_status: 'running' }],
      }],
    },
  )
  assert.equal(hydrated.length, 2)
  assert.equal(hydrated[1].content, 'partial output')
  assert.deepEqual(hydrated[1].agentRuns.map((run) => run.id), ['child'])
  assert.equal(hydrated[1].runActive, true)
  assert.match(hydrated[1].lifecycleNotice, /后台执行/)
})

test('terminal hydration clears a prior active lifecycle notice', () => {
  const hydrated = hydrateAgentRunCards(
    [{
      id: 'local-assistant',
      role: 'assistant',
      content: 'partial output',
      rootRunId: 'root',
      runActive: true,
      lifecycleNotice: 'stale active notice',
    }],
    {
      roots: [{
        root_run_id: 'root',
        assistant_message_id: null,
        trigger_message_id: 'server-user',
        mapping_status: 'exact_no_assistant',
        active: false,
        status: 'succeeded',
        runs: [{ id: 'child', lifecycle_status: 'succeeded' }],
      }],
    },
  )
  assert.equal(hydrated[0].runActive, false)
  assert.equal(hydrated[0].lifecycleNotice, undefined)
})

test('ambiguous assistant mapping is never guessed onto an existing response', () => {
  const hydrated = hydrateAgentRunCards(
    [
      { id: 'user-1', role: 'user', content: 'do work' },
      { id: 'assistant-a', role: 'assistant', content: 'a' },
      { id: 'assistant-b', role: 'assistant', content: 'b' },
    ],
    {
      roots: [{
        root_run_id: 'root',
        assistant_message_id: null,
        trigger_message_id: 'user-1',
        mapping_status: 'ambiguous_assistant',
        active: false,
        status: 'failed',
        runs: [{ id: 'child', lifecycle_status: 'failed' }],
      }],
    },
  )
  assert.equal(hydrated[1].agentRuns, undefined)
  assert.equal(hydrated[2].agentRuns, undefined)
  assert.equal(hydrated[3].durableRunPlaceholder, true)
})

test('live lifecycle keeps failed attempts distinct from later same-tool success', () => {
  let runs = updateAgentRuns([], {
    run_id: 'child',
    agent_name: 'delegate-1',
    agent_kind: 'delegate',
    event_type: 'agent.spawned',
    payload: {
      step_id: 'literature-review',
      delegation_batch_id: 'batch-1',
      delegation_slot: 1,
      delegation_batch_size: 3,
    },
  })
  runs = updateAgentRuns(runs, {
    run_id: 'child',
    agent_name: 'delegate-1',
    event_type: 'run.started',
    payload: {},
  })
  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'tool.failed',
    payload: { tool_name: 'web_search', error: 'timeout' },
  })
  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'tool.completed',
    payload: { tool_name: 'web_search' },
  })
  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'run.completed',
    payload: { completion_quality: 'degraded' },
  })
  assert.equal(runs[0].display_name, 'literature-review')
  assert.equal(runs[0].delegation_slot, 1)
  assert.equal(runs[0].delegation_batch_size, 3)
  assert.deepEqual(
    runs[0].tools.map((tool) => tool.status),
    ['failed', 'success'],
  )
  assert.equal(runs[0].tools[0].later_success_same_tool, true)
  assert.equal(runStatusPresentation(runs[0]).label, '降级完成')
})

test('new-conversation request owns only its bounded route transition', () => {
  const scope = createConversationRequestScope(null)
  assert.equal(conversationRequestOwnsRoute(scope, null), true)

  bindConversationRequestScope(scope, 'conversation-a')
  assert.equal(conversationRequestOwnsRoute(scope, null), true)
  assert.equal(conversationRequestOwnsRoute(scope, 'conversation-a'), true)
  assert.equal(conversationRequestOwnsRoute(scope, 'conversation-b'), false)

  observeConversationRequestRoute(scope, 'conversation-a')
  assert.equal(conversationRequestOwnsRoute(scope, 'conversation-a'), true)
  assert.equal(conversationRequestOwnsRoute(scope, null), false)

  scope.cancelled = true
  assert.equal(conversationRequestOwnsRoute(scope, 'conversation-a'), false)
})

test('conversation identity is not a task-acceptance receipt', () => {
  const scope = createConversationRequestScope('conversation-a')
  assert.equal(scope.resolvedConversationId, 'conversation-a')
  assert.equal(conversationRequestWasAccepted(scope), false)

  bindConversationRequestScope(scope, 'conversation-a')
  assert.equal(conversationRequestWasAccepted(scope), false)

  recordAcceptedRunReceipt(scope, 'root-run-1')
  assert.equal(conversationRequestWasAccepted(scope), true)
  assert.equal(scope.acceptedRunId, 'root-run-1')

  // The first receipt owns the request; a corrupt later identity cannot rekey
  // an already accepted turn in live UI state.
  recordAcceptedRunReceipt(scope, 'root-run-conflict')
  assert.equal(scope.acceptedRunId, 'root-run-1')
})

test('live reducer ignores provisional terminals and freezes the first authoritative terminal', () => {
  let runs = updateAgentRuns([], {
    run_id: 'child',
    event_type: 'run.failed',
    seq: 1,
    authoritative: false,
    payload: {
      authoritative: true,
      error: 'provisional failure',
    },
  })
  assert.equal(runs[0].lifecycle_status, 'running')
  assert.equal(runs[0].provisional_terminal_observed, true)

  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'run.completed',
    seq: 2,
    payload: {
      authoritative: true,
      completion_quality: 'degraded',
      terminal_reason: 'first-terminal',
    },
  })
  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'run.failed',
    seq: 3,
    payload: {
      authoritative: true,
      error: 'late conflict',
    },
  })
  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'run.started',
    seq: 4,
    payload: {},
  })

  assert.equal(runs[0].lifecycle_status, 'degraded')
  assert.equal(runs[0].status, 'succeeded')
  assert.equal(runs[0].status_reason, 'first-terminal')
  assert.equal(runs[0].terminal_conflict, true)
  assert.equal(runs[0].error, undefined)
})

test('live reducer is idempotent and tool call first terminal never flips', () => {
  const delta = {
    run_id: 'child',
    event_type: 'agent.delta',
    seq: 1,
    payload: { content: 'one' },
  }
  let runs = updateAgentRuns([], delta)
  runs = updateAgentRuns(runs, delta)
  assert.equal(runs[0].preview, 'one')

  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'tool.started',
    seq: 2,
    payload: {
      tool_name: 'web_search',
      tool_call_id: 'call-1',
    },
  })
  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'tool.failed',
    seq: 3,
    payload: {
      tool_name: 'web_search',
      tool_call_id: 'call-1',
      error: 'timeout',
    },
  })
  runs = updateAgentRuns(runs, {
    run_id: 'child',
    event_type: 'tool.completed',
    seq: 4,
    payload: {
      tool_name: 'web_search',
      tool_call_id: 'call-1',
    },
  })
  assert.equal(runs[0].tools.length, 1)
  assert.equal(runs[0].tools[0].status, 'failed')
  assert.equal(runs[0].tools[0].terminal_conflict, true)
  assert.equal(runs[0].tools[0].later_success_same_tool, false)

  const artifact = {
    run_id: 'child',
    event_type: 'artifact.created',
    seq: 5,
    payload: {
      artifact_id: 'artifact-1',
      path: 'report.md',
    },
  }
  runs = updateAgentRuns(runs, artifact)
  runs = updateAgentRuns(runs, artifact)
  assert.equal(runs[0].artifacts.length, 1)
  assert.equal(runs[0].artifact_count, 1)
})

test('live tool and artifact projections expose bounded counts', () => {
  let runs = []
  for (let index = 0; index < 40; index += 1) {
    runs = updateAgentRuns(runs, {
      run_id: 'child',
      event_type: 'tool.completed',
      seq: index,
      payload: {
        tool_name: `tool-${index}`,
        tool_call_id: `call-${index}`,
      },
    })
    runs = updateAgentRuns(runs, {
      run_id: 'child',
      event_type: 'artifact.created',
      seq: 100 + index,
      payload: {
        artifact_id: `artifact-${index}`,
        path: `artifact-${index}.md`,
      },
    })
  }
  assert.equal(runs[0].tool_attempt_count, 40)
  assert.equal(runs[0].tools.length, 24)
  assert.equal(runs[0].tool_attempts_truncated, true)
  assert.equal(runs[0].artifact_count, 40)
  assert.equal(runs[0].artifacts.length, 24)
  assert.equal(runs[0].artifacts_truncated, true)
})

test('refresh hydration exposes defensive projection truncation', () => {
  const hydrated = hydrateAgentRunCards(
    [{ id: 'assistant-1', role: 'assistant', content: 'done' }],
    {
      projection_truncated: {
        events: true,
        artifacts: true,
        runs: false,
        run_dtos: true,
      },
      roots: [{
        root_run_id: 'root',
        assistant_message_id: 'assistant-1',
        mapping_status: 'exact',
        active: false,
        status: 'succeeded',
        runs: [{
          id: 'root',
          lifecycle_status: 'succeeded',
          dto_truncated: true,
          dto_truncated_fields: ['error', 'tool_events'],
          error_source_chars: 9000,
          error_truncated: true,
          tool_event_count: null,
          tool_events_truncated: true,
        }],
      }],
    },
  )
  assert.deepEqual(
    hydrated[0].projectionTruncated,
    ['events', 'artifacts', 'run_dtos'],
  )
  assert.equal(hydrated[0].agentRuns[0].dto_truncated, true)
  assert.deepEqual(
    hydrated[0].agentRuns[0].dto_truncated_fields,
    ['error', 'tool_events'],
  )
})

test('live delta state is bounded and does not retain redundant delta events', () => {
  let runs = updateAgentRuns([], {
    run_id: 'child',
    event_type: 'agent.spawned',
    payload: {},
  })
  for (let index = 0; index < 80; index += 1) {
    runs = updateAgentRuns(runs, {
      run_id: 'child',
      event_type: 'agent.delta',
      seq: index + 1,
      payload: { content: 'x'.repeat(100) },
    })
  }
  assert.equal(runs[0].events.length, 1)
  assert.equal(runs[0].events[0].event_type, 'agent.spawned')
  assert.equal(runs[0].preview.length, 4000)

  for (let index = 0; index < 80; index += 1) {
    runs = updateAgentRuns(runs, {
      run_id: 'child',
      event_type: 'tool.failed',
      seq: index + 100,
      payload: {
        tool_name: `tool-${index}`,
        error: 'bounded fixture failure',
      },
    })
  }
  assert.equal(runs[0].events.length, 64)
})
