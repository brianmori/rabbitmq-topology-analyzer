import { useEffect, useState } from 'react'
import { fetchAccess, fetchAnalyses, fetchHealth } from './api/topology'
import type { AccessResponse, AnalysisCatalogueResponse } from './types/topology'
import './App.css'

/**
 * Iteration 1 scaffolding only.
 *
 * This component's only job right now is to prove the full round trip
 * works: browser -> Vite dev server -> proxy -> FastAPI backend -> back.
 * Iteration 2 replaces this with the real cluster picker; iteration 3 adds
 * the React Flow canvas.
 *
 * `useState` holds values that change over time and re-render the
 * component when they do -- think of it as the React equivalent of an
 * instance attribute that also happens to know when to redraw itself.
 * `useEffect` runs side effects (like a fetch call) after the component
 * renders; the empty `[]` dependency array below means "run this once,
 * right after the first render" -- roughly analogous to code you'd put in
 * a Python `__post_init__`.
 */
function App() {
  const [healthStatus, setHealthStatus] = useState<string>('checking...')
  const [access, setAccess] = useState<AccessResponse | null>(null)
  const [analyses, setAnalyses] = useState<AnalysisCatalogueResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchHealth()
      .then((result) => setHealthStatus(result.status))
      .catch((err: unknown) => setError(String(err)))

    fetchAnalyses()
      .then((result) => setAnalyses(result))
      .catch((err: unknown) => setError(String(err)))

    fetchAccess()
      .then((result) => setAccess(result))
      .catch((err: unknown) => setError(String(err)))
  }, [])

  return (
    <div style={{ fontFamily: 'sans-serif', padding: '2rem' }}>
      <h1>RabbitMQ Topology Analyzer</h1>
      <p>Backend health: <strong>{healthStatus}</strong></p>

      {access && (
        <p>
          Access: <strong>{access.roles.join(', ') || 'no application role'}</strong>
          {' · '}upload {access.capabilities.manageUploads ? 'allowed' : 'not allowed'}
        </p>
      )}

      {error && <p style={{ color: 'crimson' }}>Error: {error}</p>}

      {analyses && (
        <div>
          <p>Static analyses: {analyses.static.length}</p>
          <p>Session analyses: {analyses.session.length}</p>
        </div>
      )}
    </div>
  )
}

export default App
