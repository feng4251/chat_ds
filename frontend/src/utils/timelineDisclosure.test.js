import assert from 'node:assert/strict'
import test from 'node:test'

import { liveDisclosureOpen } from './timelineDisclosure.js'

test('only the current live block auto-opens without overriding user choice', () => {
  assert.equal(liveDisclosureOpen(null, true), true)
  assert.equal(liveDisclosureOpen(null, false), false)
  assert.equal(liveDisclosureOpen(true, false), true)
  assert.equal(liveDisclosureOpen(false, true), false)
})
