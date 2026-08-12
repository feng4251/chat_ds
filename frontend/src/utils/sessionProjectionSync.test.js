import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createSessionRefreshCoordinator,
  runCardMessageRevision,
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
