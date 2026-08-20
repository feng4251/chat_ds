import assert from 'node:assert/strict'
import test from 'node:test'

import { normalizeToolIdentityStream } from '../provider_compat_bridge.mjs'

async function collect(values) {
  async function* source() {
    for (const value of values) yield value
  }
  const result = []
  for await (const value of normalizeToolIdentityStream(source())) result.push(value)
  return result
}

test('preserves a renamed cross-domain tool identity across blank deltas', async () => {
  const chunks = await collect([
    { type: 'block-start', index: 3, blockType: 'tool-call' },
    {
      type: 'tool-call-delta', index: 3,
      id: 'call-inventory-7', name: 'mcp__warehouse__lookup', argumentsDelta: '{',
    },
    {
      type: 'tool-call-delta', index: 3,
      id: '', name: '', argumentsDelta: '"sku":"A-7"}',
    },
    {
      type: 'block-end', index: 3,
      block: { type: 'tool-call', id: '', name: '', arguments: '{"sku":"A-7"}' },
    },
  ])

  assert.equal(chunks[2].id, 'call-inventory-7')
  assert.equal(chunks[2].name, 'mcp__warehouse__lookup')
  assert.equal(chunks[3].block.id, 'call-inventory-7')
  assert.equal(chunks[3].block.name, 'mcp__warehouse__lookup')
  assert.equal(chunks[3].block.arguments, '{"sku":"A-7"}')
})

test('keeps interleaved tool-call identities isolated by block index', async () => {
  const chunks = await collect([
    { type: 'tool-call-delta', index: 0, id: 'call-a', name: 'alpha', argumentsDelta: '{' },
    { type: 'tool-call-delta', index: 1, id: 'call-b', name: 'beta', argumentsDelta: '[' },
    { type: 'tool-call-delta', index: 0, id: '', name: '', argumentsDelta: '}' },
    { type: 'tool-call-delta', index: 1, id: '', name: '', argumentsDelta: ']' },
  ])
  assert.deepEqual(
    chunks.map(chunk => [chunk.index, chunk.id, chunk.name]),
    [[0, 'call-a', 'alpha'], [1, 'call-b', 'beta'], [0, 'call-a', 'alpha'], [1, 'call-b', 'beta']],
  )
})

test('rejects a non-empty identity mutation instead of dispatching ambiguously', async () => {
  await assert.rejects(
    collect([
      { type: 'tool-call-delta', index: 0, id: 'call-a', name: 'alpha', argumentsDelta: '' },
      { type: 'tool-call-delta', index: 0, id: 'call-b', name: 'alpha', argumentsDelta: '' },
    ]),
    /chatds_provider_tool_id_changed/,
  )
})
