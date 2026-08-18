import { formatDate, formatRelative } from '../utils/format.js'

// One card in the grid: where it came from, when, and enough of the text to
// decide whether to open it.
export default function NewsCard({ article, onOpen }) {
  const published = article.published_at || article.fetched_at

  // The whole card is the target - a title-only hit area is a needlessly
  // small one - so it carries the button role and keyboard handling itself.
  const handleKeyDown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onOpen(article.id)
    }
  }

  return (
    <article
      className="card card--interactive"
      role="button"
      tabIndex={0}
      onClick={() => onOpen(article.id)}
      onKeyDown={handleKeyDown}
      aria-label={`Read "${article.title}" side by side with its rewrite`}
    >
      <div className="card__meta">
        <span className="card__source">{article.source_name}</span>
        <time className="card__date" dateTime={published} title={formatDate(published)}>
          {formatRelative(published) || formatDate(published)}
        </time>
      </div>

      <h3 className="card__title">{article.title}</h3>
      <p className="card__preview">{article.preview}</p>

      <div className="card__footer">
        <span className="card__cta">Compare moods →</span>
        {/* Every card links to the original reporting: a reader who wants the
            source of a claim should never have to hunt for it. The link stops
            propagation so it does not also open the comparison view. */}
        <a
          className="card__source-link"
          href={article.source_url}
          target="_blank"
          rel="noreferrer noopener"
          onClick={(event) => event.stopPropagation()}
        >
          Source ↗
        </a>
      </div>
    </article>
  )
}
