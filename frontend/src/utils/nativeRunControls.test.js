import test from 'node:test'
import assert from 'node:assert/strict'

import {
  activeNativeRunFromCards,
  canAttemptNativeControl,
  canSubmitNativeControl,
  createNativeControlId,
  draftAfterNativeControlReceipt,
  isTerminalNativeControlStatus,
  pendingNativeControlId,
  reconcileActiveNativeRun,
  resolveNativeControlTarget,
} from './nativeRunControls.js'

test('runtime controls are enabled by declared capabilities, not engine names', () => {
  const activeRun = activeNativeRunFromCards({
    roots: [{
      root_run_id: 'a'.repeat(32),
      engine_id: 'renamed-native-engine',
      active: true,
      controls: [],
    }],
  })
  const engineOptions = [{
    id: 'renamed-native-engine',
    capabilities: ['native_interrupt', 'native_followup', 'native_steer'],
  }]
  assert.equal(canSubmitNativeControl({
    activeRun, engineOptions, action: 'followup', text: 'Inspect the archive.',
  }), true)
  assert.equal(canSubmitNativeControl({
    activeRun, engineOptions, action: 'steer', text: 'Use the newer record.',
  }), true)
  assert.equal(canSubmitNativeControl({
    activeRun, engineOptions, action: 'interrupt', text: '',
  }), true)
})

test('ambiguous activity and attachments fail closed for message controls', () => {
  assert.equal(activeNativeRunFromCards({
    roots: [
      { root_run_id: '1'.repeat(32), engine_id: 'one', active: true },
      { root_run_id: '2'.repeat(32), engine_id: 'two', active: true },
    ],
  }), null)
  const activeRun = {
    runId: '3'.repeat(32), engineId: 'native', controls: [],
  }
  const engineOptions = [{ id: 'native', capabilities: ['native_followup'] }]
  assert.equal(canSubmitNativeControl({
    activeRun, engineOptions, action: 'followup', text: 'Continue', attachmentCount: 1,
  }), false)
  assert.equal(canSubmitNativeControl({
    activeRun, engineOptions, action: 'followup', text: 'Continue', stateUnknown: true,
  }), false)
})

test('a declared native engine can recover controls before a run target is hydrated', () => {
  const engineOptions = [{
    id: 'renamed-harbor-engine',
    capabilities: ['native_interrupt', 'native_followup'],
  }]
  assert.equal(canAttemptNativeControl({
    engineId: 'renamed-harbor-engine',
    engineOptions,
    action: 'followup',
    text: 'Inspect the inventory ledger.',
  }), true)
  assert.equal(canAttemptNativeControl({
    engineId: 'renamed-harbor-engine',
    engineOptions,
    action: 'interrupt',
    text: '',
  }), true)
  assert.equal(canAttemptNativeControl({
    engineId: 'renamed-harbor-engine',
    engineOptions,
    action: 'followup',
    text: 'Inspect the inventory ledger.',
    attachmentCount: 1,
  }), false)
})

test('a stale empty hydration cannot erase an SSE-accepted run identity', () => {
  const acceptedRunId = '8'.repeat(32)
  const currentRun = {
    runId: acceptedRunId,
    engineId: 'renamed-native-engine',
    controls: [],
    controlsTruncated: false,
  }
  assert.deepEqual(reconcileActiveNativeRun({
    currentRun,
    runCards: { roots: [], has_active_runs: false },
    acceptedRunId,
  }), currentRun)

  assert.equal(reconcileActiveNativeRun({
    currentRun,
    runCards: {
      roots: [{
        root_run_id: acceptedRunId,
        engine_id: 'renamed-native-engine',
        active: false,
        controls: [],
      }],
      has_active_runs: false,
    },
    acceptedRunId,
  }), null)

  assert.equal(reconcileActiveNativeRun({
    currentRun,
    runCards: {
      roots: [
        { root_run_id: 'a'.repeat(32), engine_id: 'one', active: true },
        { root_run_id: 'b'.repeat(32), engine_id: 'two', active: true },
      ],
      has_active_runs: true,
    },
    acceptedRunId,
  }), null)
})

test('missing native control target is recovered only from one exact active root', async () => {
  const runId = '9'.repeat(32)
  let loads = 0
  const exact = await resolveNativeControlTarget({
    activeRun: null,
    stateUnknown: true,
    loadRunCards: async () => {
      loads += 1
      return {
        has_active_runs: true,
        roots: [{
          root_run_id: runId,
          engine_id: 'renamed-native-engine',
          active: true,
          controls: [],
        }],
      }
    },
  })
  assert.equal(loads, 1)
  assert.equal(exact.activeRun.runId, runId)

  const ambiguous = await resolveNativeControlTarget({
    activeRun: null,
    stateUnknown: true,
    loadRunCards: async () => ({
      has_active_runs: true,
      roots: [
        { root_run_id: 'a'.repeat(32), engine_id: 'one', active: true },
        { root_run_id: 'b'.repeat(32), engine_id: 'two', active: true },
      ],
    }),
  })
  assert.equal(ambiguous.activeRun, null)
})

test('browser control ids lower UUIDs to the exact durable identity', () => {
  assert.equal(createNativeControlId({
    randomUUID: () => '12345678-1234-4234-8234-1234567890ab',
  }), '123456781234423482341234567890ab')
})

test('pending controls reuse their durable id after a reconnect', () => {
  const followupId = '4'.repeat(32)
  const interruptId = '5'.repeat(32)
  const activeRun = {
    runId: '6'.repeat(32),
    controls: [
      {
        control_id: followupId,
        action: 'followup',
        status: 'pending',
        message_id: 'followup-message',
      },
      {
        control_id: interruptId,
        action: 'interrupt',
        status: 'pending',
        message_id: null,
      },
    ],
  }
  const messages = [{
    id: 'followup-message',
    source: 'native_control',
    content: 'Inspect the renamed harbor ledger.',
  }]
  assert.equal(pendingNativeControlId({
    activeRun,
    messages,
    action: 'followup',
    text: ' Inspect the renamed harbor ledger. ',
  }), followupId)
  assert.equal(pendingNativeControlId({
    activeRun,
    messages,
    action: 'interrupt',
  }), interruptId)
  assert.equal(isTerminalNativeControlStatus('pending'), false)
  assert.equal(isTerminalNativeControlStatus('delivered'), true)
  assert.equal(isTerminalNativeControlStatus('rejected'), true)
})

test('a late delivered receipt clears only its unchanged queued draft', () => {
  const retry = {
    controlId: '7'.repeat(32),
    action: 'followup',
    text: 'Inspect the renamed archive.',
  }
  const delivered = { control_id: retry.controlId, status: 'delivered' }
  assert.equal(draftAfterNativeControlReceipt(
    ' Inspect the renamed archive. ', retry, delivered,
  ), '')
  assert.equal(draftAfterNativeControlReceipt(
    'A newer draft', retry, delivered,
  ), 'A newer draft')
  assert.equal(draftAfterNativeControlReceipt(
    retry.text, retry, { ...delivered, status: 'pending' },
  ), retry.text)
})
