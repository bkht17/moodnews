// The fact-check badge shown on the rewritten side.
//
// Every part of this is driven by the backend's fact_check payload: the status
// comes from the two-layer pipeline, and the "n/m" count is the number of
// numbers, dates and quotes the programmatic layer actually matched in the
// rewritten text. Nothing here is decorative - a failed check renders as a
// failure, in red, with the missing facts listed underneath.

const STATUS_META = {
  passed: {
    icon: '✓',
    modifier: 'passed',
    label: 'Facts verified',
  },
  warning: {
    icon: '!',
    modifier: 'warning',
    label: 'Verified with a warning',
  },
  failed: {
    icon: '✕',
    modifier: 'failed',
    label: 'Fact check failed',
  },
  unchecked: {
    icon: '?',
    modifier: 'unchecked',
    label: 'Not fact-checked',
  },
}

export default function FactCheckBadge({ factCheck }) {
  if (!factCheck) return null

  const meta = STATUS_META[factCheck.status] || STATUS_META.unchecked
  const { verified, total, missing_facts: missing, contradictions } = factCheck

  // An article can genuinely contain no numbers, dates or quotes. Saying
  // "0/0 verified" would read as a failure, so say what actually happened.
  const count =
    total > 0
      ? `${verified}/${total}`
      : factCheck.status === 'unchecked'
        ? ''
        : 'no numbers, dates or quotes to check'

  const hasDetail = missing?.length > 0 || contradictions?.length > 0

  return (
    <div className={`factcheck factcheck--${meta.modifier}`}>
      <div className="factcheck__head">
        <span className="factcheck__icon" aria-hidden="true">
          {meta.icon}
        </span>
        <span className="factcheck__label">
          {meta.label}
          {count && <span className="factcheck__count"> {count}</span>}
        </span>
        {factCheck.attempts > 1 && (
          <span
            className="factcheck__retry"
            title="The first rewrite failed its fact check and was regenerated with a stricter prompt."
          >
            retried
          </span>
        )}
      </div>

      {factCheck.summary && (
        <p className="factcheck__summary">{factCheck.summary}</p>
      )}

      {hasDetail && (
        <details className="factcheck__detail">
          <summary>What the check found</summary>
          {contradictions?.length > 0 && (
            <>
              <p className="factcheck__detail-heading">
                Contradicts the original:
              </p>
              <ul>
                {contradictions.map((item, index) => (
                  <li key={`c-${index}`}>{item}</li>
                ))}
              </ul>
            </>
          )}
          {missing?.length > 0 && (
            <>
              <p className="factcheck__detail-heading">
                Missing from the rewrite:
              </p>
              <ul>
                {missing.map((item, index) => (
                  <li key={`m-${index}`}>{item}</li>
                ))}
              </ul>
            </>
          )}
        </details>
      )}

      <p className="factcheck__method">
        {factCheck.auditor === 'ok'
          ? 'Checked twice: numbers, dates and quotes matched against the original, then reviewed by a fact-checking auditor.'
          : 'Numbers, dates and quotes matched against the original. The second-layer auditor did not run.'}
      </p>
    </div>
  )
}
