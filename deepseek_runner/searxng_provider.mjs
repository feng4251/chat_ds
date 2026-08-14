import z from '@deepseek-ai/schemastery'
import { WebError } from '@deepseek-ai/dsh-web'

export const name = 'chatds-searxng-search'
export const inject = ['web']

export const Config = z.object({ endpoint: z.string().required() })

function source(row) {
  if (!row || typeof row !== 'object' || typeof row.url !== 'string') return undefined
  return {
    url: row.url,
    ...(typeof row.title === 'string' && row.title ? { title: row.title } : {}),
    ...(typeof row.content === 'string' && row.content ? { snippet: row.content } : {}),
    ...(typeof row.publishedDate === 'string' && row.publishedDate
      ? { publishedAt: row.publishedDate }
      : {}),
  }
}

export class SearxngSearchProvider {
  id = 'chatds-searxng'

  constructor(endpoint) {
    const parsed = new URL(endpoint)
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.pathname !== '/search'
      || parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error('chatds-searxng-search: endpoint must be an exact /search URL')
    }
    this.endpoint = parsed.toString()
  }

  available() { return true }

  async search(request, signal) {
    const target = new URL(this.endpoint)
    target.searchParams.set('q', request.query)
    target.searchParams.set('format', 'json')
    target.searchParams.set('safesearch', '1')
    let response
    try {
      response = await fetch(target, {
        method: 'GET',
        redirect: 'error',
        headers: { accept: 'application/json', 'user-agent': 'chatds-deepseek-harness/1' },
        ...(signal === undefined ? {} : { signal }),
      })
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw new WebError('SearXNG search aborted', 'WEB_ABORTED', { cause: error })
      }
      throw new WebError('SearXNG search request failed', 'WEB_PROVIDER_ERROR', { cause: error })
    }
    if (!response.ok) {
      throw new WebError(`SearXNG returned HTTP ${response.status}`, 'WEB_PROVIDER_ERROR')
    }
    let body
    try {
      body = await response.json()
    } catch (error) {
      throw new WebError('SearXNG returned malformed JSON', 'WEB_PROVIDER_ERROR', { cause: error })
    }
    const sources = Array.isArray(body?.results)
      ? body.results.map(source).filter(Boolean)
      : []
    return { sources, truncated: false }
  }
}

export function apply(ctx, config) {
  ctx.web.registerSearchProvider(new SearxngSearchProvider(config.endpoint))
}
