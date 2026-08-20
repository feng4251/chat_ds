/**
 * ChatDS-owned compatibility middleware for OpenAI-compatible provider
 * streams consumed by the unmodified DeepSeek Harness runtime.
 *
 * OpenAI's tool-call delta shape permits later chunks to omit identity
 * fields.  Some compatible providers serialize those omissions as empty
 * strings instead.  The upstream adapter quite reasonably treats a present
 * field as authoritative, so an empty late delta can otherwise erase the
 * non-empty call id/name observed at block start.  Preserve the first
 * non-empty identity and fail closed if a provider later changes it.
 */

export const name = 'chatds-provider-compat-bridge'

function mergeIdentity(current, incoming, field) {
  if (typeof incoming !== 'string' || incoming.length === 0) return current
  if (current !== undefined && current !== incoming) {
    throw new Error(`chatds_provider_tool_${field}_changed`)
  }
  return incoming
}

/**
 * Normalize only tool identity metadata. Content, reasoning, arguments,
 * usage, finish reasons, ordering, and retries remain upstream-owned.
 */
export async function* normalizeToolIdentityStream(source) {
  const identities = new Map()
  for await (const chunk of source) {
    if (chunk?.type === 'block-start' && chunk.blockType === 'tool-call') {
      if (!identities.has(chunk.index)) identities.set(chunk.index, {})
      yield chunk
      continue
    }

    if (chunk?.type === 'tool-call-delta') {
      const identity = identities.get(chunk.index) ?? {}
      identity.id = mergeIdentity(identity.id, chunk.id, 'id')
      identity.name = mergeIdentity(identity.name, chunk.name, 'name')
      identities.set(chunk.index, identity)
      yield {
        ...chunk,
        ...(identity.id === undefined ? {} : { id: identity.id }),
        ...(identity.name === undefined ? {} : { name: identity.name }),
      }
      continue
    }

    if (chunk?.type === 'block-end' && chunk.block?.type === 'tool-call') {
      const identity = identities.get(chunk.index) ?? {}
      identity.id = mergeIdentity(identity.id, chunk.block.id, 'id')
      identity.name = mergeIdentity(identity.name, chunk.block.name, 'name')
      if (identity.id === undefined || identity.name === undefined) {
        throw new Error('chatds_provider_tool_identity_missing')
      }
      identities.set(chunk.index, identity)
      yield {
        ...chunk,
        block: {
          ...chunk.block,
          id: identity.id,
          name: identity.name,
        },
      }
      continue
    }

    yield chunk
  }
}

export function apply(ctx) {
  ctx.on('llm/stream', (_options, next) => normalizeToolIdentityStream(next()))
}
