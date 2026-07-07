const API = '/api'

async function request(path, options = {}) {
  const token = localStorage.getItem('token')
  console.log('[request]', path, 'token:', token ? token.slice(0, 20) + '...' : 'MISSING')
  const headers = { 'Content-Type': 'application/json', ...options.headers }
  if (token) headers['Authorization'] = 'Bearer ' + token
  const res = await fetch(API + path, { ...options, headers })
  console.log('[request]', path, 'status:', res.status)
  if (res.status === 401) {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    window.location.href = '/login'
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    const detail = await res.json().then(d => d.detail).catch(() => null)
    throw new Error(detail || `Request failed (${res.status})`)
  }
  return res
}

export async function login(username, password) {
  const res = await request('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password })
  })
  if (!res.ok) throw new Error((await res.json()).detail || 'Login failed')
  const data = await res.json()
  localStorage.setItem('token', data.access_token)
  localStorage.setItem('user', JSON.stringify(data.user))
  return data
}

export async function register(username, password, email) {
  const res = await request('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ username, password, email })
  })
  if (!res.ok) throw new Error((await res.json()).detail || 'Register failed')
  const data = await res.json()
  localStorage.setItem('token', data.access_token)
  localStorage.setItem('user', JSON.stringify(data.user))
  return data
}

export async function getModels() {
  const res = await request('/chat/models')
  return (await res.json()).models
}

export async function getSkills() {
  const res = await request('/chat/skills')
  return (await res.json()).skills
}

export async function getCustomModels() {
  const res = await request('/models/config')
  return await res.json()
}

export async function createCustomModel(payload) {
  const res = await request('/models/config', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error((await res.json()).detail || 'Failed to add model')
  return await res.json()
}

export async function deleteCustomModel(cfgId) {
  const res = await request('/models/config/' + cfgId, { method: 'DELETE' })
  if (!res.ok) throw new Error((await res.json()).detail || 'Failed to delete')
  return await res.json()
}

export async function getConversations() {
  console.log('[getConversations] calling /api/conversations')
  const res = await request('/conversations')
  const data = await res.json()
  console.log('[getConversations] got', data.length, 'conversations:', data.map(c => c.id.slice(0,8)).join(', '))
  return data
}

export async function getMessages(convId) {
  const res = await request('/conversations/' + convId + '/messages')
  return await res.json()
}

export async function renameConversation(convId, title) {
  const res = await request('/conversations/' + convId + '/title', {
    method: 'PATCH',
    body: JSON.stringify({ title })
  })
  return await res.json()
}

export async function deleteConversation(convId) {
  const res = await request('/conversations/' + convId, { method: 'DELETE' })
  return await res.json()
}

export function chatCompletion(content, conversationId, modelId, imageUrls, skillId, onChunk) {
  const token = localStorage.getItem('token')
  return fetch(API + '/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + token
    },
    body: JSON.stringify({
      content,
      conversation_id: conversationId,
      model_id: modelId,
      image_urls: imageUrls,
      skill_id: skillId,
    })
  }).then(async (res) => {
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let convId = null
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() // keep partial line
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            if (data.conversation_id) convId = data.conversation_id
            onChunk(data)
          } catch (e) {}
        }
      }
    }
    return convId
  })
}
