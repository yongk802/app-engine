import {createHash, createHmac, randomBytes, timingSafeEqual} from 'node:crypto';
import http from 'node:http';
import https from 'node:https';
import {mkdir, readFile, open, rename, unlink} from 'node:fs/promises';
import path from 'node:path';
import {PROTOCOL_VERSION, RoomError, RoomServiceContract} from './contract.mjs';
import {installTournaments} from './tournaments.mjs';
import {installFriends} from './friends.mjs';

const MAX_ROOMS = 100;
const MAX_REQUESTS = 10000;
const MAX_BODY = 65536;
const copy = value => structuredClone(value);
const secret = () => randomBytes(32).toString('base64url');
const digest = value => createHash('sha256').update(value).digest('hex');
const fail = (code, message, status = 400) => { throw new RoomError(code, message, status); };
const object = value => !!value && typeof value === 'object' && !Array.isArray(value) && [Object.prototype, null].includes(Object.getPrototypeOf(value));
const eqHash = (value, hash) => typeof value === 'string' && typeof hash === 'string' && /^[a-f0-9]{64}$/.test(hash) && timingSafeEqual(Buffer.from(digest(value), 'hex'), Buffer.from(hash, 'hex'));
const canonical = value => JSON.stringify(value, (_, v) => object(v) ? Object.fromEntries(Object.keys(v).sort().map(k => [k, v[k]])) : v);
const fields = {
  create: ['name', 'protocol', 'rulesVersion', 'catalogDigest', 'turnTimer'],
  notify: ['roomId', 'requestId', 'webhook'],
  join: ['roomId', 'invite', 'name', 'protocol', 'rulesVersion', 'catalogDigest', 'requestId'],
  snapshot: ['roomId'], accept: ['roomId', 'requestId', 'joinId'], reject: ['roomId', 'requestId', 'joinId'],
  deck: ['roomId', 'requestId', 'deck'], ready: ['roomId', 'requestId', 'ready'],
  action: ['roomId', 'requestId', 'expectedRevision', 'actionId'], message: ['roomId', 'requestId', 'text'],
  signal: ['roomId', 'requestId', 'callId', 'kind', 'data'], leave: ['roomId', 'requestId'], rematch: ['roomId', 'requestId'],
  dailySubmit: ['day', 'name', 'playerId', 'actions', 'requestId', 'protocol', 'rulesVersion', 'catalogDigest'],
  dailyBoard: ['day', 'playerId', 'protocol', 'rulesVersion', 'catalogDigest'],
};
const DAILY_DAYS_KEPT = 60;
let TOURNEY_FIELDS = {}, FRIEND_FIELDS = {};
const TURN_TIMERS = {'24h': 86400000, '72h': 259200000};
const CONNECTED_WINDOW = 30000;
const CONCEDE = 'concede::::::';
const privateHost = host => /^(localhost|127\.|10\.|192\.168\.|169\.254\.|::1$|\[::1\]|fc|fd)/i.test(host) || /^172\.(1[6-9]|2\d|3[01])\./.test(host) || /\.(local|lan|home|internal)$/i.test(host);
/** Fire-and-forget webhook POST. LAN and loopback hosts may present the operator's own certificate. */
export function deliverWebhook(webhook, payload, {timeout = 5000} = {}) {
  return new Promise(resolve => {
    let url;
    try { url = new URL(webhook.url); } catch { resolve(false); return; }
    if (!['http:', 'https:'].includes(url.protocol)) { resolve(false); return; }
    const body = JSON.stringify(payload);
    const request = (url.protocol === 'https:' ? https : http).request(url, {method: 'POST', timeout, headers: {'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body), 'X-App-Notify-Token': webhook.token}, ...(url.protocol === 'https:' && privateHost(url.hostname) ? {rejectUnauthorized: false} : {})}, response => { response.resume(); resolve(response.statusCode >= 200 && response.statusCode < 300); });
    request.on('timeout', () => request.destroy(new Error('timeout')));
    request.on('error', () => resolve(false));
    request.end(body);
  });
}
const PLAYER_ID = /^[A-Za-z0-9_-]{16,64}$/;
function text(value, name, maximum) {
  if (typeof value !== 'string' || !value.trim() || value.length > maximum || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) fail('INVALID_REQUEST', `Invalid ${name}.`);
  return value;
}
function fingerprint(op, payload) {
  const data = {...payload}; delete data.requestId; delete data.invite;
  return digest(canonical({op, ...data}));
}

/** Game-agnostic room service. The game reaches it through the `engine` option (a
 * RulesModule: createMatch, legalActions, applyAction, viewFor, validateDeck, generateDeck,
 * rulesVersion, catalogDigest), an optional `chooseAction` policy for AI seats, and an
 * optional `daily` plugin ({validDay, replayDaily, gigResult, rankBoard}) for a seeded
 * challenge board. Nothing here imports a game.
 * The entire dispatch is serialized. A candidate copy is published only after
 * its private durable state has been replaced atomically. No HTTP concern lives here. */
export class RoomService extends RoomServiceContract {
  constructor(options) {
    super(options);
    this.engine = options.engine;
    this.rulesVersion = options.rulesVersion;
    this.catalogDigest = options.catalogDigest;
    this.storageDir = path.resolve(options.storageDir);
    this.now = options.now || Date.now;
    this.deliver = options.deliver || deliverWebhook;
    this.chooseAction = options.chooseAction ?? options.engine?.chooseAction ?? null;
    this.dailyPlugin = options.daily ?? options.engine?.daily ?? null;
    this.rooms = new Map();
    this.tail = Promise.resolve();
    this.initialized = false;
    this.file = path.join(this.storageDir, 'rooms.json');
    this.dailyFile = path.join(this.storageDir, 'daily.json');
    this.dailyDays = {};
    this.tournamentFile = path.join(this.storageDir, 'tournaments.json');
    this.tournaments = new Map();
    this.friendFile = path.join(this.storageDir, 'friends.json');
    this.players = new Map();
  }

  async init() {
    if (this.initialized) return;
    try {
      await mkdir(this.storageDir, {recursive: true, mode: 0o700});
      let saved;
      try { saved = JSON.parse(await readFile(this.file, 'utf8')); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      const loaded = new Map();
      if (saved !== undefined) {
        if (!object(saved)) throw new Error('Invalid room storage.');
        if (saved.protocol !== PROTOCOL_VERSION || saved.rulesVersion !== this.rulesVersion || saved.catalogDigest !== this.catalogDigest || !Array.isArray(saved.rooms) || saved.rooms.length > 1000) throw new Error('Invalid room storage.');
        for (const room of saved.rooms) {
          this.validateStored(room);
          if (loaded.has(room.id)) throw new Error('Duplicate room.');
          room.signals = [];
          // A process restart is not evidence that either browser is connected.
          for (const seat of room.seats.filter(Boolean)) seat.lastSeen = null;
          if (room.pending) room.pending.lastSeen = null;
          loaded.set(room.id, room);
        }
      }
      this.rooms = loaded;
      this.dailyDays = await this.loadDaily();
      this.tournaments = await this.loadTournaments();
      this.players = await this.loadFriends();
      this.initialized = true;
    } catch { fail('STORAGE_FAILURE', 'Room storage could not be loaded safely.', 503); }
  }

  validateStored(room) {
    const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
    const webhook = w => w == null || (object(w) && typeof w.url === 'string' && w.url.length <= 2048 && typeof w.token === 'string' && w.token.length <= 4096);
    const seat = s => object(s) && typeof s.name === 'string' && s.name.length <= 80 && hash(s.tokenHash) && typeof s.ready === 'boolean' && Array.isArray(s.requests) && s.requests.length <= MAX_REQUESTS && s.requests.every(r => object(r) && typeof r.id === 'string' && hash(r.fingerprint)) && webhook(s.webhook) && (s.ai === undefined || typeof s.ai === 'boolean');
    if (room.tournament != null && (!object(room.tournament) || typeof room.tournament.id !== 'string' || typeof room.tournament.matchId !== 'string')) throw new Error('Invalid room.');
    if (room.randomMatch !== undefined && typeof room.randomMatch !== 'boolean') throw new Error('Invalid room.');
    if (room.friendInvite != null && (!object(room.friendInvite) || typeof room.friendInvite.from !== 'string' || typeof room.friendInvite.to !== 'string' || !Number.isFinite(room.friendInvite.at) || (room.friendInvite.declined !== undefined && typeof room.friendInvite.declined !== 'boolean'))) throw new Error('Invalid room.');
    if (room.turnTimer != null && !Object.values(TURN_TIMERS).includes(room.turnTimer)) throw new Error('Invalid room.');
    if (room.turnDeadline != null && !Number.isFinite(room.turnDeadline)) throw new Error('Invalid room.');
    if (room.timeout != null && (!object(room.timeout) || ![0, 1].includes(room.timeout.seat) || !Number.isFinite(room.timeout.at))) throw new Error('Invalid room.');
    if (!object(room) || !/^[A-Za-z0-9_-]{22}$/.test(room.id) || !['lobby', 'playing', 'over', 'closed'].includes(room.status) || !Number.isSafeInteger(room.revision) || !Number.isSafeInteger(room.gameRevision) || !Array.isArray(room.seats) || room.seats.length !== 2 || !seat(room.seats[0]) || (room.seats[1] !== null && !seat(room.seats[1])) || (room.pending !== null && (!seat(room.pending) || typeof room.pending.id !== 'string')) || !hash(room.inviteHash) || !Number.isFinite(room.inviteExpires) || typeof room.inviteConsumed !== 'boolean' || !Array.isArray(room.joins) || room.joins.length > MAX_REQUESTS || !room.joins.every(j => object(j) && typeof j.id === 'string' && hash(j.fingerprint) && hash(j.tokenHash)) || !Array.isArray(room.messages) || room.messages.length > 100 || !room.messages.every(m => object(m) && [0, 1].includes(m.sender) && typeof m.id === 'string' && typeof m.text === 'string' && m.text.length <= 2000 && Number.isFinite(m.at))) throw new Error('Invalid room.');
    if (['playing', 'over'].includes(room.status) && !room.game) throw new Error('Missing game.');
    if (room.game) {
      if (room.game.rulesVersion !== this.rulesVersion || room.game.catalogDigest !== this.catalogDigest) throw new Error('Incompatible game.');
      this.engine.viewFor(copy(room.game), 0);
      this.engine.viewFor(copy(room.game), 1);
    }
    for (const s of room.seats.filter(Boolean)) if (s.deck && !this.engine.validateDeck(s.deck).valid) throw new Error('Invalid saved deck.');
  }

  async loadDaily() {
    if (!this.dailyPlugin) return {};
    let saved;
    try { saved = JSON.parse(await readFile(this.dailyFile, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; return {}; }
    if (!object(saved) || saved.version !== 1 || saved.rulesVersion !== this.rulesVersion || saved.catalogDigest !== this.catalogDigest || !object(saved.days)) throw new Error('Invalid daily storage.');
    const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
    for (const [day, entries] of Object.entries(saved.days)) {
      if (!this.dailyPlugin.validDay(day) || !object(entries) || Object.keys(entries).length > 1000) throw new Error('Invalid daily storage.');
      for (const [key, entry] of Object.entries(entries)) {
        if (!hash(key) || !object(entry) || typeof entry.name !== 'string' || entry.name.length > 40 || !['win', 'loss', 'tie'].includes(entry.outcome) || !Number.isSafeInteger(entry.turns) || !Number.isSafeInteger(entry.rounds) || !Array.isArray(entry.cred) || entry.cred.length !== 2 || !entry.cred.every(Number.isSafeInteger) || typeof entry.at !== 'string' || !hash(entry.actionsHash)) throw new Error('Invalid daily storage.');
      }
    }
    return saved.days;
  }

  /** Daily Gig results need no seat: the recorded actions are replayed against the shared
   * seed, so a board entry is proof of a finished match, never a self-reported number. */
  async daily(op, p) {
    if (!this.dailyPlugin) fail('MULTIPLAYER_UNAVAILABLE', 'This game has no daily board.', 503);
    this.compatible(p);
    if (!this.dailyPlugin.validDay(p.day)) fail('INVALID_REQUEST', 'Invalid gig day.');
    if (typeof p.playerId !== 'string' || !PLAYER_ID.test(p.playerId)) fail('INVALID_REQUEST', 'Invalid player ID.');
    const key = digest(`night-city-daily:${p.playerId}`);
    if (op === 'dailyBoard') return this.dailyBoard(p.day, key);
    text(p.requestId, 'request ID', 128);
    const name = text(p.name, 'player name', 40).trim();
    if (!Array.isArray(p.actions) || !p.actions.length || p.actions.length > 2000 || !p.actions.every(id => typeof id === 'string' && id.length > 0 && id.length <= 512)) fail('INVALID_REQUEST', 'Invalid action list.');
    const age = (this.now() - Date.parse(`${p.day}T00:00:00Z`)) / 86400000;
    if (!(age >= -1 && age < 8)) fail('BAD_STATE', 'This gig day is not open for results.', 409);
    const entries = this.dailyDays[p.day] || {}, existing = entries[key], actionsHash = digest(canonical(p.actions));
    if (existing && existing.actionsHash !== actionsHash) fail('REQUEST_CONFLICT', 'A different result was already posted for this gig.', 409);
    let final;
    try { final = this.dailyPlugin.replayDaily(this.engine, this.dailyPlugin.chooseAction ?? this.chooseAction, p.day, p.actions); }
    catch (error) { fail('ILLEGAL_ACTION', `The gig replay failed: ${error.message}`, 422); }
    if (final.phase !== 'over') fail('BAD_STATE', 'The gig is not finished.', 409);
    const entry = {name, ...this.dailyPlugin.gigResult(final), at: existing?.at ?? new Date(this.now()).toISOString(), actionsHash};
    const days = {...this.dailyDays, [p.day]: {...entries, [key]: entry}};
    const cutoff = new Date(this.now() - DAILY_DAYS_KEPT * 86400000).toISOString().slice(0, 10);
    for (const day of Object.keys(days)) if (day < cutoff) delete days[day];
    await this.commitDaily(days);
    return this.dailyBoard(p.day, key);
  }

  dailyBoard(day, key) {
    const entries = Object.entries(this.dailyDays[day] || {}).map(([k, e]) => ({name: e.name, outcome: e.outcome, turns: e.turns, rounds: e.rounds, cred: e.cred, at: e.at, self: k === key}));
    return copy({day, entries: this.dailyPlugin.rankBoard(entries).slice(0, 100), protocol: PROTOCOL_VERSION, rulesVersion: this.rulesVersion, catalogDigest: this.catalogDigest});
  }

  async commitDaily(days) {
    const data = JSON.stringify({version: 1, rulesVersion: this.rulesVersion, catalogDigest: this.catalogDigest, days});
    const temporary = path.join(this.storageDir, `.daily-${secret()}.tmp`);
    let handle;
    try {
      handle = await open(temporary, 'wx', 0o600);
      await handle.writeFile(data, 'utf8');
      await handle.sync();
      await handle.close(); handle = null;
      await rename(temporary, this.dailyFile);
    } catch {
      if (handle) await handle.close().catch(() => {});
      await unlink(temporary).catch(() => {});
      fail('STORAGE_FAILURE', 'The result could not be saved. Please retry.', 503);
    }
    this.dailyDays = days;
  }

  dispatch(op, payload, token = '', player = '') {
    // Capture at call time so queued callers cannot change a pending command.
    let frozen;
    try {
      const allowed = fields[op] || TOURNEY_FIELDS[op] || FRIEND_FIELDS[op];
      if (!object(payload) || !allowed || Object.keys(payload).some(k => !allowed.includes(k)) || Buffer.byteLength(JSON.stringify(payload)) > MAX_BODY) fail('INVALID_REQUEST', 'Invalid command.');
      frozen = copy(payload);
      if (typeof token !== 'string' || token.length > 256) fail('AUTH_REQUIRED', 'A valid seat credential is required.', 401);
    } catch (error) { return Promise.reject(error instanceof RoomError ? error : new RoomError('INVALID_REQUEST', 'Invalid command.')); }
    const run = this.tail.then(() => this.run(op, frozen, token, player)).catch(error => {
      if (error instanceof RoomError) throw error;
      throw new RoomError('INTERNAL_ERROR', 'The room service could not process this command.', 500);
    });
    this.tail = run.catch(() => {});
    return run;
  }

  compatible(p) {
    if (p.protocol !== PROTOCOL_VERSION || p.rulesVersion !== this.rulesVersion || p.catalogDigest !== this.catalogDigest) fail('INCOMPATIBLE', 'The room requires matching protocol, rules and catalog versions.', 409);
  }

  newSeat(name, token) { return {name, tokenHash: digest(token), ready: false, deck: null, lastSeen: this.now(), requests: [], left: false}; }

  async run(op, p, token, player = '') {
    if (!this.initialized) fail('STORAGE_FAILURE', 'Room service is not initialized.', 503);
    if (op === 'dailySubmit' || op === 'dailyBoard') return this.daily(op, p);
    if (op.startsWith('tourney')) return this.tourney(op, p, token);
    if (op.startsWith('friend') || op.startsWith('queue')) return this.friend(op, p, token, player);
    if (op === 'create') {
      this.compatible(p);
      const name = text(p.name, 'player name', 80).trim();
      if ([...this.rooms.values()].filter(r => r.status !== 'closed').length >= MAX_ROOMS || this.rooms.size >= 1000) fail('CAPACITY', 'Room capacity has been reached.', 503);
      if (p.turnTimer != null && !Object.hasOwn(TURN_TIMERS, p.turnTimer)) fail('INVALID_REQUEST', 'Choose a turn timer of none, 24h or 72h.');
      const token = secret(), invite = secret(), id = randomBytes(16).toString('base64url');
      const room = {id, revision: 1, gameRevision: 0, status: 'lobby', seats: [this.newSeat(name, token), null], pending: null, inviteHash: digest(invite), inviteExpires: this.now() + 86400000, inviteConsumed: false, joins: [], game: null, messages: [], signals: [], turnTimer: p.turnTimer ? TURN_TIMERS[p.turnTimer] : null, turnDeadline: null, timeout: null};
      const result = {roomId: id, token, invite, seat: 0, snapshot: this.snapshot(room, 0)};
      await this.commit(room);
      return result;
    }
    text(p.roomId, 'room ID', 64);
    const existing = this.rooms.get(p.roomId);
    if (!existing) fail('ROOM_NOT_FOUND', 'Room not found.', 404);
    const room = copy(existing);
    const expired = this.expire(room);
    if (op === 'join') return this.join(room, p);
    if (!token) fail('AUTH_REQUIRED', 'A seat credential is required.', 401);
    const seatIndex = room.seats.findIndex(s => s && eqHash(token, s.tokenHash));
    const pending = seatIndex < 0 && room.pending && eqHash(token, room.pending.tokenHash);
    if (seatIndex < 0 && !pending && op === 'leave' && room.joins.some(j=>j.withdrawn && eqHash(token,j.tokenHash))) return this.snapshot(room,1,true);
    if (seatIndex < 0 && !pending) fail('AUTH_REQUIRED', 'A valid seat credential is required.', 401);
    if (pending) {
      if (op === 'leave') {
        text(p.requestId, 'request ID', 128);
        const admission=room.joins.find(j=>eqHash(token,j.tokenHash));
        if(admission)admission.withdrawn=true;
        room.pending=null;room.revision++;
        const result=this.snapshot(room,1,true);await this.commit(room);return result;
      }
      if (op !== 'snapshot') fail('FORBIDDEN', 'The host must accept you first.', 403);
      room.pending.lastSeen = this.now();
      const result = this.snapshot(room, 1, true);
      await this.commit(room);
      return result;
    }
    const seat = room.seats[seatIndex];
    if (op === 'snapshot') {
      seat.lastSeen = this.now();
      if (expired) room.revision++;
      const result = this.snapshot(room, seatIndex);
      await this.commit(room);
      if (expired) { this.announce(existing, room, seatIndex); await this.settleTournamentRoom(room); }
      return result;
    }
    text(p.requestId, 'request ID', 128);
    const fp = fingerprint(op, p), previous = seat.requests.find(r => r.id === p.requestId);
    if (previous) {
      if (previous.fingerprint !== fp) fail('REQUEST_CONFLICT', 'This request ID was already used for a different command.', 409);
      return this.snapshot(room, seatIndex);
    }
    if (seat.requests.length >= MAX_REQUESTS) fail('CAPACITY', 'This room has reached its command limit.', 503);
    if (seat.left && op !== 'leave') fail('BAD_STATE', 'You have left this room.', 409);
    const repeatedLeave = op === 'leave' && seat.left;
    switch (op) {
      case 'accept':
      case 'reject': {
        if (seatIndex !== 0) fail('FORBIDDEN', 'Only the host can decide admission.', 403);
        text(p.joinId, 'join ID', 128);
        if (room.status !== 'lobby' || !room.pending || room.pending.id !== p.joinId) fail('BAD_STATE', 'That admission request is no longer pending.', 409);
        if (op === 'accept') {
          if (room.seats[1]) fail('ROOM_FULL', 'Both seats are occupied.', 409);
          if (room.inviteExpires <= this.now()) fail('INVALID_INVITE', 'This invitation has expired.', 403);
          room.seats[1] = room.pending;
          room.inviteConsumed = true;
        }
        room.pending = null;
        break;
      }
      case 'deck': {
        this.lobby(room);
        if (!object(p.deck) || Object.keys(p.deck).some(k => !['name', 'legends', 'cards'].includes(k)) || !Array.isArray(p.deck.cards) || !Array.isArray(p.deck.legends) || p.deck.cards.length > 50 || p.deck.legends.length > 3 || [...p.deck.cards, ...p.deck.legends].some(id => typeof id !== 'string' || !id || id.length > 128) || (p.deck.name !== undefined && (typeof p.deck.name !== 'string' || p.deck.name.length > 160))) fail('INVALID_DECK', 'A deck is sent as a name, up to three Legend ids and up to 50 main-deck card ids.', 422);
        // The server's rules decide, and say why: a player fixes a deck faster with the reason.
        let verdict = null;
        try { verdict = this.engine.validateDeck(copy(p.deck)); } catch {}
        if (!verdict?.valid) {
          const reasons = Array.isArray(verdict?.errors) ? verdict.errors.map(e => typeof e === 'string' ? e : e?.message).filter(Boolean) : [];
          fail('INVALID_DECK', reasons.length ? `This deck is not legal here: ${reasons.slice(0, 3).join(' ')}${reasons.length > 3 ? ` (+${reasons.length - 3} more)` : ''}` : 'Choose a legal supported deck.', 422);
        }
        seat.deck = copy(p.deck); seat.ready = false;
        break;
      }
      case 'ready': {
        this.lobby(room);
        if (typeof p.ready !== 'boolean') fail('INVALID_REQUEST', 'Ready must be a boolean.');
        if (p.ready && !seat.deck) fail('INVALID_DECK', 'Choose a legal deck before readying.', 422);
        seat.ready = p.ready;
        if (room.seats.every(s => s?.ready && s.deck)) {
          try {
            room.game = this.engine.createMatch(room.seats.map(s => ({...copy(s.deck), name: s.name})), randomBytes(4).readUInt32BE());
          } catch { fail('INVALID_DECK', 'The selected decks could not start a match.', 422); }
          room.status = 'playing'; room.gameRevision++;
        }
        break;
      }
      case 'action': {
        if (!Number.isSafeInteger(p.expectedRevision) || p.expectedRevision < 0) fail('INVALID_REQUEST', 'A valid game revision is required.');
        if (p.expectedRevision !== room.gameRevision) fail('STALE_REVISION', 'The game changed; refresh before acting.', 409);
        if (room.status !== 'playing') fail('BAD_STATE', 'There is no active match.', 409);
        text(p.actionId, 'action ID', 4096);
        const offered = this.engine.legalActions(copy(room.game), seatIndex).find(a => a.id === p.actionId);
        if (!offered) fail('ILLEGAL_ACTION', 'That action is not currently offered to you.', 422);
        const result = this.engine.applyAction(copy(room.game), seatIndex, copy(offered));
        if (!result?.ok || !result.state) fail('ILLEGAL_ACTION', 'That action could not be applied.', 422);
        room.game = result.state; room.gameRevision++;
        if (room.game.phase === 'over') room.status = 'over';
        break;
      }
      case 'message': {
        if (room.status === 'closed') fail('BAD_STATE', 'This room is closed.', 409);
        const message = text(p.text, 'message', 2000);
        room.messages.push({id: secret(), sender: seatIndex, text: message, at: this.now()});
        room.messages = room.messages.slice(-100);
        break;
      }
      case 'signal': {
        if (room.status === 'closed' || !room.seats[1] || room.seats.some(s => s.left)) fail('BAD_STATE', 'Voice requires both accepted players.', 409);
        text(p.callId, 'call ID', 128);
        if (!['offer', 'answer', 'ice', 'hangup'].includes(p.kind) || !object(p.data) || Buffer.byteLength(JSON.stringify(p.data)) > 16384) fail('INVALID_REQUEST', 'Invalid voice signal.');
        room.signals.push({id: secret(), sender: seatIndex, callId: p.callId, kind: p.kind, data: copy(p.data), at: this.now()});
        room.signals = room.signals.slice(-64);
        break;
      }
      case 'leave': {
        if (!seat.left) {
          if (room.status === 'playing') {
            const concede = this.engine.legalActions(copy(room.game), seatIndex).find(a => a.kind === 'concede');
            const result = concede && this.engine.applyAction(copy(room.game), seatIndex, copy(concede));
            if (!result?.ok || !result.state || result.state.phase !== 'over') fail('ILLEGAL_ACTION', 'The match could not be conceded.', 422);
            room.game = result.state; room.status = 'over'; room.gameRevision++;
          } else room.status = 'closed';
          seat.left = true; seat.ready = false;
          room.pending = null; room.signals = [];
        }
        break;
      }
      case 'rematch': {
        if (room.status !== 'over' || room.seats.some(s => !s || s.left)) fail('BAD_STATE', 'Both players must still be seated after a completed match.', 409);
        room.status = 'lobby'; room.game = null; room.gameRevision++; room.turnDeadline = null; room.timeout = null;
        for (const s of room.seats) s.ready = false;
        room.signals = [];
        break;
      }
      case 'notify': {
        if (p.webhook !== null) {
          if (!object(p.webhook) || Object.keys(p.webhook).some(k => !['url', 'token'].includes(k)) || typeof p.webhook.url !== 'string' || p.webhook.url.length > 2048 || typeof p.webhook.token !== 'string' || !p.webhook.token || p.webhook.token.length > 4096) fail('INVALID_REQUEST', 'Invalid alert destination.');
          let url;
          try { url = new URL(p.webhook.url); } catch { fail('INVALID_REQUEST', 'Invalid alert destination.'); }
          if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) fail('INVALID_REQUEST', 'Invalid alert destination.');
        }
        seat.webhook = p.webhook ? {url: p.webhook.url, token: p.webhook.token} : null;
        break;
      }
    }
    if (['ready', 'action'].includes(op)) this.armTimer(room);
    if (['ready', 'action'].includes(op) && room.seats.some(s => s?.ai)) this.playAI(room);
    seat.lastSeen = this.now();
    seat.requests.push({id: p.requestId, fingerprint: fp});
    if (!repeatedLeave) room.revision++;
    const result = this.snapshot(room, seatIndex);
    await this.commit(room);
    this.announce(existing, room, seatIndex);
    await this.settleTournamentRoom(room);
    return result;
  }

  /** Each accepted move restarts the mover's opponent clock; no timer means no deadline. */
  armTimer(room) {
    room.turnDeadline = room.status === 'playing' && room.turnTimer ? this.now() + room.turnTimer : null;
  }
  /** A player whose clock ran out concedes the moment anyone touches the room. */
  expire(room) {
    if (room.status !== 'playing' || !room.turnDeadline || this.now() < room.turnDeadline || !room.game) return false;
    const actor = room.game.actor;
    const result = this.engine.applyAction(copy(room.game), actor, CONCEDE);
    if (!result?.ok) { room.turnDeadline = null; return false; }
    room.game = result.state; room.gameRevision++; room.status = 'over'; room.turnDeadline = null;
    room.timeout = {seat: actor, at: this.now()};
    return true;
  }
  connected(seat) { return !!seat && (seat.ai === true || (!seat.left && seat.lastSeen !== null && this.now() - seat.lastSeen < CONNECTED_WINDOW)); }
  /** After a commit: tell an absent player that the table is waiting on them, or that the match ended. */
  announce(before, room, mover) {
    if (!room.game) return;
    const beforeActor = before.game?.actor ?? null, afterActor = room.game.actor;
    const targets = [];
    if (room.status === 'playing' && afterActor !== beforeActor && afterActor !== mover) targets.push(afterActor);
    if (room.status === 'over' && before.status === 'playing') for (const i of [0, 1]) if (i !== mover) targets.push(i);
    for (const i of new Set(targets)) {
      const seat = room.seats[i];
      if (!seat?.webhook || seat.ai || this.connected(seat)) continue;
      const other = room.seats[1 - i]?.name || 'Your friend';
      const left = room.turnDeadline ? ` You have ${Math.max(1, Math.round((room.turnDeadline - this.now()) / 3600000))} hours before the clock runs out.` : '';
      const payload = room.status === 'playing'
        ? {title: 'Your move at the Night City table', body: `${other} played.${left}`, dedup_key: `nct:${room.id}:move`, priority: 'normal', url: '/?view=apps&app=cyberpunk-tcg'}
        : {title: 'Your Night City match is over', body: room.timeout ? `${room.seats[room.timeout.seat]?.name || 'A player'} ran out of time.` : `${other} finished the game.`, dedup_key: `nct:${room.id}:over`, priority: 'normal', url: '/?view=apps&app=cyberpunk-tcg'};
      Promise.resolve(this.deliver(seat.webhook, payload)).catch(() => false);
    }
  }

  lobby(room) { if (room.status !== 'lobby') fail('BAD_STATE', 'This operation requires the lobby.', 409); }

  async join(room, p) {
    this.compatible(p);
    const name = text(p.name, 'player name', 80).trim();
    text(p.requestId, 'request ID', 128);
    if (typeof p.invite !== 'string' || p.invite.length > 256 || !eqHash(p.invite, room.inviteHash)) fail('INVALID_INVITE', 'This invitation is invalid or expired.', 403);
    const fp = fingerprint('join', p), previous = room.joins.find(j => j.id === p.requestId);
    const token = createHmac('sha256', p.invite).update(`night-city-rooms:seat:1:${room.id}:${p.requestId}`).digest('base64url');
    if (previous) {
      if (previous.fingerprint !== fp) fail('REQUEST_CONFLICT', 'This request ID was already used for a different command.', 409);
      const accepted = room.seats[1] && eqHash(token, room.seats[1].tokenHash);
      const pending = room.pending && eqHash(token, room.pending.tokenHash);
      if (!accepted && !pending) fail('FORBIDDEN', 'This admission request was revoked.', 403);
      return {roomId: room.id, token, seat: 1, snapshot: this.snapshot(room, 1, !accepted)};
    }
    if (room.inviteConsumed || room.inviteExpires <= this.now()) fail('INVALID_INVITE', 'This invitation is invalid or expired.', 403);
    if (room.seats[1] || room.pending) fail('ROOM_FULL', 'The guest seat is already occupied or pending.', 409);
    this.lobby(room);
    if (room.joins.length >= MAX_REQUESTS) fail('CAPACITY', 'This room has reached its admission limit.', 503);
    room.pending = {...this.newSeat(name, token), id: secret()};
    room.joins.push({id: p.requestId, fingerprint: fp, tokenHash: digest(token)});
    room.revision++;
    const result = {roomId: room.id, token, seat: 1, snapshot: this.snapshot(room, 1, true)};
    await this.commit(room);
    return result;
  }

  snapshot(room, self, pending = false) {
    const players = room.seats.map((s, i) => ({name: s?.name || (i === 1 ? room.pending?.name || '' : ''), accepted: !!s, ready: !!s?.ready, deckReady: !!s?.deck, connected: this.connected(s)}));
    let view = null, actions = [];
    if (!pending && room.game) {
      view = this.engine.viewFor(copy(room.game), self);
      actions = room.status === 'playing' && !room.seats[self].left ? this.engine.legalActions(copy(room.game), self) : [];
    }
    const yourMove = !pending && room.status === 'playing' && !!room.game && room.game.actor === self;
    return copy({roomId: room.id, revision: room.revision, gameRevision: room.gameRevision, status: room.status, self, players, pendingJoin: room.pending && self === 0 ? {id: room.pending.id, name: room.pending.name} : null, view, actions, messages: pending ? [] : room.messages, signals: pending ? [] : room.signals.filter(s => s.sender !== self), protocol: PROTOCOL_VERSION, rulesVersion: this.rulesVersion, catalogDigest: this.catalogDigest, yourMove, turnTimer: room.turnTimer ?? null, turnDeadline: room.status === 'playing' ? room.turnDeadline ?? null : null, timeout: room.timeout ? {seat: room.timeout.seat, at: room.timeout.at} : null, alerts: !pending && !!room.seats[self]?.webhook, friendInvite: this.friendInviteInfo(room), randomMatch: !!room.randomMatch, now: this.now()});
  }

  async commit(room) {
    const next = new Map(this.rooms); next.set(room.id, room);
    const data = JSON.stringify({protocol: PROTOCOL_VERSION, rulesVersion: this.rulesVersion, catalogDigest: this.catalogDigest, rooms: [...next.values()].map(r => ({...r, signals: []}))});
    const temporary = path.join(this.storageDir, `.rooms-${secret()}.tmp`);
    let handle;
    try {
      handle = await open(temporary, 'wx', 0o600);
      await handle.writeFile(data, 'utf8');
      await handle.sync();
      await handle.close(); handle = null;
      await rename(temporary, this.file);
    } catch {
      if (handle) await handle.close().catch(() => {});
      await unlink(temporary).catch(() => {});
      fail('STORAGE_FAILURE', 'The command could not be saved. Please retry.', 503);
    }
    this.rooms = next;
  }
}
// Tournaments extend the prototype; installed after the class exists.
TOURNEY_FIELDS = installTournaments(RoomService, {secret, digest, eqHash, text, object, copy, fail, canonical, PROTOCOL_VERSION});
FRIEND_FIELDS = installFriends(RoomService, {secret, digest, eqHash, text, object, copy, fail, PROTOCOL_VERSION});
