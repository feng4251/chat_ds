/** Candidate-image integration check for the root-scoped native workflow plugin. */

import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'

import { Context } from '/opt/deepseek-harness/vendor/cordis/lib/index.js'
import SystemPrompt from '/opt/deepseek-harness/packages/core/system-prompt/lib/index.js'
import ToolRuntime from '/opt/deepseek-harness/packages/core/tools/lib/index.js'
import AgentRegistry from '/opt/deepseek-harness/packages/core/agent/lib/index.js'
import { WorkflowEngine } from '/opt/deepseek-harness/packages/workflow/workflow/lib/index.js'
import { Session, SessionId } from '/opt/deepseek-harness/packages/core/session/lib/index.js'
import { createScope } from '/opt/deepseek-harness/packages/core/scope/lib/index.js'
import { CallId } from '/opt/deepseek-harness/packages/llm/llm/lib/index.js'


const nativeSessionId = `chatds-${'5'.repeat(32)}`
const source = Buffer.from('role: inspect a renamed warehouse\n', 'utf8')
const sourcePath = '/skill-view/plugin/skills/warehouse-audit/orchestration/workers/inspector.yaml'
mkdirSync('/runtime/controller', { recursive: true })
mkdirSync('/skill-view/plugin/skills/warehouse-audit/orchestration/workers', { recursive: true })
writeFileSync(sourcePath, source)
writeFileSync('/runtime/controller/native-workflow.json', JSON.stringify({
  schema: 'chatds.deepseek-skill-workflow.v1',
  native_session_id: nativeSessionId,
  skill_name: 'warehouse-audit',
  route_id: 'renamed-route',
  route_sha256: 'a'.repeat(64),
  run_name: `skill-workflow-${'a'.repeat(16)}`,
  user_turn_text: 'Inspect the renamed warehouse',
  max_attempts_per_worker: 2,
  handoff_max_chars: 24000,
  phases: [{
    mode: 'sequential',
    phase_id: 'phase-0',
    workers: [{
      worker_id: 'inspector',
      source_path: 'orchestration/workers/inspector.yaml',
      source_sha256: createHash('sha256').update(source).digest('hex'),
      source_size: source.length,
    }],
  }],
}))
process.env.CHATDS_DSH_WORKFLOW_PROJECTION = '/runtime/controller/native-workflow.json'


class StubWorkflowEngine extends WorkflowEngine {
  constructor(ctx) {
    super(ctx)
    this.requests = []
  }

  start(request) {
    this.requests.push(request)
    let settle
    const result = new Promise((resolve) => { settle = resolve })
    this.settle = settle
    return {
      id: 'run-image-check',
      meta: request.meta,
      result,
      cancel: () => {},
      dispose: async () => {},
    }
  }

  agentStart(value) {
    this.emitWorkflowEvent(
      'workflow/agent-start',
      { id: 'run-image-check', meta: this.requests[0].meta },
      value,
    )
  }

  agentEnd(value) {
    this.emitWorkflowEvent(
      'workflow/agent-end',
      { id: 'run-image-check', meta: this.requests[0].meta },
      value,
    )
  }
}


const ctx = new Context()
await ctx.plugin(SystemPrompt)
await ctx.plugin(ToolRuntime)
await ctx.plugin(AgentRegistry)
await ctx.plugin(StubWorkflowEngine)
const plugin = await import('/opt/chatds-deepseek-plugins/native_workflow.mjs')
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
await new Promise((resolve) => setImmediate(resolve))

assert.equal(ctx.tools.get('execute_skill_workflow'), undefined)
assert.notEqual(ctx.tools.get('execute_skill_workflow', agent), undefined)
const pending = ctx.tools.execute({
  signal: new AbortController().signal,
  callId: CallId('image-check-call'),
  name: 'execute_skill_workflow',
  arguments: {},
  agent,
})
await new Promise((resolve) => setImmediate(resolve))
assert.equal(ctx.workflowEngine.requests.length, 1)
assert.equal(ctx.workflowEngine.requests[0].args.skillName, 'warehouse-audit')
assert.equal(ctx.workflowEngine.requests[0].args.phases[0].workers[0].workerId, 'inspector')
ctx.workflowEngine.agentStart({
  seq: 1,
  label: 'inspector',
  phase: 'phase-0',
  childId: SessionId('child-image-check'),
})
ctx.workflowEngine.agentEnd({
  seq: 1,
  label: 'inspector',
  phase: 'phase-0',
  childId: SessionId('child-image-check'),
  outcome: 'completed',
})
ctx.workflowEngine.settle({
  stopReason: 'completed',
  agentsStarted: 1,
  value: {
    schema: 'chatds.deepseek-skill-workflow-result.v1',
    routeSha256: 'a'.repeat(64),
    results: [{ workerId: 'inspector', handoff: 'verified inventory' }],
  },
})
const result = await pending
assert.equal(result.isError, false)
assert.deepEqual(session.events
  .map((event) => event.type)
  .filter((type) => type.startsWith('tool-workflow/')), [
  'tool-workflow/run-start',
  'tool-workflow/agent-start',
  'tool-workflow/agent-end',
  'tool-workflow/run-end',
])
assert.equal(steered.length, 0)

await unregister()
await scope.dispose()
await ctx.fiber.dispose()
process.stdout.write('native workflow candidate-image integration passed\n')
