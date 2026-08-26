import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyTurnActivity,
  attachTurnActivities,
  mergeTurnActivities,
  reduceTurnActivities,
  turnActivityHighWater,
} from './turnActivity.js'

function event(seq, nodeId, kind, payload, run = 'root') {
  return {
    event_id: `event-${seq}`,
    root_run_id: 'root',
    run_id: run,
    seq,
    node_id: nodeId,
    kind,
    operation: kind === 'content' || kind === 'reasoning' ? 'append' : 'merge',
    payload,
  }
}

test('mixed thinking, text, tool and text retain their true anchor order', () => {
  const nodes = reduceTurnActivities([
    event(1, 'reasoning:1', 'reasoning', { text: 'think' }),
    event(2, 'content:2', 'content', { text: 'draft' }),
    event(3, 'tool:a', 'tool', { event: { event_type: 'tool.started', tool_name: 'Lookup', tool_call_id: 'a' } }),
    event(4, 'tool:a', 'tool', { event: { event_type: 'tool.completed', tool_name: 'Lookup', tool_call_id: 'a' } }),
    event(5, 'content:3', 'content', { text: 'result' }),
  ])
  assert.deepEqual(nodes.map((node) => node.kind), [
    'reasoning', 'content', 'tool', 'content',
  ])
  assert.equal(nodes[2].status, 'succeeded')
})

test('duplicate replay is idempotent and contiguous chunks merge in place', () => {
  const first = event(1, 'content:1', 'content', { text: 'hello ' })
  const second = event(2, 'content:1', 'content', { text: 'world' })
  let nodes = applyTurnActivity([], first)
  nodes = applyTurnActivity(nodes, first)
  nodes = applyTurnActivity(nodes, second)
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].text, 'hello world')
})

test('truncated reconciliation tail never deletes known earlier activity', () => {
  const priorNodes = reduceTurnActivities([
    event(1, 'reasoning:warehouse', 'reasoning', {
      text: 'Inspect historical receipts. ',
    }),
    event(2, 'tool:inventory', 'tool', {
      event: {
        event_type: 'tool.started',
        tool_name: 'InventoryLookup',
        tool_call_id: 'inventory',
      },
    }),
  ])
  const attached = attachTurnActivities([{
    role: 'assistant',
    rootRunId: 'root',
    durableRunPlaceholder: true,
    runActive: true,
    activityNodes: priorNodes,
  }], [
    event(5_001, 'tool:inventory', 'tool', {
      event: {
        event_type: 'tool.completed',
        tool_name: 'InventoryLookup',
        tool_call_id: 'inventory',
      },
    }),
    event(5_002, 'content:warehouse', 'content', {
      text: 'Inventory reconciled.',
    }),
  ], { truncated: true })

  assert.deepEqual(attached[0].activityNodes.map((node) => node.kind), [
    'reasoning', 'tool', 'content',
  ])
  assert.equal(attached[0].activityNodes[1].status, 'succeeded')
  assert.equal(attached[0].activityTruncated, true)
})

test('historical adjacent stream fragments compact without crossing a tool boundary', () => {
  const nodes = reduceTurnActivities([
    event(1, 'reasoning:legacy-1', 'reasoning', { text: 'inspect ' }),
    event(2, 'reasoning:legacy-2', 'reasoning', { text: 'museum ' }),
    event(3, 'reasoning:legacy-3', 'reasoning', { text: 'receipts' }),
    event(4, 'tool:catalog', 'tool', {
      event: {
        event_type: 'tool.started',
        tool_name: 'RenamedCatalogLookup',
        tool_call_id: 'catalog',
      },
    }),
    event(5, 'reasoning:legacy-4', 'reasoning', { text: 'summarize' }),
  ])
  assert.deepEqual(nodes.map((node) => node.kind), [
    'reasoning', 'tool', 'reasoning',
  ])
  assert.equal(nodes[0].text, 'inspect museum receipts')
  assert.equal(nodes[2].text, 'summarize')

  const duplicate = applyTurnActivity(nodes, event(
    2,
    'reasoning:legacy-2',
    'reasoning',
    { text: 'museum ' },
  ))
  assert.equal(duplicate[0].text, 'inspect museum receipts')
})

test('workflow node preserves semantic renamed child and terminal status', () => {
  const nodes = reduceTurnActivities([
    event(1, 'workflow:root', 'workflow', {
      event: {
        event_type: 'agent.spawned', run_id: 'child', root_run_id: 'root',
        parent_run_id: 'root', agent_name: 'delegate-1', depth: 1,
        payload: { worker_id: 'cross-domain-evidence-review' },
      },
    }, 'child'),
    event(2, 'workflow:root', 'workflow', {
      event: {
        event_type: 'run.completed', run_id: 'child', root_run_id: 'root',
        parent_run_id: 'root', depth: 1, payload: { authoritative: true },
      },
    }, 'child'),
  ])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].runs[0].display_name, 'cross-domain-evidence-review')
  assert.equal(nodes[0].runs[0].status, 'succeeded')
})

