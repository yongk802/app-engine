/** Portable human multiplayer protocol v1. Production RoomService lives in rooms.mjs.
 * All operations return JSON and never expose full MatchState, seeds, decks, another
 * player's hand/private choices, credential hashes, or private pending engine tasks.
 * @typedef {{name:string,accepted:boolean,ready:boolean,deckReady:boolean,connected:boolean}} PublicSeat
 * @typedef {{id:string,sender:number,text:string,at:number}} ChatMessage
 * @typedef {{id:string,sender:number,callId:string,kind:string,data:object,at:number}} VoiceSignal
 * @typedef {{roomId:string,revision:number,gameRevision:number,status:string,self:number,
 * players:PublicSeat[],pendingJoin:({id:string,name:string}|null),view:object|null,
 * actions:object[],messages:ChatMessage[],signals:VoiceSignal[],protocol:number,
 * rulesVersion:string,catalogDigest:string}} RoomSnapshot
 * @typedef {{roomId:string,token:string,invite?:string,seat:number,snapshot:RoomSnapshot}} Session
 * create {name,protocol:1,rulesVersion,catalogDigest,turnTimer?:'24h'|'72h'} -> Session(owner, invite).
 *   turnTimer arms a per-move clock once the match starts; a player whose clock runs
 *   out concedes the next time anyone touches the room (snapshot included).
 * join {roomId,invite,name,protocol,rulesVersion,catalogDigest,requestId} -> Session(pending seat1).
 * snapshot {roomId} + token -> RoomSnapshot; updates heartbeat (not game revision).
 * accept {roomId,requestId,joinId} + owner token -> RoomSnapshot; admits pending seat1.
 * reject {roomId,requestId,joinId} + owner token -> RoomSnapshot; revokes pending seat1.
 * deck {roomId,requestId,deck} + accepted token -> RoomSnapshot; legal supported deck,
 *   lobby only, replaces own deck and clears own ready flag, never reveals other deck.
 * ready {roomId,requestId,ready:boolean} + accepted token -> RoomSnapshot;
 *   both ready+legal decks => engine.createMatch; server random seed; start once.
 * action {roomId,requestId,expectedRevision:gameRevision,actionId} + accepted token ->
 *   RoomSnapshot; engine validates offered action, wrong revision => STALE_REVISION.
 * message {roomId,requestId,text} + accepted token -> RoomSnapshot;
 *   plain text 1..2000 chars, last100 messages, idempotent by seat+requestId.
 * signal {roomId,requestId,callId,kind,data} + accepted token -> RoomSnapshot;
 *   kind offer|answer|ice|hangup, bounded JSON data <=16KiB, last64 signals,
 *   only delivered to opposite accepted seat; no voice recording.
 * leave {roomId,requestId} + accepted token -> RoomSnapshot; during match concede,
 *   otherwise closed; repeated leave idempotent; tab hide/navigation is NOT leave.
 * rematch {roomId,requestId} + accepted token -> RoomSnapshot; over only;
 *   first request resets lobby and clears both ready flags, decks retained.
 * notify {roomId,requestId,webhook:{url,token}|null} + accepted token -> RoomSnapshot; registers
 *   (or clears) where this seat wants move alerts. After a commit the service POSTs
 *   {title,body,url,dedup_key,priority} with header X-App-Notify-Token to an ABSENT
 *   player (no heartbeat in 30s) when the turn passes to them or the match ends.
 *   LAN/loopback https hosts may present a self-signed certificate. Fire and forget.
 * Snapshot also carries yourMove, turnTimer (ms), turnDeadline (ms epoch, playing only),
 *   timeout {seat,at} when a clock ended the match, alerts (this seat has a webhook), now.
 * tourneyCreate {name,playerName,format:'single'|'robin',clock?,ai?,protocol,rulesVersion,catalogDigest} -> {tournamentId,token,invite,playerId,snapshot}
 * tourneyJoin {tournamentId,invite,name,requestId,compat} -> {tournamentId,token,playerId,snapshot}; idempotent per requestId.
 * tourneySnapshot {tournamentId} + token; tourneyStart/tourneyNotify/tourneyLeave {tournamentId,requestId,...} + token.
 *   Start fills the field with AI seats (power of two / even), pairs round 1 and creates a room per
 *   match; the snapshot carries `pending` with this player's room session for their open match.
 *   AI seats are played by the service; a finished room decides its match; a complete round pairs the
 *   next one; a round past its clock decides open matches (room clock, else the seat that showed up).
 * dailySubmit {day,name,playerId,actions,requestId,protocol,rulesVersion,catalogDigest} (no token)
 *   -> DailyBoard; the service replays the recorded human actions against the day's seed
 *   with its own rival policy; illegal or unfinished replays are rejected, a second
 *   different result for the same player and day is REQUEST_CONFLICT. Days older than
 *   a week or later than tomorrow are BAD_STATE. Nothing but name and result is shown.
 * dailyBoard {day,playerId,protocol,rulesVersion,catalogDigest} (no token) ->
 *   {day,entries:[{name,outcome,turns,rounds,cred,at,self,rank}],protocol,rulesVersion,catalogDigest}
 *
 * Authentication: crypto-random32byte bearer per seat; invitation distinct, expires
 * after24h and consumed on accepted join. Constant-time hashed-token comparisons.
 * Pending join cannot see game/chat or perform accepted-only operations.
 * Mutation requestId required nonempty<=128 chars; retries return current safe
 * snapshot without reapplying; conflicting reuse rejected REQUEST_CONFLICT.
 * Every committed mutation atomically persists before success. Serial dispatch;
 * service restart restores matches, accepted seats, dedup, chat (voice ephemeral).
 * No stale game request can mutate RNG or game state. Max100 active rooms.
 * Player connected reflects heartbeat last30s; reconnect does not release seat.
 */
export const PROTOCOL_VERSION=1;
export class RoomError extends Error {
  constructor(code,message,status=400){super(message);this.name='RoomError';this.code=code;this.status=status;}
}
/** @param {{engine:object,rulesVersion:string,catalogDigest:string,storageDir:string,
 * now?:()=>number}} options */
export class RoomServiceContract {
  constructor(options){this.options=options;}
  /** Load persisted rooms. Corrupt files fail closed. @returns {Promise<void>} */
  async init(){throw new Error('Not implemented');}
  /** @param {string} op @param {object} payload @param {string} [token]
   * @returns {Promise<RoomSnapshot|Session>}
   * @throws {RoomError} AUTH_REQUIRED(401), ROOM_NOT_FOUND(404), FORBIDDEN(403),
   * INCOMPATIBLE(409), INVALID_DECK(422), STALE_REVISION(409), ILLEGAL_ACTION(422),
   * ROOM_FULL(409), INVALID_INVITE(403), INVALID_REQUEST(400), REQUEST_CONFLICT(409),
   * BAD_STATE(409), STORAGE_FAILURE(503), CAPACITY(503).
   */
  async dispatch(op,payload,token=''){throw new Error('Not implemented');}
}
