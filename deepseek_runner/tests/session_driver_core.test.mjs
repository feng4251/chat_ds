import assert from 'node:assert/strict'
import test from 'node:test'

import {
  NATIVE_TURN_INPUT_SCHEMA,
  NATIVE_RUN_CONTROL_SCHEMA,
  applyNativeRunControl,
  selectNativeTurnPrompt,
  summarizeNativeInterval,
  synchronizeNativePermissionPreset,
  validateNativeTurnInput,
} from '../session_driver_core.mjs'

const fixture = (overrides = {}) => ({
  schema: NATIVE_TURN_INPUT_SCHEMA,
  native_session_id: `chatds-${'a'.repeat(32)}`,
  permission_preset: 'workspace-write',
  initial_prompt: '<USER>\nInspect the museum ledger\n</USER>',
  turn_prompt: 'Continue with the renamed gallery',
  ...overrides,
})

test('a stable native Session imports history once and then accepts only this Turn', () => {
  assert.equal(
    selectNativeTurnPrompt(fixture(), false),
    '<USER>\nInspect the museum ledger\n</USER>',
  )
  assert.equal(
    selectNativeTurnPrompt(fixture(), true),
    'Continue with the renamed gallery',
  )
})

test('the native Turn boundary rejects renamed identity and schema mutations', () => {
  assert.throws(
    () => validateNativeTurnInput(fixture({ native_session_id: 'factory-session' })),
    /native Turn input is invalid/,
  )
  assert.throws(
    () => validateNativeTurnInput({ ...fixture(), fixture_only: true }),
    /native Turn input is invalid/,
  )
  assert.throws(
    () => validateNativeTurnInput(fixture({ permission_preset: 'fixture-access' })),
    /native Turn input is invalid/,
  )
})

test('a resumed Session synchronizes policy through the native permission service', () => {
  const session = { events: [{ preset: 'read-only' }] }
  const writes = []
  const service = {
    current(events) {
      return events.at(-1)?.preset
    },
    set(target, preset) {
      writes.push(preset)
      target.events.push({ preset })
    },
  }
  synchronizeNativePermissionPreset(service, session, 'danger-full-access')
  assert.deepEqual(writes, ['danger-full-access'])
  synchronizeNativePermissionPreset(service, session, 'danger-full-access')
  assert.deepEqual(writes, ['danger-full-access'])
})

test('the result summary cannot replay assistant text from an earlier Turn', () => {
  const outcome = summarizeNativeInterval([
    {
      seq: 8,
      type: 'assistant/message',
      data: { message: { content: [{ type: 'text', text: 'stale' }] } },
    },
    { seq: 9, type: 'turn/start', data: {} },
    {
      seq: 10,
      type: 'assistant/message',
      data: { message: { content: [{ type: 'text', text: 'current' }] } },
    },
    { seq: 11, type: 'turn/end', data: { reason: { kind: 'completed' } } },
  ], 9)
  assert.deepEqual(outcome, {
    text: 'current',
    reason: { kind: 'completed' },
  })
})

test('generic Web controls map only to the pinned native Agent Host API', () => {
  const calls = []
  const agent = {
    cancel(cause, options) { calls.push(['cancel', cause, options]) },
    followup(message) { calls.push(['followup', message]) },
    steer(message) { calls.push(['steer', message]) },
  }
  const createMessage = (value) => ({ id: 'native-message', ...value })
  applyNativeRunControl(agent, {
    schema: NATIVE_RUN_CONTROL_SCHEMA,
    control_id: '1'.repeat(32),
    seq: 1,
    action: 'interrupt',
    text: null,
  }, createMessage)
  applyNativeRunControl(agent, {
    schema: NATIVE_RUN_CONTROL_SCHEMA,
    control_id: '2'.repeat(32),
    seq: 2,
    action: 'followup',
    text: 'Inspect the renamed observatory log.',
  }, createMessage)
  applyNativeRunControl(agent, {
    schema: NATIVE_RUN_CONTROL_SCHEMA,
    control_id: '3'.repeat(32),
    seq: 3,
    action: 'steer',
    text: 'Use the newest telescope reading first.',
  }, createMessage)
  assert.deepEqual(calls, [
    ['cancel', { kind: 'user' }, { keepInbox: true }],
    ['followup', {
      id: 'native-message',
      content: [{ type: 'text', text: 'Inspect the renamed observatory log.' }],
      source: { kind: 'user' },
    }],
    ['steer', {
      id: 'native-message',
      content: [{ type: 'text', text: 'Use the newest telescope reading first.' }],
      source: { kind: 'user' },
    }],
  ])
})

test('interrupt preserves and wakes already queued native input without duplication', () => {
  for (const target of ['next-turn', 'next-step']) {
    const first = { id: `${target}-first`, content: [{ type: 'text', text: 'Audit the harbor ledger.' }] }
    const queued = { id: `${target}-queued`, content: [{ type: 'text', text: 'Summarize the result.' }] }
    const state = {
      'next-turn': target === 'next-turn' ? [first, queued] : [],
      'next-step': target === 'next-step' ? [first, queued] : [],
    }
    let aborted = false
    let wakeLatched = false
    const calls = []
    const inbox = {
      get nextTurn() { return state['next-turn'] },
      get nextStep() { return state['next-step'] },
      remove(messageId) {
        for (const key of ['next-turn', 'next-step']) {
          const index = state[key].findIndex((message) => message.id === messageId)
          if (index >= 0) {
            state[key].splice(index, 1)
            calls.push(['remove', messageId])
            return true
          }
        }
        return false
      },
    }
    const agent = {
      status: 'running',
      inbox,
      cancel(cause, options) {
        calls.push(['cancel', cause, options])
        aborted = true
      },
      send(message, requestedTarget, wakeup) {
        calls.push(['send', message.id, requestedTarget, wakeup])
        const resolvedTarget = aborted && wakeup ? 'next-turn' : requestedTarget
        state[resolvedTarget].push(message)
        wakeLatched = aborted && wakeup
      },
      followup() {},
      steer() {},
    }

    applyNativeRunControl(agent, {
      schema: NATIVE_RUN_CONTROL_SCHEMA,
      control_id: target === 'next-turn' ? '4'.repeat(32) : '5'.repeat(32),
      seq: 4,
      action: 'interrupt',
      text: null,
    }, (value) => value)

    assert.equal(wakeLatched, true)
    assert.deepEqual(
      [...state['next-step'], ...state['next-turn']].map((message) => message.id).sort(),
      [first.id, queued.id].sort(),
    )
    assert.equal(
      [...state['next-step'], ...state['next-turn']].filter(
        (message) => message.id === queued.id,
      ).length,
      1,
    )
    assert.deepEqual(calls[0], ['cancel', { kind: 'user' }, { keepInbox: true }])
    assert.deepEqual(calls.at(-1), ['send', queued.id, 'next-turn', true])
  }
})
