/** Thin Session/I/O driver over the pinned DeepSeek Harness agent loop. */

import { installModelSelection } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'

import {
  readNativeTurnInput,
  selectNativeTurnPrompt,
  synchronizeNativePermissionPreset,
  summarizeNativeInterval,
} from './session_driver_core.mjs'

export const name = 'chatds-session-driver'
export const inject = [
  'agentDefaultModel',
  'agents',
  'permissionPresets',
  'sessions',
  'sessionPersistence',
]

function fail(io, error) {
  io.stderr.write(`dsh: ${error instanceof Error ? error.message : String(error)}\n`)
  io.exit(1)
}

async function run(ctx, io) {
  await ctx.get('loader')?.await()
  const agents = ctx.get('agents')
  const defaultModel = ctx.get('agentDefaultModel')
  const permissionPresets = ctx.get('permissionPresets')
  const sessions = ctx.get('sessions')
  const persistence = ctx.get('sessionPersistence')
  if (
    agents === undefined
    || defaultModel === undefined
    || permissionPresets === undefined
    || sessions === undefined
    || persistence === undefined
  ) return

  const input = readNativeTurnInput(process.env.CHATDS_DSH_TURN_INPUT)
  const sessionId = SessionId(input.native_session_id)
  const headers = await persistence.list()
  if (!Array.isArray(headers) || headers.length > 100_000) {
    throw new Error('chatds-session-driver: persistence catalog is invalid')
  }
  const persisted = headers.some((header) => String(header?.id ?? '') === sessionId)
  const selection = defaultModel.currentSelection()
  const setup = (agentCtx) => {
    const selected = { current: selection, assembled: undefined }
    installModelSelection(agentCtx, selected)
  }
  const handle = persisted
    ? await agents.resume({
        resumeSessionId: sessionId,
        agentOptions: { provider: selection.provider, model: selection.model },
        setup,
      })
    : await agents.create({
        sessionId,
        meta: { cwd: process.cwd() },
        agentOptions: { provider: selection.provider, model: selection.model },
        setup,
      })
  const agent = handle.agent
  await agent.whenIdle()
  synchronizeNativePermissionPreset(
    permissionPresets,
    agent.session,
    input.permission_preset,
  )
  const firstSeq = agent.session.seq
  const prompt = selectNativeTurnPrompt(input, persisted)
  agent.followup(createUserMessage({
    content: [{ type: 'text', text: prompt }],
    source: { kind: 'user' },
  }))
  await agent.whenIdle()
  await sessions.flush(agent.session)
  const outcome = summarizeNativeInterval(agent.session.events, firstSeq)
  io.stdout.write(outcome.text + '\n')
  if (outcome.reason?.kind === 'error') {
    io.stderr.write(
      `dsh: ${outcome.reason.error?.code ?? 'error'}: ${outcome.reason.error?.message ?? 'native Turn failed'}\n`,
    )
  }
  io.exit(outcome.reason?.kind === 'completed' ? 0 : 1)
}

export function apply(ctx) {
  const exit = ctx.get('appExit')
  if (exit === undefined) {
    throw new Error('chatds-session-driver: launcher appExit is unavailable')
  }
  const io = { stdout: process.stdout, stderr: process.stderr, exit }
  void run(ctx, io).catch((error) => { fail(io, error) })
}
