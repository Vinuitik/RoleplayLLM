// Every call goes to a same-origin /api, which nginx proxies to the engine.
// Nothing here knows the engine's host or port, so the phone, the desktop and
// the Cloudflare tunnel all use identical code.

const BASE = '/api'

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!response.ok) {
    const body = await response.text()
    throw new Error(`${response.status}: ${body.slice(0, 200)}`)
  }
  return response.json()
}

export const api = {
  health: () => request('/health'),
  providers: () => request('/providers'),

  newGame: (title = '', seed = '') =>
    request('/games', { method: 'POST', body: JSON.stringify({ title, seed }) }),

  // Session Zero. Slow on purpose — it builds a whole world before play starts,
  // which is what stops the engine inventing entities mid-game. Given its own
  // long timeout because it runs on the good model and is not a turn.
  generateGame: (premise, { characters = 7, facts = 14, saveAs = '' } = {}) =>
    request('/games/generate', {
      method: 'POST',
      body: JSON.stringify({ premise, characters, facts, save_as: saveAs }),
    }),
  listGames: () => request('/games'),
  deleteGame: (id) => request(`/games/${id}`, { method: 'DELETE' }),
  history: (id) => request(`/games/${id}/history`),

  takeTurn: (id, text, { conversations = true, scenePasses = 3 } = {}) =>
    request(`/games/${id}/turn`, {
      method: 'POST',
      body: JSON.stringify({ text, conversations, scene_passes: scenePasses }),
    }),

  // Diagnostics: every model call in this game, chronologically.
  telemetry: (id) => request(`/games/${id}/telemetry`),
  telemetryHealth: (id = '') =>
    request(`/telemetry/health${id ? `?game_id=${id}` : ''}`),

  rewind: (id, turn) =>
    request(`/games/${id}/rewind/${turn}`, { method: 'POST' }),

  dm: (id) => request(`/games/${id}/dm`),
  truth: (id) => request(`/games/${id}/truth`),
}

// The active game id survives reloads and app restarts — closing the PWA on a
// phone should never lose your game.
const GAME_KEY = 'rp.gameId'
export const savedGameId = () => localStorage.getItem(GAME_KEY)
export const saveGameId = (id) => localStorage.setItem(GAME_KEY, id)
export const clearGameId = () => localStorage.removeItem(GAME_KEY)
