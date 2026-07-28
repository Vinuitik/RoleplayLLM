// PAGE — the only layer that fetches. Everything below receives props, which
// keeps the components dumb and this file the single place to look when the
// data flow is wrong.

import { useCallback, useEffect, useState } from 'react'
import { api, clearGameId, savedGameId, saveGameId } from '../api'
import { ChatLog, Composer, DMPanel, StartScreen, StatusBar } from '../components/organisms'

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
  // Worldgen only: repairs made to the generated spec, and a progress line.
  const [report, setReport] = useState([])
  const [phase, setPhase] = useState('')
  // True from the moment a game is created until the player acts, so the boot
  // effect cannot overwrite the opening scene it was just handed.
  const [fresh, setFresh] = useState(false)
  const [scenarios, setScenarios] = useState([])
  const [title, setTitle] = useState('')

  useEffect(() => { api.scenarios().then(setScenarios).catch(() => setScenarios([])) }, [])

  // ── boot: resume the saved game, or offer a new one ──────────────────
  //
  // `fresh` guards a real bug: starting a game sets gameId, which fires this
  // effect, which replaced the opening scene we had just rendered with the
  // turn-0 row from the database. The opening is now persisted (so a genuine
  // reload replays it correctly), but a just-started game still must not have
  // its entries swapped out from under it mid-render.
  useEffect(() => {
    if (!gameId || fresh) return
    api.history(gameId)
      .then((history) => {
        setEntries(history)
        setTurn(history.length ? history[history.length - 1].turn : 0)
      })
      // A saved id pointing at a game the server no longer has (fresh volume,
      // deleted save) must not wedge the app on a blank screen.
      .catch(() => { clearGameId(); setGameId(null) })
  }, [gameId, fresh])

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

  async function startGame(scenarioId = 'seed', playAs = '') {
    setBusy(true); setError(null)
    try {
      const game = await api.newGame(scenarioId, playAs)
      openGame(game)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  // Shared by both ways in, so a generated world and an authored one land the
  // player in exactly the same state.
  function openGame(game) {
    setFresh(true)
    saveGameId(game.game_id)
    setGameId(game.game_id)
    setEntries([{ turn: 0, narration: game.narration, player_action: '' }])
    setSuggestions(game.suggested_actions || [])
    setTime(game.time_of_day)
    setTitle(game.title || '')
    setTurn(0)
  }

  // Session Zero. Deliberately a distinct call with its own progress copy: it
  // runs on the good model and takes real time, and a 40-second wait behind a
  // button labelled "new game" looks like a hang rather than like work.
  async function generateWorld(premise) {
    if (!premise || busy) return
    setBusy(true); setError(null); setReport([])
    setPhase('Writing the cast, the facts, and who is lying about what…')
    try {
      const game = await api.generateGame(premise)
      openGame(game)
      // Repairs made to the generated spec. Surfaced rather than swallowed: a
      // world that generated badly should be visible, not silently thin.
      setReport(game.report || [])
    } catch (e) {
      setError(e.message)
    } finally { setBusy(false); setPhase('') }
  }

  async function act() {
    const text = draft.trim()
    if (!text || busy) return
    setBusy(true); setError(null); setFresh(false)
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
        <StartScreen
          scenarios={scenarios}
          onStart={startGame}
          onGenerate={generateWorld}
          busy={busy}
          error={error}
          report={report}
          phase={phase}
        />
      </main>
    )
  }

  return (
    <main className={`shell ${dmOpen ? 'shell--dm' : ''}`}>
      <div className="shell__play">
        <StatusBar
          title={title}
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
