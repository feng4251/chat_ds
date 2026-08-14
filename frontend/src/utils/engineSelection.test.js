import assert from 'node:assert/strict'
import test from 'node:test'

import { compatibleModelsForEngine, modelForEngine } from './engineSelection.js'

const models = [
  { id: 'alpha', compatible_engines: ['engine-red'] },
  { id: 'beta', compatible_engines: ['engine-blue'], is_default: true },
  { id: 'gamma', compatible_engines: ['engine-blue'] },
  { id: 'unbound' },
]

test('engine compatibility fails closed for models without an explicit binding', () => {
  assert.deepEqual(
    compatibleModelsForEngine(models, 'engine-blue').map((model) => model.id),
    ['beta', 'gamma'],
  )
})

test('engine selection preserves compatible state and otherwise uses declared default', () => {
  assert.equal(modelForEngine(models, 'engine-blue', 'gamma', 'beta'), 'gamma')
  assert.equal(modelForEngine(models, 'engine-blue', 'alpha', 'gamma'), 'gamma')
})

test('renamed engines use the same data-driven matching rule', () => {
  const mutated = [{ id: 'warehouse-model', compatible_engines: ['warehouse-harness'] }]
  assert.equal(modelForEngine(mutated, 'warehouse-harness', '', ''), 'warehouse-model')
  assert.equal(modelForEngine(mutated, 'factory-harness', '', ''), '')
})
