/** Native DSH projection for one immutable Skill-declared worker workflow. */

import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { defineTool } from '@deepseek-ai/dsh-tools'

import {
  compileWorkflowProgram,
  loadWorkerSources,
  readWorkflowProjection,
  validateWorkflowResult,
} from './native_workflow_core.mjs'


export const name = 'chatds-native-skill-workflow'
export const inject = ['workflowEngine', 'systemPrompt', 'tools']
const TOOL_NAME = 'execute_skill_workflow'
const SOURCE = Object.freeze({ kind: 'plugin', plugin: name })
const MAX_RENDER_CHARS = 240000

function appendRunRecord(session, type, data) {
  session.append(type, data)
}

function createRecorder(ctx) {
  const active = new Map()
  ctx.on('workflow/agent-start', (info, agent) => {
    const session = active.get(info.id)
    if (session === undefined) return
    appendRunRecord(session, 'tool-workflow/agent-start', {
      runId: info.id,
      seq: agent.seq,
      label: agent.label,
      ...(agent.phase === undefined ? {} : { phase: agent.phase }),
      childId: agent.childId,
    })
  })
  ctx.on('workflow/agent-end', (info, agent) => {
    const session = active.get(info.id)
    if (session === undefined) return
    appendRunRecord(session, 'tool-workflow/agent-end', {
      runId: info.id,
      seq: agent.seq,
      outcome: agent.outcome,
    })
  })
  return {
    start(session, run) {
      appendRunRecord(session, 'tool-workflow/run-start', {
        runId: run.id,
        name: run.meta.name,
      })
      active.set(run.id, session)
    },
    finish(run, stopReason) {
      const session = active.get(run.id)
      if (session !== undefined) {
        appendRunRecord(session, 'tool-workflow/run-end', {
          runId: run.id,
          stopReason,
        })
      }
      active.delete(run.id)
    },
    abandon(run) {
      active.delete(run.id)
    },
  }
}

function renderResult(value) {
  const rendered = JSON.stringify(value, null, 2)
  if (rendered.length <= MAX_RENDER_CHARS) return rendered
  return `${rendered.slice(0, MAX_RENDER_CHARS)}\n… [bounded workflow handoff]`
}

function rootTool(ctx, projection, program, recorder, state) {
  return defineTool({
    name: TOOL_NAME,
    description: 'Execute the activated Skill\'s immutable mandatory worker topology with native DSH subagents, exact phase barriers, and bounded failed-member retry.',
    parameters: {},
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          runId: { type: 'string', required: true },
          agentsStarted: { type: 'integer', required: true },
          result: { type: 'json', required: true },
        },
      },
      render: (_args, value) => [{ type: 'text', text: renderResult(value.result) }],
    },
    async execute(_args, exec) {
      const parent = exec.agent
      if (
        parent === undefined
        || String(parent.session.id) !== projection.native_session_id
      ) throw new Error('chatds-native-workflow: projected tool is root-Session only')
      if (state.status !== 'pending') {
        throw new Error(`chatds-native-workflow: workflow is already ${state.status}`)
      }
      state.status = 'running'
      let run
      let settled
      const onAbort = () => { run?.cancel('parent step aborted') }
      try {
        run = ctx.workflowEngine.start({
          ...program,
          parent,
          signal: exec.signal,
        })
        recorder.start(parent.session, run)
        exec.signal.addEventListener('abort', onAbort, { once: true })
        settled = await run.result
        if (settled.stopReason !== 'completed') {
          throw new Error(
            `chatds-native-workflow: native workflow ${settled.stopReason}`,
          )
        }
        const result = validateWorkflowResult(settled.value, projection)
        state.status = 'passed'
        return {
          runId: run.id,
          agentsStarted: settled.agentsStarted,
          result,
        }
      } catch (error) {
        state.status = 'failed'
        throw error
      } finally {
        exec.signal.removeEventListener('abort', onAbort)
        if (run !== undefined) {
          try {
            await run.dispose()
            recorder.finish(run, settled?.stopReason ?? 'error')
          } finally {
            recorder.abandon(run)
          }
        }
      }
    },
  })
}

export function apply(ctx) {
  const projectionPath = process.env.CHATDS_DSH_WORKFLOW_PROJECTION
  if (projectionPath === undefined || projectionPath === '') return
  const projection = readWorkflowProjection(projectionPath)
  const sources = loadWorkerSources(projection)
  const program = compileWorkflowProgram(projection, sources)
  const recorder = createRecorder(ctx)
  const states = new Map()

  ctx.on('agent/created', ({ agent }) => {
    if (String(agent.session.id) !== projection.native_session_id) return
    const state = { status: 'pending', continuationCount: 0 }
    states.set(projection.native_session_id, state)
    agent.ctx.inject(['systemPrompt', 'tools'], (agentCtx) => {
      agentCtx.systemPrompt.section({
        name: 'chatds:activated-skill-workflow',
        order: 117,
        text: `This Turn activates the immutable Skill workflow ${projection.skill_name}/${projection.route_id}. Before synthesizing artifacts or giving the final answer, call ${TOOL_NAME} exactly once with no arguments. It is the only authoritative executor for the declared worker topology; wait for its native result and use every returned handoff.`,
      })
      agentCtx.tools.register(rootTool(ctx, projection, program, recorder, state))
    })
  })

  ctx.on('agent/turn-stopping', async ({ agent }) => {
    if (String(agent.session.id) !== projection.native_session_id) return
    const state = states.get(projection.native_session_id)
    if (state?.status === 'passed') return
    if (state?.status === 'pending' && state.continuationCount === 0) {
      state.continuationCount += 1
      agent.steer(createUserMessage({
        content: [{
          type: 'text',
          text: `Machine workflow receipt is missing. Call ${TOOL_NAME} now with no arguments, wait for every mandatory phase, then continue synthesis from its returned handoffs.`,
        }],
        source: SOURCE,
      }))
      return
    }
    throw new Error(
      state?.status === 'failed'
        ? 'chatds-native-workflow: mandatory native workflow failed'
        : 'chatds-native-workflow: mandatory native workflow was not executed',
    )
  })
}
