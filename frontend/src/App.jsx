import { useCallback, useEffect, useState } from 'react'
import MoodSwitcher from './components/MoodSwitcher.jsx'
import NewsGrid from './components/NewsGrid.jsx'
import { getMoods, getNews } from './api/client.js'

const MOOD_STORAGE_KEY = 'moodnews.mood'
const DEFAULT_MOOD = 'neutral'

// The chosen mood outlives a reload: it is the reader's standing preference
// for how they want the news told, not per-visit state.
function readStoredMood() {
  try {
    return window.localStorage.getItem(MOOD_STORAGE_KEY) || DEFAULT_MOOD
  } catch {
    // Private browsing or blocked storage: fall back to the default.
    return DEFAULT_MOOD
  }
}

export default function App() {
  const [moods, setMoods] = useState([])
  const [moodsError, setMoodsError] = useState(null)
  const [mood, setMood] = useState(readStoredMood)

  const [articles, setArticles] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const loadMoods = useCallback(() => {
    setMoodsError(null)
    getMoods()
      .then((data) => {
        setMoods(data)
        // A mood stored by an older build may no longer exist.
        setMood((current) =>
          data.some((item) => item.key === current) ? current : DEFAULT_MOOD,
        )
      })
      .catch((err) => setMoodsError(err.message))
  }, [])

  const loadNews = useCallback(() => {
    setLoading(true)
    setError(null)
    getNews({ limit: 30 })
      .then((data) => setArticles(data.items))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  // Retry reloads both: when the backend was down, the moods failed with the
  // news, and recovering only half of the page would leave the reader without
  // a mood switcher until they reloaded by hand.
  const reload = useCallback(() => {
    loadMoods()
    loadNews()
  }, [loadMoods, loadNews])

  useEffect(() => {
    reload()
  }, [reload])

  const handleMoodChange = useCallback((key) => {
    setMood(key)
    try {
      window.localStorage.setItem(MOOD_STORAGE_KEY, key)
    } catch {
      // Persisting is a nicety; the app works without it.
    }
  }, [])

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <h1 className="app__title">
            Mood<span className="app__title-accent">News</span>
          </h1>
          <p className="app__tagline">
            Real news, retold in the tone you choose &mdash; with every fact kept
            intact.
          </p>
        </div>

        {moodsError ? (
          <p className="status status--error">
            Could not load moods: {moodsError}
          </p>
        ) : (
          <MoodSwitcher
            moods={moods}
            value={mood}
            onChange={handleMoodChange}
            disabled={!moods.length}
          />
        )}
      </header>

      <main className="app__main">
        <div className="section-head">
          <h2 className="section-head__title">Latest news</h2>
          <span className="section-head__count">
            {articles.length > 0 && `${articles.length} articles`}
          </span>
        </div>

        <NewsGrid
          articles={articles}
          loading={loading}
          error={error}
          onRetry={reload}
        />
      </main>

      <footer className="app__footer">
        <p>
          Articles are fetched from public RSS feeds and stored locally. Every
          rewrite is fact-checked against the original before it is shown.
        </p>
      </footer>
    </div>
  )
}
