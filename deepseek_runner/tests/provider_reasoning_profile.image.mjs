/** Candidate-image check for deployment-owned reasoning wire aliases. */

import assert from 'node:assert/strict'
import { createServer } from 'node:http'

import { Context } from '/opt/deepseek-harness/vendor/cordis/lib/index.js'
import LlmRuntime, {
  BlockAssembler,
} from '/opt/deepseek-harness/packages/llm/llm/lib/index.js'
import * as LlmPiAi from '/opt/deepseek-harness/packages/llm/llm-pi-ai/lib/index.js'


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

process.env.CHATDS_REASONING_TEST_KEY = 'test-only-provider-key'
const ctx = new Context()

try {
  await ctx.plugin(LlmRuntime)
  await ctx.plugin(LlmPiAi, {
    providers: {
      'renamed-gateway': {
        apiKeyEnv: 'CHATDS_REASONING_TEST_KEY',
        api: 'openai-completions',
        baseURL: `http://127.0.0.1:${address.port}/v1`,
        compat: {
          thinkingFormat: 'deepseek',
          supportsReasoningEffort: true,
        },
        reasoning: 'max',
        models: [
          {
            id: 'warehouse-planner',
            contextWindow: 128000,
            maxTokens: 8192,
            reasoningEfforts: { max: 'xhigh' },
          },
          {
            id: 'museum-curator',
            contextWindow: 262144,
            maxTokens: 16384,
            reasoningEfforts: { max: 'ultra' },
          },
        ],
      },
    },
  })

  for (const model of ['warehouse-planner', 'museum-curator']) {
    const assembler = new BlockAssembler()
    for await (const chunk of ctx.llm.stream({
      provider: 'renamed-gateway',
      model,
      messages: [],
    })) assembler.push(chunk)
    assert.deepEqual(assembler.finish, { kind: 'stop' })
  }

  assert.equal(requests.length, 2)
  assert.deepEqual(requests.map(({ path }) => path), [
    '/v1/chat/completions',
    '/v1/chat/completions',
  ])
  assert.deepEqual(requests.map(({ body }) => body.reasoning_effort), [
    'xhigh',
    'ultra',
  ])
  for (const { body } of requests) {
    assert.deepEqual(body.thinking, { type: 'enabled' })
  }
} finally {
  delete process.env.CHATDS_REASONING_TEST_KEY
  await ctx.fiber.dispose()
  await new Promise((resolve, reject) => server.close((error) => {
    if (error) reject(error)
    else resolve()
  }))
}

process.stdout.write('provider reasoning profile candidate-image integration passed\n')
