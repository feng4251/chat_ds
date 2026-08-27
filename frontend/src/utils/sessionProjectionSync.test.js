import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createSessionRefreshCoordinator,
  createSessionRefreshLoop,
  messageUpdateScrollBehavior,
  runCardMessageRevision,
  sessionProjectionRefreshPlan,
  sessionProjectionHasDelta,
  shouldFollowMessageUpdate,
} from './sessionProjectionSync.js'

test('message revision changes only at durable message boundaries', () => {
  const running = {
    roots: [{
      root_run_id: 'inventory-root',
      trigger_message_id: 'inventory-request',
      assistant_message_id: null,
      active: true,
      status: 'running',
      runs: [{ id: 'inventory-root', lifecycle_status: 'running', preview: 'a' }],
    }],
  }
  const toolProgress = structuredClone(running)
  toolProgress.roots[0].runs[0].preview = 'a later tool update'
  const terminal = structuredClone(toolProgress)
  terminal.roots[0].active = false
  terminal.roots[0].status = 'succeeded'
  terminal.roots[0].assistant_message_id = 'inventory-response'

  assert.equal(
    runCardMessageRevision(running),
    runCardMessageRevision(toolProgress),
  )
  assert.notEqual(
    runCardMessageRevision(running),
    runCardMessageRevision(terminal),
  )

  const withRunningFollowup = structuredClone(running)
  withRunningFollowup.roots[0].controls = [{
    control_id: 'followup-control',
    message_id: 'followup-message',
    action: 'followup',
    status: 'pending',
  }]
  assert.notEqual(
    runCardMessageRevision(running),
    runCardMessageRevision(withRunningFollowup),
  )
})

test('refresh coordinator coalesces overlap and preserves a forced reconciliation', async () => {
  let releaseFirst
  const first = new Promise((resolve) => { releaseFirst = resolve })
  const calls = []
  const coordinator = createSessionRefreshCoordinator({
    canRefresh: () => true,
    refresh: async ({ forceFull }) => {
      calls.push(forceFull)
      if (calls.length === 1) await first
    },
  })

  const active = coordinator.request(false)
  coordinator.request(false)
  coordinator.request(true)
  assert.deepEqual(calls, [false])

  releaseFirst()
  await active
  await coordinator.whenIdle()
  assert.deepEqual(calls, [false, true])
})

test('refresh coordinator does not run after route ownership is revoked', async () => {
  let ownsRoute = true
  let callCount = 0
  const coordinator = createSessionRefreshCoordinator({
    canRefresh: () => ownsRoute,
    refresh: async () => { callCount += 1 },
  })

  await coordinator.request(false)
  ownsRoute = false
  await coordinator.request(true)
  assert.equal(callCount, 1)
})

test('refresh loop rearms after a skipped or failed reconciliation', async () => {
  const timers = new Map()
  let nextTimer = 0
  let attempts = 0
  const loop = createSessionRefreshLoop({
    request: async () => {
      attempts += 1
      if (attempts === 1) throw new Error('transient read failure')
    },
    getDelay: () => 17,
    initialDelay: 11,
    setTimer: (callback, delay) => {
      const id = ++nextTimer
      timers.set(id, { callback, delay })
      return id
    },
    clearTimer: (id) => timers.delete(id),
  })

  loop.start()
  assert.deepEqual([...timers.values()].map((timer) => timer.delay), [11])
  const first = [...timers.values()][0]
  timers.clear()
  first.callback()
  await new Promise((resolve) => globalThis.setTimeout(resolve, 0))
  assert.equal(attempts, 1)
  assert.deepEqual([...timers.values()].map((timer) => timer.delay), [17])

  const second = [...timers.values()][0]
  timers.clear()
  second.callback()
  await new Promise((resolve) => globalThis.setTimeout(resolve, 0))
  assert.equal(attempts, 2)
  assert.equal(timers.size, 1)
  loop.stop()
  assert.equal(timers.size, 0)
})

test('refresh loop wake cancels the stale timer and forces reconciliation', async () => {
  const timers = new Map()
  let nextTimer = 0
  const forces = []
  const loop = createSessionRefreshLoop({
    request: async (forceFull) => { forces.push(forceFull) },
    initialDelay: 50,
    setTimer: (callback, delay) => {
      const id = ++nextTimer
      timers.set(id, { callback, delay })
      return id
    },
    clearTimer: (id) => timers.delete(id),
  })

  loop.start()
  assert.equal(timers.size, 1)
  loop.wake()
  await new Promise((resolve) => globalThis.setTimeout(resolve, 0))
  assert.deepEqual(forces, [true])
  assert.equal(timers.size, 1)
  loop.stop()
})

test('message following uses the pre-append pin state', () => {
  assert.equal(shouldFollowMessageUpdate(true, false), true)
  assert.equal(shouldFollowMessageUpdate(false, true), false)
  assert.equal(shouldFollowMessageUpdate(false, false), false)
})

test('a live SSE turn refreshes control authority without replacing its transcript', () => {
  assert.deepEqual(sessionProjectionRefreshPlan({
    liveRequestActive: true,
    needsFullReconcile: true,
    hasActiveRuns: true,
  }), {
    readMessages: false,
    readActivities: false,
    incrementalActivities: false,
    updateTimeline: false,
  })
  assert.deepEqual(sessionProjectionRefreshPlan({
    liveRequestActive: false,
    needsFullReconcile: false,
    hasActiveRuns: true,
  }), {
    readMessages: false,
    readActivities: true,
    incrementalActivities: true,
    updateTimeline: true,
  })
})

test('durable live updates never animate the document through moving cards', () => {
  assert.equal(messageUpdateScrollBehavior(false, true), 'auto')
  assert.equal(messageUpdateScrollBehavior(true, false), 'auto')
  assert.equal(messageUpdateScrollBehavior(false, false), 'smooth')
})

test('empty activity polls are no-ops unless the durable projection changed', () => {
  assert.equal(sessionProjectionHasDelta({ events: [] }, false), false)
  assert.equal(sessionProjectionHasDelta(null, false), false)
  assert.equal(sessionProjectionHasDelta({ events: [{ seq: 1 }] }, false), true)
  assert.equal(sessionProjectionHasDelta({ events: [] }, true), true)
})
