import NewsCard from './NewsCard.jsx'

// The grid, plus the three states it can be in besides "has articles".
export default function NewsGrid({ articles, loading, error, onRetry }) {
  if (loading) {
    // Skeleton cards rather than a spinner: the layout does not jump when the
    // real articles arrive.
    return (
      <div className="grid" aria-busy="true" aria-label="Loading articles">
        {Array.from({ length: 6 }).map((_, index) => (
          <div className="card card--skeleton" key={index}>
            <div className="skeleton skeleton--meta" />
            <div className="skeleton skeleton--title" />
            <div className="skeleton skeleton--title skeleton--short" />
            <div className="skeleton skeleton--text" />
            <div className="skeleton skeleton--text" />
            <div className="skeleton skeleton--text skeleton--short" />
          </div>
        ))}
      </div>
    )
  }

  if (error) {
    return (
      <div className="notice notice--error" role="alert">
        <p>Could not load the news: {error}</p>
        <p className="notice__detail">
          Check that the backend is running on port 8000.
        </p>
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      </div>
    )
  }

  if (!articles.length) {
    return (
      <div className="notice">
        <p>No articles stored yet.</p>
        <p className="notice__detail">
          The backend fetches from its RSS feeds on startup. Run{' '}
          <code>docker compose exec backend python -m app.cli fetch</code> to
          pull them now.
        </p>
        <button type="button" className="button" onClick={onRetry}>
          Refresh
        </button>
      </div>
    )
  }

  return (
    <div className="grid">
      {articles.map((article) => (
        <NewsCard key={article.id} article={article} />
      ))}
    </div>
  )
}
