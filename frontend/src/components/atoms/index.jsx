// ATOMS — no app knowledge, no data fetching, no layout opinions beyond their
// own box. Everything above this layer composes these; nothing here imports
// from above it. That one-way rule is what keeps atomic design useful instead
// of just being folders.

export function Button({ children, variant = 'primary', ...rest }) {
  return (
    <button className={`btn btn--${variant}`} {...rest}>
      {children}
    </button>
  )
}

export function Badge({ children, tone = 'neutral', title }) {
  return <span className={`badge badge--${tone}`} title={title}>{children}</span>
}

export function Spinner({ label = 'thinking' }) {
  return (
    <span className="spinner" role="status" aria-live="polite">
      <span className="spinner__dot" />
      <span className="spinner__dot" />
      <span className="spinner__dot" />
      <span className="sr-only">{label}</span>
    </span>
  )
}

// Meters are the numeric state made legible. `hidden` renders the DM-only
// styling — the player's own view never receives those at all, so this is
// purely for the debug panel.
export function MeterBar({ label, value, max = 100, hidden = false }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100))
  return (
    <div className={`meter ${hidden ? 'meter--hidden' : ''}`}>
      <div className="meter__head">
        <span className="meter__label">{label}</span>
        <span className="meter__value">{value}</span>
      </div>
      <div className="meter__track">
        <div className="meter__fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export function Field({ value, onChange, onSubmit, disabled, placeholder }) {
  return (
    <textarea
      className="field"
      rows={2}
      value={value}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        // Enter sends, Shift+Enter newlines — chat convention. Without the
        // shift check, composing a two-line action is impossible.
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault()
          onSubmit()
        }
      }}
    />
  )
}

export function Divider({ children }) {
  return <div className="divider">{children && <span>{children}</span>}</div>
}
