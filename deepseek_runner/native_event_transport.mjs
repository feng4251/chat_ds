import { createConnection } from 'node:net'

const MAX_EVENT_BYTES = 16 * 1024 * 1024

let shared

async function connect() {
  const endpoint = process.env.CHATDS_EVENT_SOCKET
  if (typeof endpoint !== 'string' || !endpoint.startsWith('/runtime/controller/')) {
    throw new Error('chatds-native-events: controller socket is unavailable')
  }
  const socket = createConnection({ path: endpoint })
  const state = {
    socket,
    references: 0,
    closing: false,
    failure: undefined,
  }
  await new Promise((resolve, reject) => {
    const connected = () => {
      socket.off('error', rejected)
      resolve()
    }
    const rejected = (error) => {
      socket.off('connect', connected)
      reject(error)
    }
    socket.once('connect', connected)
    socket.once('error', rejected)
  })
  socket.on('error', () => {
    if (!state.closing) state.failure = 'native_event_transport_failed'
  })
  socket.on('close', () => {
    if (!state.closing) state.failure = 'native_event_transport_closed'
  })
  return state
}

async function close(state) {
  if (state.closing) return
  state.closing = true
  await new Promise((resolve) => {
    const timeout = setTimeout(() => {
      state.socket.destroy()
      resolve()
    }, 5_000)
    state.socket.end(() => {
      clearTimeout(timeout)
      resolve()
    })
  })
}

/**
 * Acquire the one process-scoped native event stream. The root controller
 * authenticates this socket's Linux peer PID before accepting any evidence.
 */
export async function acquireNativeEventPublisher(ctx) {
  if (shared === undefined) shared = connect()
  const state = await shared
  state.references += 1
  ctx.effect(() => async () => {
    state.references -= 1
    if (state.references === 0) await close(state)
  }, 'chatds-native-events: close authenticated stream')
  return (event) => {
    if (state.failure !== undefined || state.closing) {
      throw new Error(`chatds-native-events: ${state.failure ?? 'transport_closed'}`)
    }
    const bytes = Buffer.from(`${JSON.stringify(event)}\n`, 'utf8')
    if (bytes.length === 0 || bytes.length > MAX_EVENT_BYTES) {
      throw new Error('chatds-native-events: event size is invalid')
    }
    state.socket.write(bytes)
  }
}
