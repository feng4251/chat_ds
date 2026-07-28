import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

test('API client never writes bearer-token material to console logs', async () => {
  const source = await readFile(new URL('../api.js', import.meta.url), 'utf8')
  const consoleCalls = source.match(/console\.(?:log|debug|info|warn|error)\([^\n]*/g) || []

  for (const call of consoleCalls) {
    assert.doesNotMatch(call, /\btoken\b|authorization/i)
  }
  assert.doesNotMatch(source, /\btoken\s*\.\s*slice\s*\(/)
})
