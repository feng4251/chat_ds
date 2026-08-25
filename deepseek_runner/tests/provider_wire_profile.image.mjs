/** Candidate-image integration for deployment-owned reasoning wire aliases. */

import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { Context } from '/opt/deepseek-harness/vendor/cordis/lib/index.js'
import LlmRuntime, {
  BlockAssembler,
  createUserMessage,
} from '/opt/deepseek-harness/packages/llm/llm/lib/index.js'
import * as LlmDeepSeek from '/opt/deepseek-harness/packages/llm/llm-deepseek/lib/index.js'
import {
  createProviderWireProfileFetch,
} from '/opt/chatds-deepseek-plugins/provider_wire_profile.mjs'


const requests = []
const responseEvents = [
  '{"choices":[{"delta":{"role":"assistant","content":""},"index":0,"finish_reason":null}]}',
  '{"choices":[{"delta":{"content":"ok"},"index":0,"finish_reason":null}]}',
  '{"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}',
  '[DONE]',
]

const server = createServer((request, response) => {
  let body = ''
  request.setEncoding('utf8')
  request.on('data', (chunk) => { body += chunk })
  request.on('end', () => {
    requests.push({ path: request.url, body: JSON.parse(body) })
    response.writeHead(200, { 'content-type': 'text/event-stream' })
    for (const event of responseEvents) response.write(`data: ${event}\n\n`)
    response.end()
  })
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const address = server.address()
assert.notEqual(address, null)
assert.equal(typeof address, 'object')

const nativeFetch = globalThis.fetch
const stateHome = await mkdtemp(join(tmpdir(), 'chatds-provider-wire-profile-'))
process.env.CHATDS_REASONING_TEST_KEY = 'test-only-provider-key'
process.env.DSH_HOME = stateHome
const baseURL = `http://127.0.0.1:${address.port}/v1`
const cases = [
  { model: 'warehouse-planner', wireEffort: 'xhigh' },
  { model: 'museum-curator', wireEffort: 'ultra' },
]
const ctx = new Context()

try {
  await ctx.plugin(LlmRuntime)
  await ctx.plugin(LlmDeepSeek, {
    apiKeyEnv: 'CHATDS_REASONING_TEST_KEY',
    baseURL,
    thinking: 'enabled',
    reasoningEffort: 'max',
    maxTokens: 8192,
    defaultContextWindow: 262144,
    models: cases.map(({ model }) => ({
      id: model,
      name: model,
      contextWindow: 262144,
      maxTokens: 8192,
    })),
  })

  for (const item of cases) {
    globalThis.fetch = createProviderWireProfileFetch(nativeFetch, {
      baseURL,
      model: item.model,
      canonicalEffort: 'max',
      wireEffort: item.wireEffort,
    })
    const assembler = new BlockAssembler()
    for await (const chunk of ctx.llm.stream({
      provider: 'deepseek-official',
      model: item.model,
      system: `System contract for ${item.model}`,
      messages: [createUserMessage({
        content: [{ type: 'text', text: `Plan for ${item.model}` }],
        source: { kind: 'plugin', plugin: 'chatds-image-test' },
      })],
    })) assembler.push(chunk)
    assert.deepEqual(assembler.finish, { kind: 'stop' })
  }

  assert.equal(requests.length, 2)
  assert.deepEqual(requests.map(({ path }) => path), [
    '/v1/chat/completions',
    '/v1/chat/completions',
  ])
  assert.deepEqual(requests.map(({ body }) => body.model), cases.map(({ model }) => model))
  assert.deepEqual(
    requests.map(({ body }) => body.reasoning_effort),
    cases.map(({ wireEffort }) => wireEffort),
  )
  for (const { body } of requests) {
    assert.deepEqual(body.thinking, { type: 'enabled' })
    assert.deepEqual(body.messages.map(({ role }) => role), ['system', 'user'])
    assert.equal(body.messages.some(({ role }) => role === 'developer'), false)
  }

  // A profile is exact-model and exact-endpoint scoped. A renamed holdout on
  // the same endpoint and a matching model elsewhere both retain canonical max.
  const forwarded = []
  const recordingFetch = async (input, init) => {
    forwarded.push({ input: String(input), body: JSON.parse(init.body) })
    return new Response(null, { status: 204 })
  }
  const exactWarehouse = createProviderWireProfileFetch(recordingFetch, {
    baseURL,
    model: 'warehouse-planner',
    canonicalEffort: 'max',
    wireEffort: 'xhigh',
  })
  const canonicalBody = {
    model: 'museum-curator',
    reasoning_effort: 'max',
    messages: [{ role: 'system', content: 'unchanged' }],
  }
  await exactWarehouse(`${baseURL}/chat/completions`, {
    method: 'POST',
    body: JSON.stringify(canonicalBody),
  })
  await exactWarehouse('https://elsewhere.invalid/v1/chat/completions', {
    method: 'POST',
    body: JSON.stringify({ ...canonicalBody, model: 'warehouse-planner' }),
  })
  assert.deepEqual(forwarded.map(({ body }) => body.reasoning_effort), ['max', 'max'])
  assert.deepEqual(forwarded.map(({ body }) => body.messages[0].role), ['system', 'system'])
} finally {
  globalThis.fetch = nativeFetch
  delete process.env.CHATDS_REASONING_TEST_KEY
  delete process.env.DSH_HOME
  await ctx.fiber.dispose()
  await new Promise((resolve, reject) => server.close((error) => {
    if (error) reject(error)
    else resolve()
  }))
  await rm(stateHome, { recursive: true, force: true })
}

process.stdout.write('provider wire profile candidate-image integration passed\n')
