/**
 * ChatDS control bridge for one isolated DeepSeek Harness Turn.
 */

import { closeSync, fsyncSync, openSync, readFileSync, writeSync } from 'node:fs'
import { randomUUID } from 'node:crypto'

export const name = 'chatds-control-bridge'
export const inject = ['approval']
const POLL_INTERVAL_MS = 200

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

export function apply(ctx) {
  const ledger = process.env.CHATDS_EVENT_LEDGER
  const decisionsPath = process.env.CHATDS_CONTROL_DECISIONS
  if (!ledger) throw new Error('chatds-control-bridge: CHATDS_EVENT_LEDGER is unavailable')
  if (!decisionsPath) throw new Error('chatds-control-bridge: CHATDS_CONTROL_DECISIONS is unavailable')

  const publish = (event) => {
    append(ledger, {
      received_at_unix_ms: Date.now(),
      channel: 'deepseek-harness',
      event: { type: 'deepseek.session.event', ...event },
    })
  }

  ctx.on('approval/request', async (request, next) => {
    const requestId = `approval-${randomUUID()}`
    const session = request.agent?.session
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
        const selected = Array.isArray(decision.selected) ? decision.selected.map(value => String(value)) : []
        answers.push({ id: String(question.id ?? ''), selected, ...(decision.custom === undefined ? {} : { custom: String(decision.custom) }) })
      }
      return { answers }
    },
  })
}
