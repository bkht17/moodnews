import { useEffect, useState } from 'react'
import { getHealth } from './api/client.js'

// Scaffolding shell: the news grid, mood switcher and comparison view are
// added in the frontend sections. For now it verifies end-to-end connectivity
// between the React dev server and the FastAPI backend.
export default function App() {
  const [health, setHealth] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    getHealth().then(setHealth).catch((err) => setError(err.message))
  }, [])

  return (
    <div className="app">
      <header className="app__header">
        <h1 className="app__title">
          Mood<span className="app__title-accent">News</span>
        </h1>
        <p className="app__tagline">
          Real news, retold in the tone you choose &mdash; with every fact kept intact.
        </p>
      </header>

      <main className="app__main">
        <section className="panel">
          <h2 className="panel__title">Backend connectivity</h2>
          {error && <p className="status status--error">Cannot reach API: {error}</p>}
          {!error && !health && <p className="status">Checking&hellip;</p>}
          {health && (
            <ul className="status-list">
              <li>
                API: <strong>{health.status}</strong>
              </li>
              <li>
                Database: <strong>{health.database}</strong>
              </li>
              <li>
                LLM key configured: <strong>{health.llm_configured ? 'yes' : 'no'}</strong>
              </li>
            </ul>
          )}
        </section>
      </main>
    </div>
  )
}
