/** Thin Session/I/O driver over the pinned DeepSeek Harness agent loop. */

import { installModelSelection } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import { createHash, randomUUID } from 'node:crypto'
import {
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { join } from 'node:path'

import {
  readNativeTurnInput,
  applyNativeRunControl,
  selectNativeTurnPrompt,
  synchronizeNativePermissionPreset,
  summarizeNativeInterval,
} from './session_driver_core.mjs'

const CONTROL_RECEIPT_SCHEMA = 'chatds.native-run-control-receipt.v1'

function canonical(value) {
  const ordered = Object.fromEntries(
    Object.keys(value).sort().map((key) => [key, value[key]]),
  )
  return JSON.stringify(ordered)
}

function readControls(root) {
  const requests = join(root, 'requests')
  if (!existsSync(requests)) return []
  const names = readdirSync(requests)
  if (names.length > 4096) {
    throw new Error('chatds-session-driver: native run control count exceeded')
  }
  return names
    .filter((name) => /^[0-9a-f]{32}\.json$/.test(name))
    .map((name) => JSON.parse(readFileSync(join(requests, name), 'utf8')))
    .sort((left, right) => left.seq - right.seq || left.control_id.localeCompare(right.control_id))
}

function writeControlReceipt(root, request, status, code = null) {
  const receipts = join(root, 'receipts')
  mkdirSync(receipts, { recursive: true, mode: 0o700 })
  const path = join(receipts, `${request.control_id}.json`)
  if (existsSync(path)) return
  const receipt = {
    schema: CONTROL_RECEIPT_SCHEMA,
    control_id: request.control_id,
    seq: request.seq,
    action: request.action,
    request_sha256: createHash('sha256').update(canonical(request)).digest('hex'),
    status,
    code,
    recorded_at_unix_ms: Date.now(),
  }
  const temporary = join(receipts, `.${request.control_id}.${randomUUID()}.tmp`)
  const descriptor = openSync(temporary, 'wx', 0o600)
  try {
    writeFileSync(descriptor, `${canonical(receipt)}\n`, 'utf8')
    fsyncSync(descriptor)
  } finally {
    closeSync(descriptor)
  }
  try {
    linkSync(temporary, path)
  } finally {
    try { unlinkSync(temporary) } catch (error) {
      if (error?.code !== 'ENOENT') throw error
    }
  }
  const directory = openSync(receipts, 'r')
  try {
    fsyncSync(directory)
  } finally {
    closeSync(directory)
  }
}

function drainNativeControls(agent, root) {
  let delivered = 0
  for (const request of readControls(root)) {
    if (existsSync(join(root, 'receipts', `${request.control_id}.json`))) continue
    try {
      const control = applyNativeRunControl(agent, request, createUserMessage)
      writeControlReceipt(root, control, 'delivered')
      delivered += 1
    } catch {
      writeControlReceipt(root, request, 'rejected', 'native_control_rejected')
    }
  }
  return delivered
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))

async function driveUntilIdle(agent, controlRoot) {
  let idleSince = null
  while (true) {
    const delivered = drainNativeControls(agent, controlRoot)
    if (agent.status === 'idle' && delivered === 0) {
      idleSince ??= Date.now()
      if (Date.now() - idleSince >= 250) break
    } else {
      idleSince = null
    }
    await Promise.race([agent.whenIdle(), delay(50)])
  }
  drainNativeControls(agent, controlRoot)
}

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
  const controlRoot = process.env.CHATDS_DSH_RUN_CONTROLS
  if (controlRoot !== '/runtime/worker/run-controls') {
    throw new Error('chatds-session-driver: native run control path is invalid')
  }
  await driveUntilIdle(agent, controlRoot)
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