test('one tool lifecycle updates both chronological card and child aggregate', () => {
  const nodes = reduceTurnActivities([
    event(1, 'workflow:root', 'workflow', {
      event: {
        event_type: 'agent.spawned', run_id: 'child', root_run_id: 'root',
        parent_run_id: 'root', agent_name: 'evidence-worker', depth: 1, seq: 1,
        payload: {},
      },
    }, 'child'),
    event(2, 'tool:x', 'tool', {
      event: {
        event_type: 'tool.started', run_id: 'child', root_run_id: 'root',
        tool_name: 'RenamedLookup', tool_call_id: 'x', seq: 2, payload: {},
      },
    }, 'child'),
    event(3, 'tool:x', 'tool', {
      event: {
        event_type: 'tool.completed', run_id: 'child', root_run_id: 'root',
        tool_name: 'RenamedLookup', tool_call_id: 'x', seq: 3, payload: {},
      },
    }, 'child'),
  ])
  assert.equal(nodes[0].kind, 'workflow')
  assert.equal(nodes[0].runs[0].tools[0].name, 'RenamedLookup')
  assert.equal(nodes[0].runs[0].tools[0].status, 'success')
  assert.equal(nodes[1].kind, 'tool')
  assert.equal(nodes[1].status, 'succeeded')
})

test('tool completion replaces the running card and retains start metadata', () => {
  const nodes = reduceTurnActivities([
    event(1, 'tool:stable', 'tool', {
      event: {
        event_type: 'tool.started',
        tool_name: 'RenamedWarehouseLookup',
        tool_call_id: 'stable-call',
        payload: { detail: 'running' },
      },
    }),
    event(2, 'tool:stable', 'tool', {
      event: {
        event_type: 'tool.progress',
        tool_name: 'RenamedWarehouseLookup',
        tool_call_id: 'stable-call',
        payload: { detail: 'checkpoint' },
      },
    }),
    event(3, 'tool:stable', 'tool', {
      event: {
        event_type: 'tool.completed',
        payload: { detail: 'complete' },
      },
    }),
  ])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].status, 'succeeded')
  assert.equal(nodes[0].payload.event.tool_name, 'RenamedWarehouseLookup')
  assert.equal(nodes[0].payload.event.tool_call_id, 'stable-call')
  assert.equal(nodes[0].payload.event.payload.detail, 'complete')
})

test('authoritative root terminal never leaves an unmatched tool executing', () => {
  const nodes = reduceTurnActivities([
    event(1, 'tool:interrupted', 'tool', {
      event: {
        event_type: 'tool.started',
        run_id: 'root',
        root_run_id: 'root',
        tool_name: 'RenamedFactoryLookup',
        tool_call_id: 'interrupted-call',
      },
    }),
    event(2, 'workflow:root', 'workflow', {
      event: {
        event_type: 'run.failed',
        run_id: 'root',
        root_run_id: 'root',
        payload: { authoritative: true, error: 'provider_interrupted' },
      },
    }),
  ])
  assert.equal(nodes.find((node) => node.kind === 'tool').status, 'failed')
})

test('later provider failure does not rewrite an already completed tool', () => {
  const nodes = reduceTurnActivities([
    event(1, 'tool:write', 'tool', {
      event: {
        event_type: 'tool.started',
        run_id: 'root',
        root_run_id: 'root',
        tool_name: 'RenamedWarehouseWrite',
        tool_call_id: 'write-call',
      },
    }),
    event(2, 'tool:write', 'tool', {
      event: {
        event_type: 'tool.completed',
        run_id: 'root',
        root_run_id: 'root',
        tool_name: 'RenamedWarehouseWrite',
        tool_call_id: 'write-call',
      },
    }),
    event(3, 'workflow:root', 'workflow', {
      event: {
        event_type: 'run.failed',
        run_id: 'root',
        root_run_id: 'root',
        payload: {
          authoritative: true,
          error: 'provider_transport_reset',
        },
      },
    }),
  ])
  assert.equal(nodes.find((node) => node.kind === 'tool').status, 'succeeded')
  assert.equal(nodes.find((node) => node.kind === 'workflow').status, 'failed')
})

test('durable assistant joins activities only by exact root run identity', () => {
  const messages = [
    { id: 'a', role: 'assistant', run_id: 'root', content: 'fallback' },
    { id: 'b', role: 'assistant', run_id: 'another', content: 'other' },
  ]
  const attached = attachTurnActivities(messages, [
    event(1, 'content:1', 'content', { text: 'exact' }),
    event(2, 'projection:root', 'projection', { status: 'committed' }),
  ])
  assert.equal(attached[0].activityNodes[0].text, 'exact')
  assert.equal(attached[1].activityNodes, undefined)
})

