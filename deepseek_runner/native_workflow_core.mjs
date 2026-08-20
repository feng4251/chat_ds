import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { resolve, sep } from 'node:path'


export const WORKFLOW_PROJECTION_SCHEMA = 'chatds.deepseek-skill-workflow.v1'
export const MAX_WORKFLOW_PROJECTION_BYTES = 40 * 1024 * 1024
export const MAX_WORKER_SOURCE_BYTES = 2 * 1024 * 1024
export const MAX_TOTAL_WORKER_SOURCE_BYTES = 32 * 1024 * 1024
export const MAX_PHASES = 128
export const MAX_WORKERS = 128
export const MAX_USER_TURN_BYTES = 2 * 1024 * 1024
export const MAX_HANDOFF_CHARS = 24000
export const MAX_ATTEMPTS_PER_WORKER = 2

const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const SAFE_SHA256 = /^[0-9a-f]{64}$/
const SAFE_NATIVE_SESSION_ID = /^chatds-[0-9a-f]{32}$/

function exactKeys(value, keys) {
  return Object.keys(value).sort().join('\0') === [...keys].sort().join('\0')
}

function safeRelativePath(value) {
  if (
    typeof value !== 'string'
    || value === ''
    || value.startsWith('/')
    || value.includes('\\')
    || value.includes('\0')
    || Buffer.byteLength(value, 'utf8') > 1024
    || value.split('/').some((part) => part === '' || part === '.' || part === '..')
  ) throw new Error('chatds-native-workflow: workflow projection is invalid')
  return value
}

function cloneWorker(worker, observed) {
  if (
    worker === null
    || typeof worker !== 'object'
    || Array.isArray(worker)
    || !exactKeys(worker, [
      'worker_id', 'source_path', 'source_sha256', 'source_size',
    ])
    || typeof worker.worker_id !== 'string'
    || !SAFE_NAME.test(worker.worker_id)
    || observed.has(worker.worker_id)
    || typeof worker.source_sha256 !== 'string'
    || !SAFE_SHA256.test(worker.source_sha256)
    || !Number.isSafeInteger(worker.source_size)
    || worker.source_size < 0
    || worker.source_size > MAX_WORKER_SOURCE_BYTES
  ) throw new Error('chatds-native-workflow: workflow projection is invalid')
  observed.add(worker.worker_id)
  return Object.freeze({
    worker_id: worker.worker_id,
    source_path: safeRelativePath(worker.source_path),
    source_sha256: worker.source_sha256,
    source_size: worker.source_size,
  })
}

export function validateWorkflowProjection(value) {
  if (
    value === null
    || typeof value !== 'object'
    || Array.isArray(value)
    || !exactKeys(value, [
      'schema', 'native_session_id', 'skill_name', 'route_id',
      'route_sha256', 'run_name', 'user_turn_text',
      'max_attempts_per_worker', 'handoff_max_chars', 'phases',
    ])
    || value.schema !== WORKFLOW_PROJECTION_SCHEMA
    || typeof value.native_session_id !== 'string'
    || !SAFE_NATIVE_SESSION_ID.test(value.native_session_id)
    || typeof value.skill_name !== 'string'
    || !SAFE_NAME.test(value.skill_name)
    || typeof value.route_id !== 'string'
    || !SAFE_NAME.test(value.route_id)
    || typeof value.route_sha256 !== 'string'
    || !SAFE_SHA256.test(value.route_sha256)
    || value.run_name !== `skill-workflow-${value.route_sha256.slice(0, 16)}`
    || typeof value.user_turn_text !== 'string'
    || value.user_turn_text.trim() === ''
    || Buffer.byteLength(value.user_turn_text, 'utf8') > MAX_USER_TURN_BYTES
    || value.max_attempts_per_worker !== MAX_ATTEMPTS_PER_WORKER
    || value.handoff_max_chars !== MAX_HANDOFF_CHARS
    || !Array.isArray(value.phases)
    || value.phases.length === 0
    || value.phases.length > MAX_PHASES
  ) throw new Error('chatds-native-workflow: workflow projection is invalid')

  const observed = new Set()
  let totalBytes = 0
  const phases = value.phases.map((phase, phaseIndex) => {
    if (
      phase === null
      || typeof phase !== 'object'
      || Array.isArray(phase)
      || !exactKeys(phase, ['mode', 'phase_id', 'workers'])
      || !['parallel', 'sequential'].includes(phase.mode)
      || phase.phase_id !== `phase-${phaseIndex}`
      || !Array.isArray(phase.workers)
      || phase.workers.length === 0
      || phase.workers.length > MAX_WORKERS
      || (phase.mode === 'sequential' && phase.workers.length !== 1)
    ) throw new Error('chatds-native-workflow: workflow projection is invalid')
    const workers = phase.workers.map((worker) => {
      const normalized = cloneWorker(worker, observed)
      totalBytes += normalized.source_size
      if (totalBytes > MAX_TOTAL_WORKER_SOURCE_BYTES) {
        throw new Error('chatds-native-workflow: workflow projection is invalid')
      }
      return normalized
    })
    return Object.freeze({
      mode: phase.mode,
      phase_id: phase.phase_id,
      workers: Object.freeze(workers),
    })
  })
  return Object.freeze({
    schema: WORKFLOW_PROJECTION_SCHEMA,
    native_session_id: value.native_session_id,
    skill_name: value.skill_name,
    route_id: value.route_id,
    route_sha256: value.route_sha256,
    run_name: value.run_name,
    user_turn_text: value.user_turn_text,
    max_attempts_per_worker: value.max_attempts_per_worker,
    handoff_max_chars: value.handoff_max_chars,
    phases: Object.freeze(phases),
  })
}

