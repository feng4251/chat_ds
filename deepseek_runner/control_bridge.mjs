/**
 * ChatDS control bridge for one isolated DeepSeek Harness Turn.
 */

import { readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { acquireNativeEventPublisher } from './native_event_transport.mjs'

export const name = 'chatds-control-bridge'
export const inject = ['approval']
const POLL_INTERVAL_MS = 200

function readDecisions(path) {
  let text = ''
  try { text = readFileSync(path, 'utf8') } catch (error) {
    if (error?.code === 'ENOENT') return []
    throw error
  }
  const rows = []
  for (const line of text.split('\n')) {
    if (line.trim() === '') continue
    try {
      const value = JSON.parse(line)
      if (value && typeof value === 'object') rows.push(value)
    } catch {}
  }
  return rows
}

async function awaitDecision(decisionsPath, requestId, signal) {
  for (;;) {
    if (signal?.aborted) return undefined
    for (const row of readDecisions(decisionsPath)) {
      if (String(row.request_id ?? '') === requestId) return row
    }
    await new Promise((resolve) => { setTimeout(resolve, POLL_INTERVAL_MS) })
  }
}

/**
 * Recover the native approval audit id that DSH appended immediately before
 * dispatching `approval/request`. This is the same correlation rule used by
 * DSH's own API proxy: newest undecided, unclaimed ask with the same call id.
 */
export function findPendingApprovalId(events, request, claimed = new Set()) {
  if (!Array.isArray(events)) return undefined
  const decided = new Set()
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    const data = event?.data
    if (event?.type === 'approval/decided') {
      const id = data?.id
      if (typeof id === 'string' && id.length > 0) decided.add(id)
      continue
    }
    if (event?.type !== 'approval/asked') continue
    const id = data?.id
    if (typeof id !== 'string' || id.length === 0 || decided.has(id) || claimed.has(id)) continue
    if ((request.callId ?? null) !== (data?.callId ?? null)) continue
    return id
  }
  return undefined
}

/** Lower one ChatDS decision receipt back into DSH's native question shape. */
export function questionAnswerFromDecision(question, decision) {
  if (question?.intent?.kind === 'plan-review') {
    const approve = String(question.intent.approve ?? '')
    const labels = (question.options ?? []).map(option => String(option.label ?? ''))
    if (approve === '' || !labels.includes(approve)) {
      throw new Error('chatds-control-bridge: invalid plan-review intent')
    }
    if (decision?.decision === 'allow') return { selected: [approve] }
    if (decision?.decision === 'deny') {
      const decline = labels.find(label => label !== '' && label !== approve)
      return { selected: decline === undefined ? [] : [decline] }
    }
    throw new Error('chatds-control-bridge: invalid plan-review decision')
  }
  const selected = Array.isArray(decision?.selected)
    ? decision.selected.map(value => String(value))
    : []
  return {
    selected,
    ...(decision?.custom === undefined ? {} : { custom: String(decision.custom) }),
  }
}

/** Decide whether an upstream approval ask reaches browser I/O. */
export function approvalDispositionForWebPreset(preset) {
  if (preset === 'read_only') return 'reject'
  if (preset === 'workspace_write') return 'relay'
  if (preset === 'session_full') return 'reject'
  throw new Error('chatds-control-bridge: invalid Web permission preset')
}

export async function apply(ctx) {
  const decisionsPath = process.env.CHATDS_CONTROL_DECISIONS
  if (!decisionsPath) throw new Error('chatds-control-bridge: CHATDS_CONTROL_DECISIONS is unavailable')
  const publishNativeEvent = await acquireNativeEventPublisher(ctx)
  const approvalDisposition = approvalDispositionForWebPreset(
    process.env.CHATDS_WEB_PERMISSION_PRESET,
  )

  const publish = (event) => {
    publishNativeEvent({ type: 'deepseek.session.event', ...event })
  }

  const claimedApprovalIds = new Set()

  ctx.on('approval/request', async (request, next) => {
    // Hard read-only never creates a browser waiter. Full access normally
    // produces no asks; an unexpected upstream ask also fails closed rather
    // than silently widening the selected tier.
    if (approvalDisposition === 'reject') return 'rejected'
    const session = request.agent?.session
    const requestId = findPendingApprovalId(
      session?.events,
      request,
      claimedApprovalIds,
    )
    // An unaudited ask did not pass through DSH's native approval service.
    // It is not this Web I/O bridge's decision, so delegate fail-closed.
    if (requestId === undefined) return next()
    claimedApprovalIds.add(requestId)
    publish({
      session_id: session === undefined ? '' : String(session.id),
      delegation_depth: session?.header?.delegationDepth ?? 0,
      session_event: {
        type: 'chatds/approval/requested',
        data: {
          request_id: requestId,
          tool_name: String(request.toolName ?? ''),
          call_id: request.callId === undefined ? undefined : String(request.callId),
          reason: request.reason === undefined ? undefined : String(request.reason),
        },
      },
    })
    const decision = await awaitDecision(decisionsPath, requestId, request.signal)
    if (decision === undefined) return next()
    const verdict = String(decision.decision ?? '')
    if (verdict === 'allow') return 'allowed-once'
    if (verdict === 'deny') return 'rejected'
    return next()
  })

  const questions = ctx.get('userQuestions')
  if (questions === undefined) return
  questions.registerProvider({
    ask: async (request) => {
      const session = request.agent?.session
      const answers = []
      for (const question of request.questions) {
        const requestId = `question-${randomUUID()}`
        publish({
          session_id: session === undefined ? '' : String(session.id),
          delegation_depth: session?.header?.delegationDepth ?? 0,
          session_event: {
            type: 'chatds/question/requested',
            data: {
              request_id: requestId,
              question_id: String(question.id ?? ''),
              question: String(question.question ?? ''),
              detail: question.detail === undefined ? undefined : String(question.detail),
              header: question.header === undefined ? undefined : String(question.header),
              multi_select: question.multiSelect === true,
              intent_kind: question.intent?.kind,
              intent_approve: question.intent?.approve,
              options: (question.options ?? []).map(option => ({
                label: String(option.label ?? ''),
                description: option.description === undefined ? undefined : String(option.description),
              })),
            },
          },
        })
        const decision = await awaitDecision(decisionsPath, requestId, request.signal)
        if (decision === undefined) throw new Error('chatds-control-bridge: the question was withdrawn before an answer')
        publish({
          session_id: session === undefined ? '' : String(session.id),
          delegation_depth: session?.header?.delegationDepth ?? 0,
          session_event: {
            type: 'chatds/question/decided',
            data: {
              request_id: requestId,
              decision: String(decision.decision ?? ''),
              tool_name: question?.intent?.kind === 'plan-review'
                ? 'exit_plan_mode'
                : 'ask_user_question',
              interaction_kind: question?.intent?.kind === 'plan-review'
                ? 'user_action'
                : 'question',
            },
          },
        })
        answers.push({
          id: String(question.id ?? ''),
          ...questionAnswerFromDecision(question, decision),
        })
      }
      return { answers }
    },
  })
}
