/** A deliberately tiny RulesModule for app-engine's own multiplayer tests. It carries no
 * game data: every match is "pass until someone concedes", which is enough to exercise
 * admission, private views, idempotency, clocks, chat, signaling, tournaments, friends and
 * storage. Real games ship their own rules module; see server.mjs. */
export const RULES_VERSION = 'fake-rules-1';
export const CATALOG_DIGEST = 'fakecafe';
const mk = (deck, name) => ({name, hand: ['h1', 'h2', 'h3'], deck: deck.cards.slice(), cards: deck.cards.length});

export function createMatch(decks, seed) {
  if (!Array.isArray(decks) || decks.length !== 2) throw new Error('Two decks are required.');
  return {version: 1, rulesVersion: RULES_VERSION, catalogDigest: CATALOG_DIGEST, seed, phase: 'playing', actor: seed % 2, turn: 1, winner: null, log: [{id: 1, text: 'Match started.'}], players: decks.map((d, i) => mk(d, d.name || `Player ${i + 1}`))};
}
export function legalActions(state, seat) {
  if (state.phase !== 'playing') return [];
  const concede = {id: 'concede::::::', kind: 'concede', label: 'Concede match'};
  if (state.actor !== seat) return [concede];
  return [{id: `pass:${state.turn}`, kind: 'pass', label: 'Pass'}, concede];
}
export function applyAction(state, seat, action) {
  const id = typeof action === 'string' ? action : action?.id;
  const offered = legalActions(state, seat).find(a => a.id === id);
  if (!offered) return {ok: false, error: {message: 'That action is not offered.'}};
  const next = structuredClone(state);
  if (offered.kind === 'concede') { next.phase = 'over'; next.winner = 1 - seat; next.log.push({id: next.log.length + 1, text: `Seat ${seat} conceded.`}); }
  else { next.turn++; next.actor = 1 - seat; next.log.push({id: next.log.length + 1, text: `Seat ${seat} passed.`}); if (next.turn > 40) { next.phase = 'over'; next.winner = -1; } }
  return {ok: true, state: next};
}
export function viewFor(state, seat) {
  const view = structuredClone(state);
  delete view.seed;
  view.player = seat;
  view.players = view.players.map((p, i) => ({name: p.name, cards: p.cards, hand: i === seat ? p.hand : [], handCount: p.hand.length}));
  return view;
}
export function validateDeck(deck) {
  const valid = !!deck && Array.isArray(deck.cards) && deck.cards.length >= 3 && Array.isArray(deck.legends);
  return valid ? {valid: true} : {valid: false, errors: ['A deck needs at least three cards.']};
}
export function generateDeck(meta = {}, seed = 1) {
  return {name: meta.name || `Crew ${seed}`, legends: ['l1'], cards: ['c1', 'c2', 'c3', `c${seed % 7 + 4}`]};
}
export function chooseAction(view, actions) { return actions.find(a => a.kind !== 'concede') || null; }

export const engine = {createMatch, legalActions, applyAction, viewFor, validateDeck, generateDeck, chooseAction, rulesVersion: RULES_VERSION, catalogDigest: CATALOG_DIGEST};
export default engine;
