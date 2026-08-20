import assert from 'node:assert/strict'
import test from 'node:test'

import {
  compileWorkflowProgram,
  validateWorkflowProjection,
} from '../native_workflow_core.mjs'

const projection = (overrides = {}) => ({
  schema: 'chatds.deepseek-skill-workflow.v1',
  native_session_id: `chatds-${'7'.repeat(32)}`,
  skill_name: 'museum-catalog',
  route_id: 'gallery-reconciliation',
  route_sha256: 'a'.repeat(64),
  run_name: 'skill-workflow-aaaaaaaaaaaaaaaa',
  user_turn_text: 'Reconcile the renamed gallery',
  max_attempts_per_worker: 2,
  handoff_max_chars: 24000,
  phases: [
    {
      mode: 'parallel',
      phase_id: 'phase-0',
      workers: [
        {
          worker_id: 'east-curator',
          source_path: 'orchestration/workers/east-curator.yaml',
          source_sha256: '1'.repeat(64),
          source_size: 17,
        },
        {
          worker_id: 'west-curator',
          source_path: 'orchestration/workers/west-curator.yaml',
          source_sha256: '2'.repeat(64),
          source_size: 17,
        },
      ],
    },
    {
      mode: 'sequential',
      phase_id: 'phase-1',
      workers: [{
        worker_id: 'catalog-fanin',
        source_path: 'orchestration/workers/catalog-fanin.yaml',
        source_sha256: '3'.repeat(64),
        source_size: 17,
      }],
    },
  ],
  ...overrides,
})

test('the projected program keeps business data in args and topology in generic code', () => {
  const value = validateWorkflowProjection(projection())
  const compiled = compileWorkflowProgram(value, new Map([
    ['east-curator', 'east instructions'],
    ['west-curator', 'west instructions'],
    ['catalog-fanin', 'fanin instructions'],
  ]))
  assert.match(compiled.script, /parallel\(/)
  assert.match(compiled.script, /maxAttempts/)
  assert.doesNotMatch(compiled.script, /museum|gallery|curator/i)
  assert.equal(compiled.args.userTurnText, 'Reconcile the renamed gallery')
  assert.deepEqual(
    compiled.args.phases.map((phase) => phase.workers.map((worker) => worker.workerId)),
    [['east-curator', 'west-curator'], ['catalog-fanin']],
  )
})

test('projection validation rejects path traversal and phase renames', () => {
  const unsafe = projection()
  unsafe.phases[0].workers[0].source_path = '../private.yaml'
  assert.throws(() => validateWorkflowProjection(unsafe), /workflow projection is invalid/)

  const renamed = projection()
  renamed.phases[1].phase_id = 'fixture-phase'
  assert.throws(() => validateWorkflowProjection(renamed), /workflow projection is invalid/)
})
