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

/**
 * Own the refresh heartbeat independently from any one React render or live
 * request.  Every completed (or failed) cycle rearms itself.  `wake()`
 * invalidates the prior timer and requests an immediate authoritative read;
 * this is used after SSE release and browser lifecycle transitions.
 */
export function createSessionRefreshLoop({
  request,
  getDelay = () => 5000,
  initialDelay = 5000,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
}) {
  let stopped = false
  let timer = null
  let generation = 0

  const arm = (delay = getDelay()) => {
    if (stopped) return
    if (timer !== null) clearTimer(timer)
    timer = setTimer(() => {
      timer = null
      void run(false)
    }, Math.max(0, Number(delay) || 0))
  }

  const run = async (forceFull) => {
    if (stopped) return
    const cycle = ++generation
    try {
      await request(Boolean(forceFull))
    } catch {
      // Reconciliation is best-effort at the transport edge.  The durable
      // projection remains authority and the next heartbeat must still run.
    } finally {
      if (!stopped && cycle === generation) arm()
    }
  }

  return {
    start() {
      arm(initialDelay)
    },
    wake() {
      if (stopped) return
      if (timer !== null) {
        clearTimer(timer)
        timer = null
      }
      void run(true)
    },
    stop() {
      stopped = true
      generation += 1
      if (timer !== null) clearTimer(timer)
      timer = null
    },
  }
}

/** Keep the user's pre-update scroll intent, not the post-append distance. */
export function shouldFollowMessageUpdate(wasPinnedToBottom, isStreaming) {
  return Boolean(wasPinnedToBottom || isStreaming)
}

/** Live transcript movement must not trigger repeated animated page travel. */
export function messageUpdateScrollBehavior(isStreaming, runActive) {
  return isStreaming || runActive ? 'auto' : 'smooth'
}

/** Empty active-run polls are not state updates. */
export function sessionProjectionHasDelta(
  activities,
  runProjectionChanged,
) {
  return Boolean(
    runProjectionChanged
    || (Array.isArray(activities?.events) && activities.events.length > 0)
  )
}
