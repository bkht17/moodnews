import { useEffect, useState } from 'react'
import FactCheckBadge from './FactCheckBadge.jsx'
import { getArticle } from '../api/client.js'
import { formatDate } from '../utils/format.js'

// Split the stored text into paragraphs where the source had them, so a
// scraped article that is one long block still renders as one block rather
// than pretending to have structure it does not.
function paragraphs(text) {
  return (text || '')
    .split(/\n{2,}/)
    .map((part) => part.trim())
    .filter(Boolean)
}

function Pane({ title, subtitle, children }) {
  return (
    <section className="pane">
      <header className="pane__head">
        <h3 className="pane__title">{title}</h3>
        {subtitle && <span className="pane__subtitle">{subtitle}</span>}
      </header>
      {children}
    </section>
  )
}

function Body({ text }) {
  return (
    <div className="pane__body">
      {paragraphs(text).map((paragraph, index) => (
        <p key={index}>{paragraph}</p>
      ))}
    </div>
  )
}

// The comparison view: the original on one side, the rewrite on the other.
//
// The original is fetched on its own first, without a mood, because that
// request is instant while a first-time rewrite has to call the LLM. The
// reader gets something to read straight away instead of an empty screen.
export default function ArticleView({ id, mood, moodLabel, onClose }) {
  const [article, setArticle] = useState(null)
  const [articleError, setArticleError] = useState(null)
  const [rewrite, setRewrite] = useState(null)
  const [rewriteError, setRewriteError] = useState(null)
  const [rewriting, setRewriting] = useState(true)

  useEffect(() => {
    setArticle(null)
    setArticleError(null)
    getArticle(id).then(setArticle).catch((err) => setArticleError(err.message))
  }, [id])

  useEffect(() => {
    let cancelled = false
    setRewriting(true)
    setRewrite(null)
    setRewriteError(null)

    getArticle(id, mood)
      .then((data) => {
        // A slow rewrite must not overwrite a newer mood's result.
        if (cancelled) return
        setArticle((current) => current || data)
        setRewrite(data.rewrite)
        setRewriteError(data.rewrite_error)
      })
      .catch((err) => {
        if (!cancelled) setRewriteError({ code: 'request_failed', message: err.message })
      })
      .finally(() => {
        if (!cancelled) setRewriting(false)
      })

    return () => {
      cancelled = true
    }
  }, [id, mood])

  // Escape closes the view, as readers expect of anything that covers the page.
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  if (articleError) {
    return (
      <div className="notice notice--error" role="alert">
        <p>Could not load this article: {articleError}</p>
        <button type="button" className="button" onClick={onClose}>
          Back to news
        </button>
      </div>
    )
  }

  const published = article?.published_at || article?.fetched_at

  return (
    <div className="article">
      <button type="button" className="article__back" onClick={onClose}>
        ← Back to news
      </button>

      {!article ? (
        <p className="status">Loading article&hellip;</p>
      ) : (
        <>
          <header className="article__head">
            <div className="article__meta">
              <span className="card__source">{article.source_name}</span>
              <span className="article__date">{formatDate(published)}</span>
              {article.facts?.verbatim_total > 0 && (
                <span
                  className="article__facts"
                  title="Numbers, dates and quotes extracted from the original before rewriting. Every rewrite is checked against them."
                >
                  {article.facts.verbatim_total} anchor facts protected
                </span>
              )}
            </div>
            <h2 className="article__title">{article.title}</h2>
          </header>

          <div className="compare">
            <Pane title="Original" subtitle={article.source_name}>
              <Body text={article.original_text} />
              <a
                className="pane__link"
                href={article.source_url}
                target="_blank"
                rel="noreferrer noopener"
              >
                Read the original at {article.source_name} ↗
              </a>
            </Pane>

            <Pane
              title="Rewritten"
              subtitle={moodLabel ? `${moodLabel} tone` : mood}
            >
              {rewriting && (
                <div className="pane__body pane__body--muted">
                  <p>
                    Rewriting this article in a {moodLabel?.toLowerCase() || mood}{' '}
                    tone&hellip;
                  </p>
                  <p className="pane__note">
                    The first rewrite of an article in a given mood calls the
                    language model and is then cached, so switching back to it
                    is instant.
                  </p>
                </div>
              )}

              {!rewriting && rewriteError && (
                <div className="notice notice--inline" role="status">
                  <p>
                    {rewriteError.code === 'llm_not_configured'
                      ? 'Mood rewriting is not available: no LLM API key is configured.'
                      : rewriteError.code === 'llm_refused'
                        ? 'This article could not be retold in this mood.'
                        : 'The rewrite could not be produced.'}
                  </p>
                  <p className="notice__detail">{rewriteError.message}</p>
                  <p className="notice__detail">
                    The original article is shown beside this panel and is
                    unaffected.
                  </p>
                </div>
              )}

              {!rewriting && rewrite && (
                <>
                  {/* The badge sits directly above the rewritten text: the
                      verdict belongs with the thing it is a verdict about. */}
                  <FactCheckBadge factCheck={rewrite.fact_check} />
                  <Body text={rewrite.text} />
                  <div className="pane__foot">
                    <a
                      className="pane__link"
                      href={article.source_url}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      Compare with the source ↗
                    </a>
                    <span className="pane__provenance">
                      {rewrite.model && `Rewritten by ${rewrite.model}`}
                      {rewrite.from_cache && ' · from cache'}
                    </span>
                  </div>
                </>
              )}
            </Pane>
          </div>
        </>
      )}
    </div>
  )
}
