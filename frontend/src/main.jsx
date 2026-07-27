import React from 'react'
import { createRoot } from 'react-dom/client'
import PlayPage from './pages/PlayPage'
import './index.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <PlayPage />
  </React.StrictMode>
)

// Register the service worker so the app installs to a phone home screen.
// It caches the shell only — never API responses, because a stale turn would be
// worse than an error message.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Non-fatal: the app works fine as a normal page without it.
    })
  })
}
