import { useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Login from './pages/Login'
import Register from './pages/Register'
import Chat from './pages/Chat'
import {
  canApplyFrontendUpdate,
  currentFrontendEntry,
  frontendVersionChanged,
  getFrontendBuildInfo,
} from './utils/frontendVersion'

const FRONTEND_VERSION_CHECK_MS = 60000

function FrontendVersionGuard() {
  useEffect(() => {
    const currentEntry = currentFrontendEntry()
    if (!currentEntry) return undefined
    let stopped = false
    let checking = false

    const check = async () => {
      if (stopped || checking) return
      checking = true
      try {
        const buildInfo = await getFrontendBuildInfo()
        if (
          !stopped
          && frontendVersionChanged(currentEntry, buildInfo)
          && canApplyFrontendUpdate()
        ) {
          window.location.reload()
        }
      } catch {
        // A failed metadata read is transient; the interval remains armed.
      } finally {
        checking = false
      }
    }

    const onPageActive = () => {
      if (document.visibilityState !== 'hidden') void check()
    }
    const interval = window.setInterval(check, FRONTEND_VERSION_CHECK_MS)
    window.addEventListener('focus', onPageActive)
    window.addEventListener('online', onPageActive)
    window.addEventListener('pageshow', onPageActive)
    document.addEventListener('visibilitychange', onPageActive)
    return () => {
      stopped = true
      window.clearInterval(interval)
      window.removeEventListener('focus', onPageActive)
      window.removeEventListener('online', onPageActive)
      window.removeEventListener('pageshow', onPageActive)
      document.removeEventListener('visibilitychange', onPageActive)
    }
  }, [])

  return null
}

export default function App() {
  return (
    <>
      <FrontendVersionGuard />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
        <Route path="/chat/:convId?" element={<Chat />} />
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Routes>
    </>
  )
}
