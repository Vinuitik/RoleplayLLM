// ORGANISMS — full sections of the interface. These own layout and local UI
// state (scrolling, which tab is open) but still receive their data from the
// page above, so the fetching stays in one place.

import { useEffect, useRef, useState } from 'react'
import { Badge, Button, Divider, Field, Spinner } from '../atoms'
import {
  CharacterKnowledge, Message, MeterPanel, ProviderStatus, Suggestions,
  TurnControls,
} from '../molecules'

// ── Session Zero ────────────────────────────────────────────────────────────

// The start screen. Two ways in: pick a scenario that exists on disk, or
// generate a new world from a premise.
//
// Nothing here knows about any particular scenario. The list comes from
// /scenarios, so a world saved by worldgen appears beside the authored court
// with no frontend change — which is the point. This file used to hardcode one
// world's title, blurb and opening suggestions, and that made "swap the world"
// a UI edit.
export function StartScreen({ scenarios = [], onStart, onGenerate, busy, error,
                              report, phase }) {
  const [premise, setPremise] = useState('')
  const [mode, setMode] = useState('play')
  // Which scenario the player has opened to choose a character in. Null = the
  // list. Choosing WHO you are is a real decision and deserves its own step —
  // in this world, playing the poisoner is a different game from hunting him.
  const [picking, setPicking] = useState(null)

  // "Surprise me" FILLS THE FIELD rather than generating immediately. The
  // premise is the one input that shapes everything downstream, so the player
  // should always see it — and be able to edit it — before a world is built on
  // it. A button that skips straight to generation is a random spin, which is
  // exactly what we do not want as the default path.
  const seeds = [
    'A siege in its fourth month. The garrison commander suspects his own quartermaster.',
    'A generation ship, three decades out. The captain has been dead for a week and only two people know.',
    'A mining town where the company doctor has started falsifying death certificates.',
    'A monastery scriptorium where one of the copyists has been altering the text for years.',
    'A whaling station at the end of the season. The tally does not match the hold.',
    'A border checkpoint the week a ceasefire is signed. Someone is still selling passage.',
    'A hospital ward during a quarantine. The register has three names too few.',
    'A film set on its last week. The lead actor has not been seen in four days.',
  ]
  const examples = seeds.slice(0, 3)

  function surpriseMe() {
    const other = seeds.filter((s) => s !== premise)
    setPremise(other[Math.floor(Math.random() * other.length)])
  }

  return (
    <div className="intro">
      <h1>{mode === 'play' ? 'Choose a world' : 'Build a world'}</h1>

      <div className="intro__tabs">
        <Button variant={mode === 'play' ? 'primary' : 'ghost'}
                onClick={() => setMode('play')} disabled={busy}>
          Play
        </Button>
        <Button variant={mode === 'generate' ? 'primary' : 'ghost'}
                onClick={() => setMode('generate')} disabled={busy}>
          Generate
        </Button>
      </div>

      {mode === 'play' && picking ? (
        <>
          <p className="intro__note">
            <button className="link" onClick={() => setPicking(null)}
                    disabled={busy}>← all worlds</button>
          </p>
          <p>Who are you in this one?</p>
          <ul className="scenarios">
            {(picking.playable || []).map((who) => (
              <li key={who.id} className="scenario">
                <div className="scenario__text">
                  <strong>
                    {who.name}
                    {who.default && <span className="scenario__as"> · intended</span>}
                  </strong>
                  <span className="scenario__as">
                    {who.role}{who.location ? ` — ${who.location.replace(/_/g, ' ')}` : ''}
                  </span>
                  {who.wants?.length > 0 && <p>wants {who.wants.join('; ')}</p>}
                </div>
                <Button onClick={() => onStart(picking.id, who.id)} disabled={busy}>
                  {busy ? '…' : 'Play'}
                </Button>
              </li>
            ))}
          </ul>
        </>
      ) : mode === 'play' ? (
        <>
          {scenarios.length === 0 && !busy && (
            <p className="intro__note">
              No scenarios found on disk. Generate one instead.
            </p>
          )}
          <ul className="scenarios">
            {scenarios.map((s) => (
              <li key={s.id} className="scenario">
                <div className="scenario__text">
                  <strong>{s.title}</strong>
                  <span className="scenario__as">
                    {(s.playable || []).length} playable character
                    {(s.playable || []).length === 1 ? '' : 's'}
                  </span>
                  <p>{s.blurb}</p>
                </div>
                <Button onClick={() => setPicking(s)} disabled={busy}>
                  Choose
                </Button>
              </li>
            ))}
          </ul>
          <p className="intro__note">
            The world moves whether or not you do. Nobody — including the
            narrator — knows what is true except the engine.
          </p>
        </>
      ) : (
        <>
          <p>
            Describe a situation with a lie in it. The generator builds the
            characters, the facts, who believes what, and what they conceal —
            all of it before you take a single turn.
          </p>
          <Field
            value={premise}
            onChange={setPremise}
            onSubmit={() => premise.trim() && onGenerate(premise.trim())}
            disabled={busy}
            placeholder="A premise. Two sentences is plenty."
          />
          <ul className="intro__examples">
            {examples.map((example) => (
              <li key={example}>
                <button className="link" disabled={busy}
                        onClick={() => setPremise(example)}>
                  {example}
                </button>
              </li>
            ))}
          </ul>
          <div className="intro__actions">
            <Button onClick={() => onGenerate(premise.trim())}
                    disabled={busy || !premise.trim()}>
              {busy ? 'building the world…' : 'Build it'}
            </Button>
            <Button variant="ghost" onClick={surpriseMe} disabled={busy}>
              Surprise me
            </Button>
          </div>
          {busy && (
            <p className="intro__note">
              <Spinner label="generating" />{' '}
              {phase || 'This runs on the good model and takes a while. It happens once.'}
            </p>
          )}
        </>
      )}

      {error && <p className="error">{error}</p>}
      {report?.length > 0 && (
        <details className="intro__report">
          <summary>{report.length} note{report.length === 1 ? '' : 's'} from generation</summary>
          <ul>{report.map((line, i) => <li key={i}>{line}</li>)}</ul>
        </details>
      )}
    </div>
  )
}

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
          <Spinner label="the world moves" />
          <span>the world moves…</span>
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

export function StatusBar({ title, time, turn, meters, providers, onRewind, busy,
                            onToggleDM, dmOpen }) {
  return (
    <header className="statusbar">
      <div className="statusbar__row">
        <strong className="statusbar__time">
          {title ? `${title} — ` : ''}{time || '—'}
        </strong>
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
