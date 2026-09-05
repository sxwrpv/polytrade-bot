import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './styles/brutalism.css'
import './styles/public-landing.css'

async function bootstrapDevPreview() {
  if (!import.meta.env.DEV) return
  try {
    await fetch('/api/auth/dev-login', { method: 'POST', credentials: 'same-origin' })
  } catch {
    /* backend may be offline; public site still renders */
  }
}

bootstrapDevPreview().then(() => {
  ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  )
})
