// ORGANISMS — full sections of the interface. These own layout and local UI
// state (scrolling, which tab is open) but still receive their data from the
// page above, so the fetching stays in one place.

import { useEffect, useRef, useState } from 'react'
import { Badge, Button, Divider, Field, Spinner } from '../atoms'
import {
  CharacterKnowledge, Message, MeterPanel, ProviderStatus, Suggestions,
  TurnControls,
} from '../molecules'

export function ChatLog({ entries, busy }) {
  const endRef = useRef(null)

  // Scroll on new entries AND when the spinner appears, so submitting an action
  // always brings the pending state into view.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [entries.length, busy])

  return (
    <div className="chatlog">
      {entries.map((entry, i) => (
        <div key={i}>
          {entry.player_action && (
            <Message role="player" text={entry.player_action} />
          )}
          {entry.narration && (
            <Message role="narrator" text={entry.narration} time={entry.time} />
          )}
        </div>
      ))}
      {busy && (
        <div className="chatlog__pending">
          <Spinner label="the court moves" />
          <span>the court moves…</span>
        </div>
      )}
      <div ref={endRef} />
    </div>
  )
}

export function Composer({ value, onChange, onSubmit, busy, suggestions }) {
  return (
    <div className="composer">
      <Suggestions
        items={suggestions}
        disabled={busy}
        onPick={(text) => { onChange(text); }}
      />
      <div className="composer__row">
        <Field
          value={value}
          onChange={onChange}
          onSubmit={onSubmit}
          disabled={busy}
          placeholder="What do you do?  (Enter sends, Shift+Enter for a new line)"
        />
        <Button onClick={onSubmit} disabled={busy || !value.trim()}>
          {busy ? '…' : 'Act'}
        </Button>
      </div>
    </div>
  )
}

export function StatusBar({ time, turn, meters, providers, onRewind, busy,
                            onToggleDM, dmOpen }) {
  return (
    <header className="statusbar">
      <div className="statusbar__row">
        <strong className="statusbar__time">{time || '—'}</strong>
        <TurnControls turn={turn} onRewind={onRewind} disabled={busy} />
        <Button variant="ghost" onClick={onToggleDM}>
          {dmOpen ? 'hide DM' : 'DM view'}
        </Button>
      </div>
      <MeterPanel meters={meters} />
      <ProviderStatus providers={providers} />
    </header>
  )
}

// The DM panel: full truth beside every character's projection. This is the tool
// that proves the hidden state never leaks — if a secret appears in the wrong
// character's column, the bug is found before the player finds the exploit.
export function DMPanel({ data, onClose, onTruth, truth }) {
  const [tab, setTab] = useState('truth')
  if (!data) return null

  return (
    <aside className="dm">
      <div className="dm__head">
        <div className="dm__tabs">
          {['truth', 'projections', 'chronicle', 'report'].map((t) => (
            <button
              key={t}
              className={`dm__tab ${tab === t ? 'is-active' : ''}`}
              onClick={() => { setTab(t); if (t === 'report') onTruth() }}
            >
              {t}
            </button>
          ))}
        </div>
        <Button variant="ghost" onClick={onClose}>close</Button>
      </div>

      {tab === 'truth' && (
        <div className="dm__body">
          <Divider>plots</Divider>
          {data.truth.plots.map((p) => (
            <div key={p.name} className="dm__plot">
              <strong>{p.name}</strong>
              <Badge tone={p.active ? 'ok' : 'neutral'}>
                {p.stage}: {p.stage_name}
              </Badge>
              <div className="dm__members">{p.members.join(', ')}</div>
            </div>
          ))}

          <Divider>meters</Divider>
          <MeterPanel meters={data.truth.meters} />
          <div className="dm__formulas">
            {data.truth.meters.map((m) => (
              <code key={m.id}>{m.id} = {m.formula}</code>
            ))}
          </div>

          <Divider>facts (engine truth)</Divider>
          <ul className="dm__facts">
            {data.truth.facts.map((f) => (
              <li key={f.id} className={f.is_true ? 'f-true' : 'f-false'}>
                <Badge tone={f.is_true ? 'ok' : 'warn'}>
                  {f.is_true ? 'true' : 'FALSE'}
                </Badge>
                {f.content}
              </li>
            ))}
          </ul>
        </div>
      )}

      {tab === 'projections' && (
        <div className="dm__body">
          <p className="dm__note">
            Each character's view. Nothing outside their list ever reaches a
            prompt — and no view carries a truth value, not even the narrator's.
          </p>
          {Object.entries(data.projections).map(([id, p]) => (
            <CharacterKnowledge key={id} {...p} />
          ))}
        </div>
      )}

      {tab === 'chronicle' && (
        <div className="dm__body">
          <ul className="dm__chronicle">
            {data.chronicle.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </div>
      )}

      {tab === 'report' && (
        <div className="dm__body">
          {!truth && <Spinner />}
          {truth && (
            <>
              <p className="dm__note">
                {truth.deceptions} belief(s) you hold are false.{' '}
                {truth.never_learned} fact(s) you never learned.
              </p>
              <ul className="dm__facts">
                {truth.facts.map((f, i) => (
                  <li key={i} className={f.deceived ? 'f-false' : ''}>
                    <Badge tone={f.deceived ? 'warn' : 'neutral'}>
                      {f.you_believed}
                    </Badge>
                    {f.content}
                    {f.told_by && <em> — told by {f.told_by}</em>}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </aside>
  )
}
