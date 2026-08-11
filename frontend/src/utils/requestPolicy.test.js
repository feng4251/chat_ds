import assert from 'node:assert/strict'
import test from 'node:test'

import {
  fetchWithIdempotentReadRetry,
  isIdempotentRead,
  isRetryableReadError,
} from './requestPolicy.js'

const noSleep = async () => {}

test('GET retries a transient gateway response and returns the recovered response', async () => {
  const responses = [{ status: 502 }, { status: 200 }]
  let calls = 0
  const response = await fetchWithIdempotentReadRetry('/settings', {}, {
    fetchImpl: async () => responses[calls++],
    retryDelaysMs: [0, 0],
    sleepImpl: noSleep,
  })

  assert.equal(response.status, 200)
  assert.equal(calls, 2)
})

test('GET retries a transport failure but never exceeds the bounded budget', async () => {
  let calls = 0
  const response = await fetchWithIdempotentReadRetry('/settings', {}, {
    fetchImpl: async () => {
      calls += 1
      if (calls < 3) throw new TypeError('network reset')
      return { status: 200 }
    },
    retryDelaysMs: [0, 0],
    sleepImpl: noSleep,
  })

  assert.equal(response.status, 200)
  assert.equal(calls, 3)
})

test('non-idempotent mutations are never replayed after a gateway response', async () => {
  let calls = 0
  const response = await fetchWithIdempotentReadRetry('/settings', { method: 'PATCH' }, {
    fetchImpl: async () => {
      calls += 1
      return { status: 502 }
    },
    retryDelaysMs: [0, 0],
    sleepImpl: noSleep,
  })

  assert.equal(response.status, 502)
  assert.equal(calls, 1)
})

test('non-transient client errors are returned without retry', async () => {
  let calls = 0
  const response = await fetchWithIdempotentReadRetry('/settings', {}, {
    fetchImpl: async () => {
      calls += 1
      return { status: 400 }
    },
    retryDelaysMs: [0, 0],
    sleepImpl: noSleep,
  })

  assert.equal(response.status, 400)
  assert.equal(calls, 1)
})

test('aborted reads and explicit mutations are not classified as retryable', () => {
  assert.equal(isIdempotentRead({}), true)
  assert.equal(isIdempotentRead({ method: 'HEAD' }), true)
  assert.equal(isIdempotentRead({ method: 'POST' }), false)
  assert.equal(
    isRetryableReadError(new TypeError('offline'), { signal: { aborted: true } }),
    false,
  )
})
