// MOLECULES — small compositions of atoms with one job each. Still no fetching:
// they take data as props so they stay trivially reusable and testable.

import { Badge, Button, MeterBar } from '../atoms'

export function Message({ role, text, time }) {
  return (
    <article className={`msg msg--${role}`}>
      {time && <header className="msg__time">{time}</header>}
      <div className="msg__body">
        {text.split('\n').map((line, i) =>
          line.trim() ? <p key={i}>{line}</p> : <br key={i} />
        )}
      </div>
    </article>
  )
}

export function Suggestions({ items, onPick, disabled }) {
  if (!items?.length) return null
  return (
    <div className="suggestions">
      {items.map((item, i) => (
        <button
          key={i}
          className="suggestion"
          disabled={disabled}
          onClick={() => onPick(item)}
        >
          {item}
        </button>
      ))}
    </div>
  )
}

export function MeterPanel({ meters }) {
  if (!meters?.length) return null
  return (
    <div className="meters">
      {meters.map((m) => (
        <MeterBar
          key={m.id || m.label}
          label={m.label}
          value={m.value}
          max={m.max || 100}
          hidden={m.hidden}
        />
      ))}
    </div>
  )
}

// Surfaces the router's state so a slow turn is explicable rather than
// mysterious — "every provider is on a 429 bench" is very different from a bug.
export function ProviderStatus({ providers }) {
  if (!providers || providers.error) {
    return <Badge tone="warn">wrapper offline</Badge>
  }
  const entries = Object.entries(providers)
  const ready = entries.filter(([, p]) => p.configured && !p.cooldown_s)
  return (
    <div className="providers">
      {entries.filter(([, p]) => p.configured).map(([name, p]) => (
        <Badge
          key={name}
          tone={p.cooldown_s ? 'warn' : 'ok'}
          title={`ok ${p.ok} / failed ${p.failed}${
            p.cooldown_s ? ` — cooling ${p.cooldown_s}s` : ''
          }`}
        >
          {name}
          {p.cooldown_s ? ` ${p.cooldown_s}s` : ''}
        </Badge>
      ))}
      {ready.length === 0 && <Badge tone="warn">all providers cooling</Badge>}
    </div>
  )
}

export function CharacterKnowledge({ name, location, alive, knows }) {
  return (
    <details className="know">
      <summary>
        <strong>{name}</strong>
        <span className="know__loc">{location}</span>
        {!alive && <Badge tone="warn">dead</Badge>}
        <span className="know__count">{knows.length}</span>
      </summary>
      <ul>
        {knows.map((k, i) => (
          <li key={i} className={k.stance === 'knows' ? 'k-knows' : 'k-suspects'}>
            {k.content}
            <span className="know__meta">
              {k.stance}
              {k.confidence < 1 ? ` ${k.confidence}` : ''}
              {k.source ? ` — via ${k.source}` : ''}
            </span>
          </li>
        ))}
      </ul>
    </details>
  )
}

export function TurnControls({ turn, onRewind, disabled }) {
  return (
    <div className="turn-controls">
      <span className="turn-controls__label">turn {turn}</span>
      <Button variant="ghost" disabled={disabled || turn < 1}
              onClick={() => onRewind(turn - 1)}>
        rewind
      </Button>
    </div>
  )
}
