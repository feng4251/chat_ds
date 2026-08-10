const API = '/api'

async function request(path, options = {}) {
  const token = localStorage.getItem('token')
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

export async function createConversation() {
  const res = await request('/conversations', { method: 'POST' })
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

export async function forkConversation(
  convId,
  title,
  includeMessages = true,
  targetEngineId = null,
  targetModelId = null,
) {
  const params = new URLSearchParams()
  if (title) params.set('title', title)
  params.set('include_messages', String(includeMessages))
  if (targetEngineId) params.set('target_engine_id', targetEngineId)
  if (targetModelId) params.set('target_model_id', targetModelId)
  const res = await request(`/conversations/${convId}/fork?${params}`, { method: 'POST' })
  return await res.json()
}

export async function getConversationSettings(convId) {
  const res = await request(`/conversations/${convId}/settings`)
  return await res.json()
}

export async function updateConversationSettings(convId, payload) {
  const res = await request(`/conversations/${convId}/settings`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
  return await res.json()
}

export async function getWorkspace(convId) {
  const res = await request(`/conversations/${convId}/workspace`)
  return await res.json()
}

export async function readWorkspaceFile(convId, path) {
  const res = await request(`/conversations/${convId}/workspace/file?path=${encodeURIComponent(path)}`)
  return await res.json()
}

export async function getWorkspaceFileBlobUrl(convId, path) {
  const token = localStorage.getItem('token')
  const res = await fetch(`${API}/conversations/${convId}/workspace/file/raw?path=${encodeURIComponent(path)}`, {
    headers: token ? { Authorization: 'Bearer ' + token } : {},
  })
  if (!res.ok) throw new Error(`File preview failed (${res.status})`)
  return URL.createObjectURL(await res.blob())
}

export async function downloadWorkspaceFile(convId, path) {
  const blobUrl = await getWorkspaceFileBlobUrl(convId, path)
  const anchor = document.createElement('a')
  anchor.href = blobUrl
  anchor.download = path.split('/').pop() || 'workspace-file'
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  try {
    anchor.click()
  } finally {
    anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(blobUrl), 0)
  }
}

export async function writeWorkspaceFile(convId, path, content) {
  const res = await request(`/conversations/${convId}/workspace/file?path=${encodeURIComponent(path)}`, {
    method: 'PUT',
    body: JSON.stringify({ content }),
  })
  return await res.json()
}

export async function deleteWorkspaceFile(convId, path) {
  const res = await request(`/conversations/${convId}/workspace/file?path=${encodeURIComponent(path)}`, {
    method: 'DELETE',
  })
  return await res.json()
}

export async function getGoal(convId) {
  const res = await request(`/conversations/${convId}/goal`)
  return await res.json()
}

export async function updateGoal(convId, payload) {
  const res = await request(`/conversations/${convId}/goal`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
  return await res.json()
}

export async function clearGoal(convId) {
  const res = await request(`/conversations/${convId}/goal`, { method: 'DELETE' })
  return await res.json()
}

export async function getRuns(convId) {
  const res = await request(`/conversations/${convId}/runs`)
  return await res.json()
}

export async function getRunCards(convId, rootLimit = 20) {
  const search = new URLSearchParams()
  search.set('root_limit', String(rootLimit))
  const res = await request(`/conversations/${convId}/run-cards?${search}`)
  return await res.json()
}

export async function getArtifacts(convId, params = {}) {
  const search = new URLSearchParams()
  if (params.run_id) search.set('run_id', params.run_id)
  if (params.limit) search.set('limit', String(params.limit))
  const suffix = search.toString() ? `?${search}` : ''
  const res = await request(`/conversations/${convId}/artifacts${suffix}`)
  return await res.json()
}

export async function getArtifact(convId, artifactId) {
  const res = await request(`/conversations/${convId}/artifacts/${artifactId}`)
  return await res.json()
}

export async function getTasks(convId) {
  const res = await request(`/conversations/${convId}/tasks`)
  return await res.json()
}

export async function getRunEvents(convId, runId, params = {}) {
  const search = new URLSearchParams()
  if (params.limit) search.set('limit', String(params.limit))
  if (params.offset) search.set('offset', String(params.offset))
  const suffix = search.toString() ? `?${search}` : ''
  const res = await request(`/conversations/${convId}/runs/${runId}/events${suffix}`)
  return await res.json()
}

export async function downloadTrajectory(convId) {
  const token = localStorage.getItem('token')
  const res = await fetch(`${API}/conversations/${convId}/trajectory`, {
    headers: { Authorization: 'Bearer ' + token },
  })
  if (!res.ok) throw new Error(`Export failed (${res.status})`)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `trajectory-${convId}.json`
  a.click()
  URL.revokeObjectURL(url)
}

export async function getSchedules(conversationId) {
  const suffix = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''
  const res = await request('/schedules' + suffix)
  return await res.json()
}

export async function createSchedule(payload) {
  const res = await request('/schedules', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await res.json()
}

export async function updateSchedule(jobId, payload) {
  const res = await request('/schedules/' + jobId, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
  return await res.json()
}

export async function deleteSchedule(jobId) {
  const res = await request('/schedules/' + jobId, { method: 'DELETE' })
  return await res.json()
}

export async function runSchedule(jobId) {
  const res = await request(`/schedules/${jobId}/run`, { method: 'POST' })
  return await res.json()
}

export async function getHooks() {
  const res = await request('/hooks')
  return await res.json()
}

export async function createHook(payload) {
  const res = await request('/hooks', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await res.json()
}

export async function updateHook(hookId, payload) {
  const res = await request('/hooks/' + hookId, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
  return await res.json()
}

export async function deleteHook(hookId) {
  const res = await request('/hooks/' + hookId, { method: 'DELETE' })
  return await res.json()
}

export async function getMcpServers(sessionId) {
  const suffix = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  const res = await request('/mcp/servers' + suffix)
  return await res.json()
}

export async function addMcpServer(payload) {
  const res = await request('/mcp/servers', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await res.json()
}

export async function deleteMcpServer(name, sessionId) {
  const suffix = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  const res = await request('/mcp/servers/' + encodeURIComponent(name) + suffix, {
    method: 'DELETE',
  })
  return await res.json()
}

// ── Skills ──────────────────────────────────────────────────────────────────

export async function getSkills(sessionId, enabledUserSkills) {
  const params = new URLSearchParams()
  if (sessionId) params.set('session_id', sessionId)
  if (Array.isArray(enabledUserSkills)) {
    if (enabledUserSkills.length === 0) {
      params.append('enabled_user_skills', '')
    } else {
      for (const s of enabledUserSkills) {
        params.append('enabled_user_skills', s)
      }
    }
  }
  const qs = params.toString()
  const res = await request('/skills' + (qs ? '?' + qs : ''))
  return await res.json()
}

export async function uploadSkill(file, category, sessionId) {
  const token = localStorage.getItem('token')
  // Read file as base64 to avoid DLP interception of multipart/form-data.
  const base64 = await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      // Strip the data:...;base64, prefix, keep only the payload
      const full = reader.result
      const comma = full.indexOf(',')
      resolve(comma >= 0 ? full.slice(comma + 1) : full)
    }
    reader.onerror = reject
    reader.readAsDataURL(file)
  })

  const res = await fetch(API + '/skills/upload/json', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + token,
    },
    body: JSON.stringify({
      filename: file.name,
      content_base64: base64,
      category: category || null,
      session_id: sessionId || null,
    }),
  })
  if (res.status === 401) {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    window.location.href = '/login'
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    const detail = await res.json().then(d => d.detail).catch(() => null)
    throw new Error(detail || `Upload failed (${res.status})`)
  }
  return await res.json()
}

export async function deleteSkill(name, sessionId) {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  const res = await request('/skills/' + encodeURIComponent(name) + qs, { method: 'DELETE' })
  if (!res.ok) throw new Error((await res.json()).detail || 'Failed to delete')
  return await res.json()
}

export async function promoteSkill(name, sessionId) {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  const res = await request('/skills/' + encodeURIComponent(name) + '/promote' + qs, {
    method: 'POST',
  })
  if (!res.ok) throw new Error((await res.json()).detail || 'Failed to promote')
  return await res.json()
}

export async function getOptionalSkills() {
  const res = await request('/skills/optional')
  return (await res.json()).skills
}

export async function uploadSessionFile(convId, file) {
  const token = localStorage.getItem('token')
  const formData = new FormData()
  formData.append('file', file)

  const res = await fetch(API + '/conversations/' + convId + '/upload', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + token },
    body: formData,
  })
  if (res.status === 401) {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    window.location.href = '/login'
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    const detail = await res.json().then(d => d.detail).catch(() => null)
    throw new Error(detail || `Upload failed (${res.status})`)
  }
  return await res.json()
}

export async function chatCompletion(
  content,
  conversationId,
  modelId,
  imageUrls,
  onChunk,
  options = {},
) {
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
      ...(modelId ? { model_id: modelId } : {}),
      image_urls: imageUrls,
    }),
    signal: options.signal,
  }).then(async (res) => {
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error(text || `HTTP ${res.status}`)
    }
    const requestIdHeader = res.headers.get('x-request-id') || ''
    const requestId = /^[A-Za-z0-9_.:/-]{1,128}$/.test(requestIdHeader)
      ? requestIdHeader
      : ''
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let convId = null
    let buf = ''
    let terminalEnvelope = null
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop()
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            let data
            try {
              data = JSON.parse(line.slice(6))
            } catch {
              // Ignore partial/non-JSON SSE lines.
              continue
            }
            if (data.conversation_id) convId = data.conversation_id
            if (data.stream_terminal) terminalEnvelope = data.stream_terminal
            // Keep consumer failures distinct from malformed upstream SSE.
            onChunk(data)
          }
        }
      }
      // Flush remaining buffer
      if (buf.startsWith('data: ')) {
        let data
        try {
          data = JSON.parse(buf.slice(6))
        } catch {
          // Ignore a trailing partial/non-JSON SSE line.
        }
        if (data) {
          if (data.conversation_id) convId = data.conversation_id
          if (data.stream_terminal) terminalEnvelope = data.stream_terminal
          onChunk(data)
        }
      }
    } catch (error) {
      // Consumer errors and route-change aborts must release the body reader;
      // otherwise the abandoned response can keep a socket and Backend relay
      // attached until garbage collection.
      await reader.cancel().catch(() => {})
      throw error
    }
    if (!terminalEnvelope) {
      const err = new Error(
        '响应连接在服务端终态确认前结束；当前内容是不完整草稿。' +
        (requestId ? ` 请求追踪 ID：${requestId}` : '')
      )
      err.code = 'stream_terminal_missing'
      if (requestId) err.requestId = requestId
      throw err
    }
    return convId
  })
}
