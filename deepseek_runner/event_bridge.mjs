import { acquireNativeEventPublisher } from './native_event_transport.mjs'

export const name = 'chatds-event-bridge'

export async function apply(ctx) {
  const publish = await acquireNativeEventPublisher(ctx)
  ctx.on('session/event', (session, event) => {
    publish({
      type: 'deepseek.session.event',
      session_id: String(session.id),
      origin: session.header.origin,
      parent_session_id: session.header.parentSession === undefined
        ? undefined
        : String(session.header.parentSession),
      delegation_depth: session.header.delegationDepth ?? 0,
      session_event: event,
    })
  })
}