export function readWorkflowProjection(path) {
  if (path !== '/runtime/controller/native-workflow.json') {
    throw new Error('chatds-native-workflow: workflow projection path is invalid')
  }
  let bytes
  try {
    bytes = readFileSync(path)
  } catch {
    throw new Error('chatds-native-workflow: workflow projection is unavailable')
  }
  if (bytes.length === 0 || bytes.length > MAX_WORKFLOW_PROJECTION_BYTES) {
    throw new Error('chatds-native-workflow: workflow projection size is invalid')
  }
  try {
    return validateWorkflowProjection(JSON.parse(bytes.toString('utf8')))
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('chatds-native-workflow:')) {
      throw error
    }
    throw new Error('chatds-native-workflow: workflow projection is malformed')
  }
}

export function loadWorkerSources(projection, root = '/skill-view/plugin/skills') {
  const contract = validateWorkflowProjection(projection)
  const absoluteRoot = resolve(root)
  const skillRoot = resolve(absoluteRoot, contract.skill_name)
  if (skillRoot !== absoluteRoot && !skillRoot.startsWith(`${absoluteRoot}${sep}`)) {
    throw new Error('chatds-native-workflow: worker source root is invalid')
  }
  const sources = new Map()
  let totalBytes = 0
  for (const phase of contract.phases) {
    for (const worker of phase.workers) {
      const path = resolve(skillRoot, worker.source_path)
      if (path !== skillRoot && !path.startsWith(`${skillRoot}${sep}`)) {
        throw new Error('chatds-native-workflow: worker source path is invalid')
      }
      let bytes
      try {
        bytes = readFileSync(path)
      } catch {
        throw new Error('chatds-native-workflow: worker source is unavailable')
      }
      totalBytes += bytes.length
      if (
        bytes.length !== worker.source_size
        || bytes.length > MAX_WORKER_SOURCE_BYTES
        || totalBytes > MAX_TOTAL_WORKER_SOURCE_BYTES
        || createHash('sha256').update(bytes).digest('hex') !== worker.source_sha256
      ) throw new Error('chatds-native-workflow: worker source digest mismatch')
      sources.set(worker.worker_id, bytes.toString('utf8'))
    }
  }
  return sources
}

