/**
 * Deployment-owned provider wire-dialect binding for the unmodified native
 * DeepSeek adapter.
 *
 * DeepSeek Harness keeps the canonical reasoning level (`max`) and its native
 * message serializer. A deployment may declare a different spelling for that
 * exact level. Only the matching provider URL, model, and JSON field are
 * rewritten; messages, tools, headers, streaming, retries, and every other
 * request remain native-owned.
 */

const SAFE_REASONING_EFFORT = /^[A-Za-z0-9._-]{1,64}$/
const CANONICAL_MAX_EFFORT = 'max'

function normalizedProfile(raw) {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('chatds_provider_wire_profile_invalid')
  }
  const baseURL = String(raw.baseURL ?? '')
  const model = String(raw.model ?? '')
  const canonicalEffort = String(raw.canonicalEffort ?? CANONICAL_MAX_EFFORT)
  const wireEffort = String(raw.wireEffort ?? '')
  if (
    model.length === 0
    || !SAFE_REASONING_EFFORT.test(canonicalEffort)
    || !SAFE_REASONING_EFFORT.test(wireEffort)
  ) {
    throw new Error('chatds_provider_wire_profile_invalid')
  }

  let endpoint
  try {
    endpoint = new URL(baseURL)
  } catch {
    throw new Error('chatds_provider_wire_profile_invalid')
  }
  if (
    !['http:', 'https:'].includes(endpoint.protocol)
    || endpoint.username.length > 0
    || endpoint.password.length > 0
    || endpoint.search.length > 0
    || endpoint.hash.length > 0
  ) {
    throw new Error('chatds_provider_wire_profile_invalid')
  }
  const basePath = endpoint.pathname.replace(/\/+$/, '')
  return Object.freeze({
    origin: endpoint.origin,
    pathname: `${basePath}/chat/completions`,
    model,
    canonicalEffort,
    wireEffort,
  })
}

function requestURL(input) {
  if (typeof input === 'string' || input instanceof URL) {
    try {
      return new URL(String(input))
    } catch {
      return null
    }
  }
  if (typeof Request !== 'undefined' && input instanceof Request) {
    try {
      return new URL(input.url)
    } catch {
      return null
    }
  }
  return null
}

/**
 * Return fetch arguments with only the declared reasoning spelling changed.
 * The result carries an explicit receipt bit for deterministic tests.
 */
function rewriteWithProfile(input, init, profile) {
  const url = requestURL(input)
  const method = String(init?.method ?? (
    typeof Request !== 'undefined' && input instanceof Request
      ? input.method
      : 'GET'
  )).toUpperCase()
  if (
    url === null
    || method !== 'POST'
    || url.origin !== profile.origin
    || url.pathname !== profile.pathname
    || url.search.length > 0
    || url.hash.length > 0
  ) {
    return { input, init, rewritten: false }
  }

  if (typeof init?.body !== 'string') {
    throw new Error('chatds_provider_wire_body_unavailable')
  }
  let body
  try {
    body = JSON.parse(init.body)
  } catch {
    throw new Error('chatds_provider_wire_body_invalid')
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('chatds_provider_wire_body_invalid')
  }
  if (
    body.model !== profile.model
    || body.reasoning_effort !== profile.canonicalEffort
  ) {
    return { input, init, rewritten: false }
  }

  return {
    input,
    init: {
      ...init,
      body: JSON.stringify({
        ...body,
        reasoning_effort: profile.wireEffort,
      }),
    },
    rewritten: true,
  }
}

export function rewriteProviderWireRequest(input, init, rawProfile) {
  return rewriteWithProfile(input, init, normalizedProfile(rawProfile))
}

/** Build a fetch-compatible wrapper without changing the native adapter. */
export function createProviderWireProfileFetch(originalFetch, rawProfile) {
  if (typeof originalFetch !== 'function') {
    throw new Error('chatds_provider_wire_fetch_unavailable')
  }
  const profile = normalizedProfile(rawProfile)
  return function profiledFetch(input, init) {
    const request = rewriteWithProfile(input, init, profile)
    return Reflect.apply(originalFetch, this, [request.input, request.init])
  }
}

/** Install the profile when this module is loaded through Node `--import`. */
export function installProviderWireProfile(environment = process.env) {
  const wireEffort = environment.CHATDS_DSH_REASONING_WIRE_EFFORT
  if (wireEffort === undefined || wireEffort === CANONICAL_MAX_EFFORT) {
    return false
  }
  globalThis.fetch = createProviderWireProfileFetch(globalThis.fetch, {
    baseURL: environment.DEEPSEEK_BASE_URL,
    model: environment.CHATDS_DSH_MODEL,
    canonicalEffort: CANONICAL_MAX_EFFORT,
    wireEffort,
  })
  return true
}

installProviderWireProfile()
