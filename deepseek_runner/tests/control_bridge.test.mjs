import assert from 'node:assert/strict'
import test from 'node:test'

import {
  approvalDispositionForWebPreset,
  findPendingApprovalId,
  questionAnswerFromDecision,
} from '../control_bridge.mjs'

test('Web tiers preserve native one-shot approval semantics across renamed tasks', () => {
  assert.equal(approvalDispositionForWebPreset('read_only'), 'reject')
  assert.equal(approvalDispositionForWebPreset('workspace_write'), 'relay')
  assert.equal(approvalDispositionForWebPreset('session_full'), 'reject')
  assert.throws(
    () => approvalDispositionForWebPreset('museum-fixture-access'),
    /invalid Web permission preset/,
  )
})

test('reuses the native audited id for a renamed cross-domain call', () => {
  const events = [
    { type: 'turn/start', data: {} },
    {
      type: 'approval/asked',
      data: { id: 'native-approval-7', toolName: 'warehouse_write', callId: 'call-7' },
    },
  ]
  assert.equal(
    findPendingApprovalId(events, { callId: 'call-7' }),
    'native-approval-7',
  )
})

test('parallel asks cannot claim each other or reuse a settled id', () => {
  const events = [
    { type: 'approval/asked', data: { id: 'approval-a', callId: 'call-a' } },
    { type: 'approval/asked', data: { id: 'approval-b', callId: 'call-b' } },
  ]
  const claimed = new Set(['approval-b'])
  assert.equal(
    findPendingApprovalId(events, { callId: 'call-a' }, claimed),
    'approval-a',
  )
  assert.equal(
    findPendingApprovalId(events, { callId: 'call-b' }, claimed),
    undefined,
  )
  events.push({ type: 'approval/decided', data: { id: 'approval-a', outcome: 'rejected' } })
  assert.equal(
    findPendingApprovalId(events, { callId: 'call-a' }, new Set()),
    undefined,
  )
})

test('call-id-less asks match only call-id-less native audits', () => {
  const events = [
    { type: 'approval/asked', data: { id: 'with-call', callId: 'call-x' } },
    { type: 'approval/asked', data: { id: 'without-call' } },
  ]
  assert.equal(findPendingApprovalId(events, {}), 'without-call')
})

test('plan-review decisions preserve native option labels under rename', () => {
  const question = {
    intent: { kind: 'plan-review', approve: 'Ship museum plan' },
    options: [
      { label: 'Revise museum plan' },
      { label: 'Ship museum plan' },
    ],
  }
  assert.deepEqual(
    questionAnswerFromDecision(question, { decision: 'allow' }),
    { selected: ['Ship museum plan'] },
  )
  assert.deepEqual(
    questionAnswerFromDecision(question, { decision: 'deny' }),
    { selected: ['Revise museum plan'] },
  )
})

test('generic question answers remain structured without plan inference', () => {
  assert.deepEqual(
    questionAnswerFromDecision(
      { options: [{ label: 'Cold' }, { label: 'Dry' }], multiSelect: true },
      { selected: ['Cold', 'Dry'], custom: 'Warehouse 7' },
    ),
    { selected: ['Cold', 'Dry'], custom: 'Warehouse 7' },
  )
})
