/** Candidate-image integration check for the native artifact Stop frontier. */

import assert from 'node:assert/strict'
import { chmodSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'

import { Context } from '/opt/deepseek-harness/vendor/cordis/lib/index.js'
import AgentRegistry, { agentEvents } from '/opt/deepseek-harness/packages/core/agent/lib/index.js'
import { Session, SessionId } from '/opt/deepseek-harness/packages/core/session/lib/index.js'
import { createScope } from '/opt/deepseek-harness/packages/core/scope/lib/index.js'


const nativeSessionId = `chatds-${'3'.repeat(32)}`
mkdirSync('/runtime/controller', { recursive: true })
chmodSync('/runtime/controller', 0o750)
mkdirSync('/workspace', { recursive: true })
const projectionPath = '/runtime/controller/native-artifacts.json'
writeFileSync(projectionPath, JSON.stringify({
  schema: 'chatds.deepseek-artifact-gate.v1',
  native_session_id: nativeSessionId,
  bound_skill_names: ['museum-catalog'],
  workflow_run_name: null,
  contracts: [{
    skill_name: 'museum-catalog',
    declared_final_artifact: '{NAME}_FINAL.md',
    declared_modular_files: ['01_*.md', '02_*.md'],
    declared_ancillary_files: [],
    expected_min_bytes: 64,
    expected_max_bytes: 4096,
    expected_min_lines: 4,
    expected_max_lines: 100,
    declared_section_count: 2,
  }],
  workspace_before: {},
}))
chmodSync(projectionPath, 0o440)
process.env.CHATDS_DSH_ARTIFACT_PROJECTION = projectionPath

const ctx = new Context()
await ctx.plugin(AgentRegistry)
const plugin = await import('/opt/chatds-deepseek-plugins/native_artifact_gate.mjs')
await ctx.plugin(plugin)

const session = Session.create(SessionId(nativeSessionId), [], {
  version: 0,
  id: SessionId(nativeSessionId),
  createdAt: 0,
  cwd: '/workspace',
})
const steered = []
const agent = {
  id: SessionId(nativeSessionId),
  options: {},
  session,
  status: 'running',
  ctx: new Context(),
  send: () => {},
  followup: () => {},
  steer: (message) => { steered.push(message) },
  inject: () => {},
  cancel: () => {},
  runMaintenance: (task) => task(new AbortController().signal),
  whenIdle: () => Promise.resolve(),
}
const scope = createScope(ctx, agent)
agent.ctx = scope.ctx
const unregister = ctx.agents.register(agent)

await agentEvents(ctx, agent).serial('agent/turn-stopping', {
  turn: 1,
  signal: new AbortController().signal,
})
assert.equal(steered.length, 1)
assert.match(steered[0].content[0].text, /artifact receipt is incomplete/)

writeFileSync(
  '/workspace/museum_FINAL.md',
  `# Museum\n\n## Findings\n\n${'verified evidence '.repeat(5)}\n`,
)
writeFileSync('/workspace/01_east.md', 'east')
writeFileSync('/workspace/02_west.md', 'west')
await agentEvents(ctx, agent).serial('agent/turn-stopping', {
  turn: 1,
  signal: new AbortController().signal,
})
assert.equal(steered.length, 1)

rmSync('/workspace/museum_FINAL.md')
await assert.rejects(
  agentEvents(ctx, agent).serial('agent/turn-stopping', {
    turn: 1,
    signal: new AbortController().signal,
  }),
  /artifact contract remains incomplete/,
)

await unregister()
await scope.dispose()
await ctx.fiber.dispose()
process.stdout.write('native artifact gate candidate-image integration passed\n')
