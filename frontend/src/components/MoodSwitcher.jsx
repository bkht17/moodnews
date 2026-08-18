// The global mood switcher: one choice that applies to every article opened.
//
// Rendered as a radio group rather than a <select> so every option is visible
// at once - the moods are the point of the product, not a settings detail.
export default function MoodSwitcher({ moods, value, onChange, disabled }) {
  const selected = moods.find((mood) => mood.key === value)

  return (
    <div className="mood-switcher">
      <div className="mood-switcher__head">
        <span className="mood-switcher__label" id="mood-switcher-label">
          Mood
        </span>
        <span className="mood-switcher__hint">
          {selected
            ? selected.description
            : 'Choose the tone articles are retold in'}
        </span>
      </div>

      <div
        className="mood-switcher__options"
        role="radiogroup"
        aria-labelledby="mood-switcher-label"
      >
        {moods.map((mood) => (
          <button
            key={mood.key}
            type="button"
            role="radio"
            aria-checked={mood.key === value}
            className={`mood-pill${mood.key === value ? ' mood-pill--active' : ''}`}
            onClick={() => onChange(mood.key)}
            disabled={disabled}
            title={mood.description}
          >
            {mood.label}
          </button>
        ))}
      </div>
    </div>
  )
}
