import { formatDate, formatRelative } from '../utils/format.js'

// One card in the grid: where it came from, when, and enough of the text to
// decide whether to open it.
export default function NewsCard({ article }) {
  const published = article.published_at || article.fetched_at

  return (
    <article className="card">
      <div className="card__meta">
        <span className="card__source">{article.source_name}</span>
        <time className="card__date" dateTime={published} title={formatDate(published)}>
          {formatRelative(published) || formatDate(published)}
        </time>
      </div>

      <h3 className="card__title">{article.title}</h3>
      <p className="card__preview">{article.preview}</p>

      <div className="card__footer">
        {/* Every card links to the original reporting: a reader who wants the
            source of a claim should never have to hunt for it. */}
        <a
          className="card__source-link"
          href={article.source_url}
          target="_blank"
          rel="noreferrer noopener"
        >
          Read at source ↗
        </a>
      </div>
    </article>
  )
}
