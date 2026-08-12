import assert from 'node:assert/strict'
import test from 'node:test'

import {
  canApplyFrontendUpdate,
  currentFrontendEntry,
  frontendVersionChanged,
  getFrontendBuildInfo,
} from './frontendVersion.js'

test('current frontend entry ignores unrelated scripts', () => {
  const documentLike = {
    scripts: [
      { src: 'https://example.invalid/vendor.js' },
      { src: 'https://chat.example/assets/index-current.js' },
    ],
  }
  assert.equal(
    currentFrontendEntry(documentLike),
    '/assets/index-current.js',
  )
})

test('frontend version changes only for a different emitted entry asset', () => {
  assert.equal(frontendVersionChanged(
    '/assets/index-current.js',
    { entry: '/assets/index-current.js' },
  ), false)
  assert.equal(frontendVersionChanged(
    '/assets/index-current.js',
    { entry: '/assets/index-next.js' },
  ), true)
  assert.equal(frontendVersionChanged('', { entry: '/assets/index-next.js' }), false)
})

test('build info read bypasses browser caches', async () => {
  let captured
  const result = await getFrontendBuildInfo(async (url, options) => {
    captured = { url, options }
    return {
      ok: true,
      json: async () => ({ entry: '/assets/index-next.js' }),
    }
  })
  assert.equal(captured.url, '/build-info.json')
  assert.equal(captured.options.cache, 'no-store')
  assert.equal(result.entry, '/assets/index-next.js')
})

test('frontend update waits for a visible, idle page without an active editor', () => {
  const idle = {
    visibilityState: 'visible',
    activeElement: { tagName: 'BODY' },
    querySelector: () => null,
  }
  assert.equal(canApplyFrontendUpdate(idle), true)
  assert.equal(canApplyFrontendUpdate({ ...idle, visibilityState: 'hidden' }), false)
  assert.equal(canApplyFrontendUpdate({
    ...idle,
    querySelector: () => ({}),
  }), false)
  assert.equal(canApplyFrontendUpdate({
    ...idle,
    activeElement: { tagName: 'TEXTAREA' },
  }), false)
})
