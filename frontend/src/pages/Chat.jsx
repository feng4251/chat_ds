import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import Sidebar from '../components/Sidebar'
import ChatArea from '../components/ChatArea'
import Settings from '../components/Settings'
import SkillLibrary from '../components/SkillLibrary'
import { getModels, getConversations } from '../api'

export default function Chat() {
  const { convId } = useParams()
  const nav = useNavigate()
  const [models, setModels] = useState([])
  const [conversations, setConversations] = useState([])
  const [showSidebar, setShowSidebar] = useState(true)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [skillLibOpen, setSkillLibOpen] = useState(false)
  const activeConv = convId || null

  useEffect(() => {
    const token = localStorage.getItem('token')
    if (!token) { nav('/login'); return }
    refreshModels()
    loadConversations()
  }, [nav])

  async function refreshModels() {
    try { setModels(await getModels()) } catch (err) { console.error('Failed to load models:', err) }
  }

  async function loadConversations() {
    try { setConversations(await getConversations()) } catch (err) { console.error('Failed to load conversations:', err) }
  }

  function onNewChat() {
    nav('/chat')
  }

  function onSelectConv(id) {
    nav(`/chat/${id}`)
  }

  function onConvCreated(id) {
    nav(`/chat/${id}`)
    loadConversations()
  }

  return (
    <div className="h-screen flex bg-stone-50 overflow-hidden">
      {!showSidebar && (
        <button
          onClick={() => setShowSidebar(true)}
          className="fixed top-3 left-3 z-50 p-2 bg-gray-800 rounded-lg text-white lg:hidden"
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
      )}

      <div className={`${showSidebar ? 'block' : 'hidden'} lg:block lg:w-80 flex-shrink-0 h-full`}>
        <Sidebar
          conversations={conversations}
          activeConv={activeConv}
          onNewChat={onNewChat}
          onSelectConv={onSelectConv}
          onClose={() => setShowSidebar(false)}
          onRefresh={loadConversations}
          onOpenSettings={() => setSettingsOpen(true)}
          onOpenSkillLibrary={() => setSkillLibOpen(true)}
          onConversationCreated={onSelectConv}
        />
      </div>

      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <ChatArea
          activeConv={activeConv}
          models={models}
          onConvCreated={onConvCreated}
          onConvRefresh={loadConversations}
        />
      </div>

      <Settings
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onModelsChanged={refreshModels}
      />

      <SkillLibrary
        open={skillLibOpen}
        onClose={() => setSkillLibOpen(false)}
      />
    </div>
  )
}
