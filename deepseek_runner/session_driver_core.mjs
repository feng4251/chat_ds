import { readFileSync } from 'node:fs'

export const NATIVE_TURN_INPUT_SCHEMA = 'chatds.deepseek-native-turn.v1'
export const MAX_NATIVE_TURN_INPUT_BYTES = 64 * 1024 * 1024
export const NATIVE_PERMISSION_PRESETS = Object.freeze([
  'read-only',
  'workspace-write',
  'danger-full-access',
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
