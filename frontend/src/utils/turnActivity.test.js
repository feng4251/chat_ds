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
