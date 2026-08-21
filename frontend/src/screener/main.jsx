/* Entry point for the standalone Wallet Screener.
 *
 * A separate Vite entry, not a route inside the Mini App bundle: the screener
 * must be able to move to screener.polytradebot.live as a static site without
 * a rewrite, and it must not drag the authenticated app's code — or its
 * session bootstrap — onto an anonymous public page.
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import ScreenerPage from './ScreenerPage'
import '../styles/brutalism.css'
import '../styles/screener.css'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ScreenerPage />
  </StrictMode>,
)
