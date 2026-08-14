import { closeSync, fsyncSync, openSync, writeSync } from 'node:fs'

export const name = 'chatds-event-bridge'

function append(path, envelope) {
  const bytes = Buffer.from(`${JSON.stringify(envelope)}\n`, 'utf8')
  const fd = openSync(path, 'a', 0o600)
  try {
    let offset = 0
    while (offset < bytes.length) offset += writeSync(fd, bytes, offset)
    fsyncSync(fd)
  } finally {
    closeSync(fd)
  }
}

export function apply(ctx) {
  const ledger = process.env.CHATDS_EVENT_LEDGER
  if (!ledger) throw new Error('chatds-event-bridge: CHATDS_EVENT_LEDGER is unavailable')
  ctx.on('session/event', (session, event) => {
    append(ledger, {
      received_at_unix_ms: Date.now(),
      channel: 'deepseek-harness',
      event: {
        type: 'deepseek.session.event',
        session_id: String(session.id),
        origin: session.header.origin,
        parent_session_id: session.header.parentSession === undefined
          ? undefined
          : String(session.header.parentSession),
        delegation_depth: session.header.delegationDepth ?? 0,
        session_event: event,
      },
    })
  })
}
