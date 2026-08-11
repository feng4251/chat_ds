const DEFAULT_RETRY_DELAYS_MS = Object.freeze([150, 400])
const RETRYABLE_HTTP_STATUSES = new Set([502, 503, 504])

function normalizedMethod(options) {
  return String(options?.method || 'GET').toUpperCase()
}

export function isIdempotentRead(options = {}) {
  return ['GET', 'HEAD'].includes(normalizedMethod(options))
}

export function isRetryableReadResponse(response) {
  return Boolean(response && RETRYABLE_HTTP_STATUSES.has(response.status))
}

export function isRetryableReadError(error, options = {}) {
  if (!isIdempotentRead(options) || options?.signal?.aborted) return false
  if (error?.name === 'AbortError') return false
  return error instanceof TypeError
}

function sleep(ms) {
  return new Promise((resolve) => globalThis.setTimeout(resolve, ms))
}

export async function fetchWithIdempotentReadRetry(
  url,
  options = {},
  {
    fetchImpl = fetch,
    retryDelaysMs = DEFAULT_RETRY_DELAYS_MS,
    sleepImpl = sleep,
  } = {},
) {
  const delays = isIdempotentRead(options) ? retryDelaysMs : []
  let attempt = 0

  while (true) {
    try {
      const response = await fetchImpl(url, options)
      if (!isRetryableReadResponse(response) || attempt >= delays.length) {
        return response
      }
    } catch (error) {
      if (!isRetryableReadError(error, options) || attempt >= delays.length) {
        throw error
      }
    }

    await sleepImpl(delays[attempt])
    attempt += 1
  }
}
