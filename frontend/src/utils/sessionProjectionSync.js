const MESSAGE_BOUNDARY_FIELDS = [
  'root_run_id',
  'trigger_message_id',
  'assistant_message_id',
  'mapping_status',
  'active',
  'status',
]

/**
 * Return the part of a run-card projection that can add, remove, or remap a
 * durable chat message. Tool/event progress is intentionally excluded: it is
 * rendered from run cards without downloading the entire transcript again.
 */
export function runCardMessageRevision(payload = {}) {
  return JSON.stringify((payload.roots || []).map((root) => (
    MESSAGE_BOUNDARY_FIELDS.map((field) => root?.[field] ?? null)
  )))
}

/** Exact revision for the bounded run-card DTO used by the current page. */
export function runCardProjectionRevision(payload = {}) {
  return JSON.stringify(payload || {})
}

/** Messages are immutable rows; retain exactness for unusual update paths. */
export function messageProjectionRevision(messages = []) {
  return JSON.stringify(messages || [])
}

/**
 * Serialize refresh requests from timers, focus/online events, and durable-run
 * activity. A forced request arriving during an in-flight read is retained and
 * executed once afterwards. This prevents request storms and stale overlap.
 */
export function createSessionRefreshCoordinator({
  refresh,
  canRefresh = () => true,
  onError = () => {},
}) {
  let stopped = false
  let inFlight = null
  let pending = false
  let pendingForce = false

  const request = (forceFull = false) => {
    if (stopped || !canRefresh()) return Promise.resolve(undefined)
    if (inFlight) {
      pending = true
      pendingForce = pendingForce || Boolean(forceFull)
      return inFlight
    }

    const execute = async () => {
      try {
        return await refresh({ forceFull: Boolean(forceFull) })
      } catch (error) {
        onError(error)
        return undefined
      } finally {
        inFlight = null
        if (pending && !stopped) {
          const nextForce = pendingForce
          pending = false
          pendingForce = false
          await request(nextForce)
        }
      }
    }
    inFlight = execute()
    return inFlight
  }

  return {
    request,
    stop() {
      stopped = true
      pending = false
      pendingForce = false
    },
    async whenIdle() {
      while (inFlight) await inFlight
    },
  }
}
