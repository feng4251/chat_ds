import { readFileSync } from 'node:fs'

export const NATIVE_TURN_INPUT_SCHEMA = 'chatds.deepseek-native-turn.v1'
export const MAX_NATIVE_TURN_INPUT_BYTES = 64 * 1024 * 1024
export const NATIVE_PERMISSION_PRESETS = Object.freeze([
  'read-only',
  'workspace-write',
  'danger-full-access',
])
export const NATIVE_RUN_CONTROL_SCHEMA = 'chatds.native-run-control.v1'
export const NATIVE_RUN_CONTROL_ACTIONS = Object.freeze([
  'interrupt', 'followup', 'steer',
])

function exactKeys(value, keys) {
  return Object.keys(value).sort().join('\0') === [...keys].sort().join('\0')
}

export function validateNativeTurnInput(value) {
  if (
    value === null
    || typeof value !== 'object'
    || Array.isArray(value)
    || !exactKeys(value, [
      'schema', 'native_session_id', 'permission_preset',
      'initial_prompt', 'turn_prompt',
    ])
    || value.schema !== NATIVE_TURN_INPUT_SCHEMA
    || typeof value.native_session_id !== 'string'
    || !/^chatds-[0-9a-f]{32}$/.test(value.native_session_id)
    || !NATIVE_PERMISSION_PRESETS.includes(value.permission_preset)
    || typeof value.initial_prompt !== 'string'
    || value.initial_prompt.trim() === ''
    || typeof value.turn_prompt !== 'string'
    || value.turn_prompt.trim() === ''
  ) {
    throw new Error('chatds-session-driver: native Turn input is invalid')
  }
  const encoded = Buffer.from(JSON.stringify(value), 'utf8')
  if (encoded.length > MAX_NATIVE_TURN_INPUT_BYTES) {
    throw new Error('chatds-session-driver: native Turn input is too large')
  }
  return Object.freeze({
    schema: NATIVE_TURN_INPUT_SCHEMA,
    native_session_id: value.native_session_id,
    permission_preset: value.permission_preset,
    initial_prompt: value.initial_prompt,
    turn_prompt: value.turn_prompt,
  })
}

export function synchronizeNativePermissionPreset(service, session, preset) {
  if (
    service === null
    || typeof service !== 'object'
    || typeof service.current !== 'function'
    || typeof service.set !== 'function'
    || session === null
    || typeof session !== 'object'
    || !Array.isArray(session.events)
    || !NATIVE_PERMISSION_PRESETS.includes(preset)
  ) {
    throw new Error('chatds-session-driver: native permission boundary is invalid')
  }
  if (service.current(session.events) !== preset) {
    service.set(session, preset)
  }
  if (service.current(session.events) !== preset) {
    throw new Error('chatds-session-driver: native permission preset was not applied')
  }
}

export function readNativeTurnInput(path) {
  if (path !== '/runtime/controller/native-turn.json') {
    throw new Error('chatds-session-driver: native Turn input path is invalid')
  }
  let bytes
  try {
    bytes = readFileSync(path)
  } catch {
    throw new Error('chatds-session-driver: native Turn input is unavailable')
  }
  if (bytes.length === 0 || bytes.length > MAX_NATIVE_TURN_INPUT_BYTES) {
    throw new Error('chatds-session-driver: native Turn input size is invalid')
  }
  try {
    return validateNativeTurnInput(JSON.parse(bytes.toString('utf8')))
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('chatds-session-driver:')) {
      throw error
    }
    throw new Error('chatds-session-driver: native Turn input is malformed')
  }
}

export function selectNativeTurnPrompt(input, persisted) {
  const validated = validateNativeTurnInput(input)
  if (typeof persisted !== 'boolean') {
    throw new Error('chatds-session-driver: persistence state is invalid')
  }
  return persisted ? validated.turn_prompt : validated.initial_prompt
}

