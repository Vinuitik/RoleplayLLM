// PAGE — the only layer that fetches. Everything below receives props, which
// keeps the components dumb and this file the single place to look when the
// data flow is wrong.

import { useCallback, useEffect, useState } from 'react'
import { api, clearGameId, savedGameId, saveGameId } from '../api'
import { Button } from '../components/atoms'
import { ChatLog, Composer, DMPanel, StatusBar } from '../components/organisms'

export default function PlayPage() {
  const [gameId, setGameId] = useState(savedGameId())
  const [entries, setEntries] = useState([])
  const [suggestions, setSuggestions] = useState([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [time, setTime] = useState('')
  const [turn, setTurn] = useState(0)
  const [meters, setMeters] = useState([])
  const [providers, setProviders] = useState(null)
  const [dmOpen, setDmOpen] = useState(false)
  const [dm, setDm] = useState(null)
  const [truth, setTruth] = useState(null)

  // ── boot: resume the saved game, or offer a new one ──────────────────
  useEffect(() => {
    if (!gameId) return
    api.history(gameId)
      .then((history) => {
        setEntries(history)
        setTurn(history.length ? history[history.length - 1].turn : 0)
      })
      // A saved id pointing at a game the server no longer has (fresh volume,
      // deleted save) must not wedge the app on a blank screen.
      .catch(() => { clearGameId(); setGameId(null) })
  }, [gameId])

  // Poll the router so a stalled turn is explicable. Cheap call, and it only
  // runs while the tab is open.
  useEffect(() => {
    const load = () => api.providers().then(setProviders).catch(() => setProviders(null))
    load()
    const timer = setInterval(load, 20000)
    return () => clearInterval(timer)
  }, [])

  const refreshDM = useCallback(() => {
    if (!gameId) return
    api.dm(gameId).then((data) => {
      setDm(data)
      setMeters(data.truth.meters.filter((m) => !m.hidden))
      setTime(data.time_of_day)
    }).catch(() => {})
  }, [gameId])

  useEffect(() => { if (gameId) refreshDM() }, [gameId, turn, refreshDM])

  async function startGame() {
    setBusy(true); setError(null)
    try {
      const game = await api.newGame('The Hand of the King')
      saveGameId(game.game_id)
      setGameId(game.game_id)
      setEntries([{ turn: 0, narration: game.narration, player_action: '' }])
      setSuggestions(game.suggested_actions)
      setTime(game.time_of_day)
      setTurn(0)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function act() {
    const text = draft.trim()
    if (!text || busy) return
    setBusy(true); setError(null)
    // Optimistically show the action so the UI responds instantly — a turn can
    // take many seconds across several providers.
    setEntries((prev) => [...prev, { turn: turn + 1, player_action: text, narration: '' }])
    setDraft('')
    try {
      const result = await api.takeTurn(gameId, text)
      setEntries((prev) => [
        ...prev.slice(0, -1),
        { turn: result.turn, player_action: text, narration: result.narration,
          time: result.time_of_day },
      ])
      setSuggestions(result.suggested_actions)
      setTime(result.time_of_day)
      setTurn(result.turn)
    } catch (e) {
      setError(e.message)
      setEntries((prev) => prev.slice(0, -1))   // roll the optimistic entry back
      setDraft(text)                            // and give the player their text
    } finally { setBusy(false) }
  }

  async function rewind(toTurn) {
    if (busy) return
    setBusy(true)
    try {
      const result = await api.rewind(gameId, toTurn)
      setEntries(result.history)
      setTurn(result.turn)
      setTime(result.time_of_day)
      setSuggestions([])
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function loadTruth() {
    try { setTruth(await api.truth(gameId)) } catch { /* panel shows a spinner */ }
  }

  if (!gameId) {
    return (
      <main className="shell shell--empty">
        <div className="intro">
          <h1>The Hand of the King</h1>
          <p>
            You are Orys Ashwood, nine days into the office. The king is dying and
            everyone at the table is lying about something different.
          </p>
          <p className="intro__note">
            The world moves whether or not you do. Nobody — including the narrator —
            knows what is true except the engine.
          </p>
          <Button onClick={startGame} disabled={busy}>
            {busy ? 'shuffling the deck…' : 'Take the office'}
          </Button>
          {error && <p className="error">{error}</p>}
        </div>
      </main>
    )
  }

  return (
    <main className={`shell ${dmOpen ? 'shell--dm' : ''}`}>
      <div className="shell__play">
        <StatusBar
          time={time} turn={turn} meters={meters} providers={providers}
          onRewind={rewind} busy={busy}
          dmOpen={dmOpen} onToggleDM={() => { setDmOpen(!dmOpen); refreshDM() }}
        />
        <ChatLog entries={entries} busy={busy} />
        {error && <p className="error">{error}</p>}
        <Composer
          value={draft} onChange={setDraft} onSubmit={act}
          busy={busy} suggestions={suggestions}
        />
      </div>
      {dmOpen && (
        <DMPanel data={dm} truth={truth} onTruth={loadTruth}
                 onClose={() => setDmOpen(false)} />
      )}
    </main>
  )
}