// The business task and worker instructions ride as immutable JSON args. This
// body is intentionally fixture-free: it expresses only barriers, bounded
// failed-member retry, dependency handoff, and fan-in semantics.
export const WORKFLOW_SCRIPT = `
const completed = []
const compact = (value) => {
  const rendered = typeof value === 'string' ? value : JSON.stringify(value)
  return rendered.slice(0, args.handoffMaxChars)
}
const priorHandoffs = () => JSON.stringify(completed)
const workerPrompt = (worker, attempt) => [
  'Execute exactly one immutable Skill worker role inside a native workflow.',
  'Load the named Skill with the native Skill tool when supporting resources are needed.',
  'Follow the embedded worker instruction source completely. Do not call the root-only projected workflow tool.',
  'Do not claim success when a provider, retrieval, or tool call failed; report the failure explicitly.',
  'Return a concise evidence handoff to the parent, bounded to the requested handoff size.',
  'Skill: ' + args.skillName,
  'Worker: ' + worker.workerId,
  'Attempt: ' + attempt + ' of ' + args.maxAttempts,
  'Current user task (verbatim):\n' + args.userTurnText,
  'Completed predecessor handoffs (machine ordered):\n' + priorHandoffs(),
  'Authoritative worker instruction source:\n' + worker.instructions,
].join('\n\n')
for (const phaseSpec of args.phases) {
  phase(phaseSpec.phaseId)
  let pending = [...phaseSpec.workers]
  for (let attempt = 1; attempt <= args.maxAttempts && pending.length > 0; attempt += 1) {
    const values = phaseSpec.mode === 'parallel'
      ? await parallel(pending.map((worker) => () => agent(
          workerPrompt(worker, attempt),
          { label: worker.workerId, phase: phaseSpec.phaseId },
        )))
      : [await agent(
          workerPrompt(pending[0], attempt),
          { label: pending[0].workerId, phase: phaseSpec.phaseId },
        )]
    const retry = []
    for (let index = 0; index < pending.length; index += 1) {
      const value = values[index]
      if (value === null || compact(value).trim() === '') {
        retry.push(pending[index])
      } else {
        completed.push({ workerId: pending[index].workerId, handoff: compact(value) })
      }
    }
    pending = retry
  }
  if (pending.length > 0) {
    throw new Error('mandatory workflow frontier did not settle after bounded failed-member retry')
  }
}
return {
  schema: 'chatds.deepseek-skill-workflow-result.v1',
  routeSha256: args.routeSha256,
  results: completed,
}
`

export function compileWorkflowProgram(projection, sources) {
  const contract = validateWorkflowProjection(projection)
  if (!(sources instanceof Map)) {
    throw new Error('chatds-native-workflow: worker sources are invalid')
  }
  const phases = contract.phases.map((phase) => ({
    mode: phase.mode,
    phaseId: phase.phase_id,
    workers: phase.workers.map((worker) => {
      const instructions = sources.get(worker.worker_id)
      if (typeof instructions !== 'string' || instructions.trim() === '') {
        throw new Error('chatds-native-workflow: worker sources are invalid')
      }
      return {
        workerId: worker.worker_id,
        instructions,
      }
    }),
  }))
  return Object.freeze({
    script: WORKFLOW_SCRIPT,
    meta: Object.freeze({
      name: contract.run_name,
      description: 'Execute the activated immutable Skill worker topology.',
      phases: Object.freeze(contract.phases.map((phase) => Object.freeze({
        title: phase.phase_id,
      }))),
    }),
    args: Object.freeze({
      skillName: contract.skill_name,
      routeSha256: contract.route_sha256,
      userTurnText: contract.user_turn_text,
      maxAttempts: contract.max_attempts_per_worker,
      handoffMaxChars: contract.handoff_max_chars,
      phases,
    }),
    maxTotalAgents: contract.phases.reduce(
      (count, phase) => count + phase.workers.length,
      0,
    ) * contract.max_attempts_per_worker,
  })
}

export function validateWorkflowResult(value, projection) {
  const contract = validateWorkflowProjection(projection)
  const expected = new Set(
    contract.phases.flatMap((phase) => phase.workers.map((worker) => worker.worker_id)),
  )
  if (
    value === null
    || typeof value !== 'object'
    || Array.isArray(value)
    || !exactKeys(value, ['schema', 'routeSha256', 'results'])
    || value.schema !== 'chatds.deepseek-skill-workflow-result.v1'
    || value.routeSha256 !== contract.route_sha256
    || !Array.isArray(value.results)
    || value.results.length !== expected.size
  ) throw new Error('chatds-native-workflow: native workflow result is invalid')
  const observed = new Set()
  for (const row of value.results) {
    if (
      row === null
      || typeof row !== 'object'
      || Array.isArray(row)
      || !exactKeys(row, ['workerId', 'handoff'])
      || !expected.has(row.workerId)
      || observed.has(row.workerId)
      || typeof row.handoff !== 'string'
      || row.handoff.trim() === ''
      || row.handoff.length > contract.handoff_max_chars
    ) throw new Error('chatds-native-workflow: native workflow result is invalid')
    observed.add(row.workerId)
  }
  return value
}