test('bounded tail replay marks only the exact attached assistant as truncated', () => {
  const attached = attachTurnActivities([
    {
      role: 'assistant', durableRunPlaceholder: true,
      rootRunId: 'root', runActive: true, content: '',
    },
    {
      role: 'assistant', durableRunPlaceholder: true,
      rootRunId: 'another', runActive: true, content: '',
    },
  ], [
    event(8, 'content:tail', 'content', { text: 'newest window' }),
  ], { truncated: true })
  assert.equal(attached[0].activityTruncated, true)
  assert.equal(attached[1].activityTruncated, undefined)
})

test('refresh ignores an unsealed terminal timeline but replays active exact placeholder', () => {
  const partial = [event(1, 'content:1', 'content', { text: 'partial' })]
  const terminal = attachTurnActivities([
    { role: 'assistant', run_id: 'root', content: 'durable fallback' },
  ], partial)
  assert.equal(terminal[0].activityNodes, undefined)

  const active = attachTurnActivities([{
    role: 'assistant', durableRunPlaceholder: true, rootRunId: 'root',
    runActive: true, content: '',
  }], partial)
  assert.equal(active[0].activityNodes[0].text, 'partial')
})

test('approval decision updates the original pending card', () => {
  const nodes = reduceTurnActivities([
    event(1, 'approval:x', 'approval', {
      request_id: 'x', request_seq: 9, status: 'pending', tool_name: 'Bash',
    }),
    event(2, 'approval:x', 'approval', {
      request_id: 'x', status: 'allowed', tool_name: 'Bash',
    }),
  ])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].payload.status, 'allowed')
  assert.equal(nodes[0].payload.request_seq, 9)
})

test('native question decision preserves the exact projected question card', () => {
  const questions = [{
    question: 'Which museum wing should be audited?',
    header: 'Wing',
    multi_select: false,
    options: [
      { label: 'East', description: 'East wing' },
      { label: 'West', description: 'West wing' },
    ],
  }]
  const nodes = reduceTurnActivities([
    event(10, 'approval:question', 'approval', {
      request_id: 'museum-question', request_seq: 90, status: 'pending',
      interaction_kind: 'question', questions,
    }),
    event(11, 'approval:question', 'approval', {
      request_id: 'museum-question', status: 'allowed',
      interaction_kind: 'question',
    }),
  ])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].payload.status, 'allowed')
  assert.equal(nodes[0].payload.request_seq, 90)
  assert.deepEqual(nodes[0].payload.questions, questions)
})

test('root-scoped incremental replay advances without replacing prior nodes', () => {
  const initial = attachTurnActivities([{
    role: 'assistant', durableRunPlaceholder: true, rootRunId: 'root',
    runActive: true, content: '',
  }], [
    event(1, 'reasoning:1', 'reasoning', { text: 'first' }),
    event(2, 'content:2', 'content', { text: 'draft' }),
  ])
  assert.equal(turnActivityHighWater(initial, 'root'), 2)
  const merged = mergeTurnActivities(initial, [
    event(3, 'tool:z', 'tool', {
      event: { event_type: 'tool.started', tool_name: 'Lookup', tool_call_id: 'z' },
    }),
  ])
  assert.deepEqual(merged[0].activityNodes.map((node) => node.kind), [
    'reasoning', 'content', 'tool',
  ])
  assert.equal(turnActivityHighWater(merged, 'root'), 3)
})

test('ten thousand progress updates for one renamed task stay in one row', () => {
  const updates = Array.from({ length: 10_000 }, (_, index) => event(
    index + 1,
    'progress:renamed-warehouse-worker',
    'progress',
    { text: `checkpoint ${index + 1}`, category: 'native-task' },
  ))
  const nodes = reduceTurnActivities(updates)
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].text, 'checkpoint 10000')
  assert.equal(nodes[0].lastSeq, 10_000)
})

test('root terminal settles the existing progress row in place', () => {
  const nodes = reduceTurnActivities([
    event(1, 'progress:renamed-museum-worker', 'progress', {
      text: 'worker running', category: 'native-task', status: 'running',
    }),
    event(2, 'progress:renamed-museum-worker', 'progress', {
      text: 'worker finished', category: 'native-task', status: 'succeeded',
    }),
    event(3, 'workflow:root', 'workflow', {
      event: {
        event_type: 'run.completed', run_id: 'root', root_run_id: 'root',
        payload: { authoritative: true },
      },
    }),
  ])
  const progress = nodes.filter((node) => node.kind === 'progress')
  assert.equal(progress.length, 1)
  assert.equal(progress[0].text, 'worker finished')
  assert.equal(progress[0].status, 'succeeded')
})

test('failed root settles an unmatched progress row without creating a second row', () => {
  const nodes = reduceTurnActivities([
    event(1, 'progress:renamed-factory-worker', 'progress', {
      text: 'worker running', category: 'native-task', status: 'running',
    }),
    event(2, 'workflow:root', 'workflow', {
      event: {
        event_type: 'run.failed', run_id: 'root', root_run_id: 'root',
        payload: { authoritative: true, error: 'provider_http_429' },
      },
    }),
  ])
  const progress = nodes.filter((node) => node.kind === 'progress')
  assert.equal(progress.length, 1)
  assert.equal(progress[0].status, 'failed')
})