export function validateNativeRunControl(value) {
  if (
    value === null
    || typeof value !== 'object'
    || Array.isArray(value)
    || !exactKeys(value, ['schema', 'control_id', 'seq', 'action', 'text'])
    || value.schema !== NATIVE_RUN_CONTROL_SCHEMA
    || typeof value.control_id !== 'string'
    || !/^[0-9a-f]{32}$/.test(value.control_id)
    || !Number.isSafeInteger(value.seq)
    || value.seq < 1
    || value.seq > 4096
    || !NATIVE_RUN_CONTROL_ACTIONS.includes(value.action)
    || (
      value.action === 'interrupt'
        ? value.text !== null
        : typeof value.text !== 'string'
          || value.text.trim() === ''
          || value.text.length > 2_000_000
          || value.text.includes('\0')
    )
  ) {
    throw new Error('chatds-session-driver: native run control is invalid')
  }
  return Object.freeze({
    schema: NATIVE_RUN_CONTROL_SCHEMA,
    control_id: value.control_id,
    seq: value.seq,
    action: value.action,
    text: value.text,
  })
}

export function applyNativeRunControl(agent, value, createMessage) {
  const control = validateNativeRunControl(value)
  if (
    agent === null
    || typeof agent !== 'object'
    || typeof agent.cancel !== 'function'
    || typeof agent.followup !== 'function'
    || typeof agent.steer !== 'function'
    || typeof createMessage !== 'function'
  ) {
    throw new Error('chatds-session-driver: native run control boundary is invalid')
  }
  if (control.action === 'interrupt') {
    const wasRunning = agent.status === 'running'
    agent.cancel({ kind: 'user' }, { keepInbox: true })
    if (wasRunning && agent.inbox !== undefined) {
      const inbox = agent.inbox
      if (
        inbox === null
        || !Array.isArray(inbox.nextTurn)
        || !Array.isArray(inbox.nextStep)
        || typeof inbox.remove !== 'function'
      ) {
        throw new Error('chatds-session-driver: native inbox boundary is invalid')
      }
      // A follow-up queued before cancellation is already durable, but the
      // upstream wake latch is armed only by input sent after the activity's
      // AbortSignal changes state. Move one still-pending message through the
      // public Inbox/Agent APIs after cancel: this preserves every identity
      // and ordering edge while ensuring the native driver opens its next
      // Turn instead of letting process teardown discard the queue.
      const wakeMessage = inbox.nextTurn.at(-1) ?? inbox.nextStep.at(-1)
      if (wakeMessage !== undefined) {
        if (
          typeof wakeMessage?.id !== 'string'
          || typeof agent.send !== 'function'
          || !inbox.remove(wakeMessage.id)
        ) {
          throw new Error('chatds-session-driver: native inbox wake failed')
        }
        agent.send(wakeMessage, 'next-turn', true)
      }
    }
    return control
  }
  const message = createMessage({
    content: [{ type: 'text', text: control.text }],
    source: { kind: 'user' },
  })
  if (control.action === 'followup') agent.followup(message)
  else agent.steer(message)
  return control
}

export function summarizeNativeInterval(events, firstSeq) {
  if (!Array.isArray(events) || !Number.isSafeInteger(firstSeq) || firstSeq < 0) {
    throw new Error('chatds-session-driver: native interval is invalid')
  }
  let started = false
  let text = ''
  let reason
  for (const event of events) {
    if (event === null || typeof event !== 'object' || event.seq < firstSeq) continue
    if (event.type === 'turn/start') {
      started = true
      continue
    }
    if (!started) continue
    if (event.type === 'assistant/message') {
      const content = event.data?.message?.content
      if (Array.isArray(content)) {
        const joined = content
          .filter((block) => block?.type === 'text')
          .map((block) => String(block.text ?? ''))
          .join('')
        if (joined !== '') text = joined
      }
    }
    if (event.type === 'turn/end') reason = event.data?.reason
  }
  return { text, reason }
}
