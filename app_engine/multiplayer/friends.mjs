/** Friend roster on the room service: a persistent identity per player, mutual friendships
 * added with a shareable code, presence from the same heartbeat as tables, and invitations
 * that seat a friend directly at a new table. Installed onto RoomService so rooms, clocks,
 * alerts and storage discipline are shared. Friends live in friends.json beside rooms.json
 * and, unlike rooms, survive a rules or catalog update. */
import {createHmac} from 'node:crypto';

const MAX_PLAYERS=1000,MAX_FRIENDS=50,MAX_REMEMBERED=500,INVITE_LIFETIME=86400000,MAX_TOKENS=5,QUEUE_LIFETIME=600000;
const TURN_TIMERS={'24h':86400000,'72h':259200000};
const CODE=/^ncf1:([A-Za-z0-9_-]{22}):([A-Za-z0-9_-]{1,256})$/;

export function installFriends(RoomService,{secret,digest,eqHash,text,object,copy,fail,PROTOCOL_VERSION}){
 const fields={
  friendRegister:['name','protocol','rulesVersion','catalogDigest'],
  friendSnapshot:[],friendRename:['requestId','name'],friendRecode:['requestId'],
  friendAdd:['requestId','code'],friendRemove:['requestId','friendId'],
  friendInvite:['requestId','friendId','turnTimer'],friendJoin:['requestId','roomId'],friendDecline:['requestId','roomId'],
  // Random matches: a queue of roster identities; two waiting players are seated at a fresh table.
  queueJoin:['requestId','turnTimer'],queueStatus:[],queueLeave:['requestId'],
 };
 const playerName=value=>text(value,'player name',40).trim();
 Object.assign(RoomService.prototype,{
  friendFields(){return fields;},
  async loadFriends(){
   const {readFile}=await import('node:fs/promises');
   let saved;
   try{saved=JSON.parse(await readFile(this.friendFile,'utf8'));}
   catch(error){if(error.code!=='ENOENT')throw error;return new Map();}
   if(!object(saved)||saved.version!==1||!Array.isArray(saved.players)||saved.players.length>MAX_PLAYERS)throw new Error('Invalid friend storage.');
   const loaded=new Map();
   const hash=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
   for(const p of saved.players){
    if(object(p)&&hash(p.tokenHash)&&!Array.isArray(p.tokenHashes)){p.tokenHashes=[p.tokenHash];delete p.tokenHash;}
    if(!object(p)||!/^[A-Za-z0-9_-]{22}$/.test(p.id)||typeof p.name!=='string'||p.name.length>40||!Array.isArray(p.tokenHashes)||!p.tokenHashes.length||p.tokenHashes.length>MAX_TOKENS||!p.tokenHashes.every(hash)||!hash(p.codeHash)||!Array.isArray(p.friends)||p.friends.length>MAX_FRIENDS||!p.friends.every(id=>typeof id==='string')||!Array.isArray(p.requests)||(p.player!==undefined&&typeof p.player!=='string')||(p.seatSeed!==undefined&&typeof p.seatSeed!=='string')||loaded.has(p.id))throw new Error('Invalid friend player.');
    p.lastSeen=null;
    loaded.set(p.id,p);
   }
   return loaded;
  },
  async commitFriends(next){
   const {open,rename,unlink}=await import('node:fs/promises');
   const path=await import('node:path');
   const data=JSON.stringify({version:1,players:[...next.values()]});
   const temporary=path.join(this.storageDir,`.friends-${secret()}.tmp`);
   let handle;
   try{handle=await open(temporary,'wx',0o600);await handle.writeFile(data,'utf8');await handle.sync();await handle.close();handle=null;await rename(temporary,this.friendFile);}
   catch{if(handle)await handle.close().catch(()=>{});await unlink(temporary).catch(()=>{});fail('STORAGE_FAILURE','The friend roster could not be saved. Please retry.',503);}
   this.players=next;
  },
  async savePlayers(...changed){const next=new Map(this.players);for(const p of changed)next.set(p.id,p);await this.commitFriends(next);},

  /** A friend invitation is a lobby room the inviter owns, tagged with both identities. */
  friendInviteRooms(){return [...this.rooms.values()].filter(r=>r.friendInvite);},
  friendInviteOpen(room){return room.status==='lobby'&&!room.seats[1]&&!room.friendInvite.declined&&room.inviteExpires>this.now()&&!room.seats[0].left;},
  friendInviteInfo(room){
   if(!room.friendInvite)return null;
   const to=this.players.get(room.friendInvite.to);
   return {name:to?.name||'Your friend',declined:!!room.friendInvite.declined,expired:room.inviteExpires<=this.now()};
  },

  matchesToken(pl,token){return pl.tokenHashes.some(h=>eqHash(token,h));},
  async friend(op,p,token,player=''){
   if(op==='friendRegister'){
    this.compatible(p);
    const name=playerName(p.name);
    // A host that knows who is signed in binds the roster identity to that account, so a new
    // device or browser gets the same friends back instead of a fresh, empty roster.
    const bound=player?[...this.players.values()].find(pl=>pl.player===player):null;
    if(bound){
     const again=copy(bound),playerToken=secret(),code=secret();
     again.name=name;again.tokenHashes=[...again.tokenHashes.slice(-(MAX_TOKENS-1)),digest(playerToken)];again.codeHash=code?digest(code):again.codeHash;again.lastSeen=this.now();
     await this.savePlayers(again);
     return {playerId:again.id,token:playerToken,code,snapshot:this.friendSnapshot(again)};
    }
    if(this.players.size>=MAX_PLAYERS)fail('CAPACITY','The friend roster is full.',503);
    const playerToken=secret(),code=secret(),id=secret().slice(0,22);
    const record={id,name,tokenHashes:[digest(playerToken)],codeHash:digest(code),friends:[],requests:[],createdAt:new Date(this.now()).toISOString(),lastSeen:this.now(),...(player?{player}:{})};
    await this.savePlayers(record);
    return {playerId:id,token:playerToken,code,snapshot:this.friendSnapshot(record)};
   }
   if(!token)fail('AUTH_REQUIRED','A friend credential is required.',401);
   const existing=[...this.players.values()].find(pl=>this.matchesToken(pl,token));
   if(!existing)fail('AUTH_REQUIRED','A valid friend credential is required.',401);
   const me=copy(existing);
   me.lastSeen=this.now();
   if(op==='friendSnapshot'){await this.savePlayers(me);return this.friendSnapshot(me);}
   if(op==='queueStatus'){await this.savePlayers(me);return this.queueSnapshot(me);}
   text(p.requestId,'request ID',128);
   if(me.requests.includes(p.requestId))return this.friendSnapshot(me);
   me.requests=[...me.requests.slice(-MAX_REMEMBERED),p.requestId];
   const changed=[me];
   let session=null;
   if(op.startsWith('queue')){session=await this.queue(op,p,me);}
   if(op==='friendRename')me.name=playerName(p.name);
   if(op==='friendRecode'){const code=secret();me.codeHash=digest(code);session={code};}
   if(op==='friendAdd'){
    const match=CODE.exec(typeof p.code==='string'?p.code.trim():'');
    if(!match)fail('INVALID_REQUEST','Paste a friend code beginning with ncf1:.');
    const other=this.players.get(match[1]);
    if(!other||!eqHash(match[2],other.codeHash))fail('INVALID_INVITE','This friend code is invalid or was replaced.',403);
    if(other.id===me.id)fail('INVALID_REQUEST','That is your own friend code.');
    if(!me.friends.includes(other.id)){
     if(me.friends.length>=MAX_FRIENDS||other.friends.length>=MAX_FRIENDS)fail('CAPACITY','A friend roster holds at most 50 friends.',503);
     const friend=copy(other);me.friends.push(friend.id);if(!friend.friends.includes(me.id))friend.friends.push(me.id);changed.push(friend);
    }
   }
   if(op==='friendRemove'){
    text(p.friendId,'friend ID',64);
    me.friends=me.friends.filter(id=>id!==p.friendId);
    const other=this.players.get(p.friendId);
    if(other&&other.friends.includes(me.id)){const friend=copy(other);friend.friends=friend.friends.filter(id=>id!==me.id);changed.push(friend);}
    // Open invitations between former friends are withdrawn on both sides.
    const rooms=new Map(this.rooms);let touched=false;
    for(const r of this.friendInviteRooms()){const f=r.friendInvite;if(!this.friendInviteOpen(r)||!([f.from,f.to].includes(me.id)&&[f.from,f.to].includes(p.friendId)))continue;const room=copy(r);room.status='closed';room.revision++;rooms.set(room.id,room);touched=true;}
    if(touched){this.rooms=rooms;await this.commitAll();}
   }
   if(op==='friendInvite'){
    text(p.friendId,'friend ID',64);
    const friend=this.players.get(p.friendId);
    if(!friend||!me.friends.includes(friend.id)||!friend.friends.includes(me.id))fail('FORBIDDEN','Only a friend on your roster can be invited.',403);
    if(p.turnTimer!=null&&!Object.hasOwn(TURN_TIMERS,p.turnTimer))fail('INVALID_REQUEST','Choose a turn timer of none, 24h or 72h.');
    if([...this.rooms.values()].filter(r=>r.status!=='closed').length>=100||this.rooms.size>=1000)fail('CAPACITY','Room capacity has been reached.',503);
    const rooms=new Map(this.rooms);
    // A fresh invitation replaces an earlier unanswered one to the same friend.
    for(const r of this.friendInviteRooms())if(r.friendInvite.from===me.id&&r.friendInvite.to===friend.id&&this.friendInviteOpen(r)){const old=copy(r);old.status='closed';old.revision++;rooms.set(old.id,old);}
    const seatToken=secret(),id=secret().slice(0,22);
    const room={id,revision:1,gameRevision:0,status:'lobby',seats:[this.newSeat(me.name,seatToken),null],pending:null,inviteHash:digest(secret()),inviteExpires:this.now()+INVITE_LIFETIME,inviteConsumed:false,joins:[],game:null,messages:[],signals:[],turnTimer:p.turnTimer?TURN_TIMERS[p.turnTimer]:null,turnDeadline:null,timeout:null,friendInvite:{from:me.id,to:friend.id,at:this.now()}};
    rooms.set(id,room);this.rooms=rooms;await this.commitAll();
    session={roomId:id,token:seatToken,seat:0,snapshot:this.snapshot(room,0)};
   }
   if(op==='friendJoin'||op==='friendDecline'){
    text(p.roomId,'room ID',64);
    const r=this.rooms.get(p.roomId);
    if(!r||!r.friendInvite||r.friendInvite.to!==me.id)fail('ROOM_NOT_FOUND','That invitation was not found.',404);
    const room=copy(r);
    // The seat credential is derived from the friend credential, so a retried join lands on the same seat.
    // Derived from the identity, not the device token, so any signed-in device retries onto the same seat.
    me.seatSeed||=secret();
    const seatToken=createHmac('sha256',me.seatSeed).update(`night-city-rooms:friend-seat:${room.id}`).digest('base64url');
    if(op==='friendJoin'){
     if(!(room.seats[1]&&eqHash(seatToken,room.seats[1].tokenHash))){
      if(!this.friendInviteOpen(room))fail('INVALID_INVITE','This invitation is no longer open.',403);
      room.seats[1]=this.newSeat(me.name,seatToken);room.inviteConsumed=true;room.revision++;
      const rooms=new Map(this.rooms);rooms.set(room.id,room);this.rooms=rooms;await this.commitAll();
     }
     session={roomId:room.id,token:seatToken,seat:1,snapshot:this.snapshot(room,1)};
    }else if(this.friendInviteOpen(room)){
     room.friendInvite.declined=true;room.revision++;
     const rooms=new Map(this.rooms);rooms.set(room.id,room);this.rooms=rooms;await this.commitAll();
    }
   }
   await this.savePlayers(...changed);
   const snapshot=this.friendSnapshot(me);
   if(op.startsWith('queue')){if(session)this.matched.delete(me.id);return {...this.queueSnapshot(me),match:session,roster:snapshot};}
   return session?{...session,roster:snapshot}:snapshot;
  },

  /** In-memory queue of roster identities waiting for any opponent. Two waiting identities get a
   * table with both seated; each collects their seat with queueStatus (or a repeated queueJoin).
   * Tables persist like any other; an uncollected seat is forgotten after ten minutes. */
  sweepQueue(){
   this.waiting||=new Map();this.matched||=new Map();
   const now=this.now();
   for(const [id,entry] of this.matched)if(now-entry.at>QUEUE_LIFETIME)this.matched.delete(id);
   for(const [id,entry] of this.waiting)if(now-entry.at>QUEUE_LIFETIME)this.waiting.delete(id);
   return now;
  },
  async queue(op,p,me){
   const now=this.sweepQueue();
   if(op==='queueLeave'){this.waiting.delete(me.id);return null;}
   if(this.matched.has(me.id))return this.matched.get(me.id).session;
   if(p.turnTimer!=null&&!Object.hasOwn(TURN_TIMERS,p.turnTimer))fail('INVALID_REQUEST','Choose a turn timer of none, 24h or 72h.');
   const other=[...this.waiting.keys()].find(id=>id!==me.id&&this.players.has(id));
   if(!other){if(!this.waiting.has(me.id))this.waiting.set(me.id,{at:now,turnTimer:p.turnTimer||null});return null;}
   const rival=this.players.get(other),timer=this.waiting.get(other)?.turnTimer||p.turnTimer||null;
   this.waiting.delete(other);this.waiting.delete(me.id);
   if([...this.rooms.values()].filter(r=>r.status!=='closed').length>=100||this.rooms.size>=1000)fail('CAPACITY','Room capacity has been reached.',503);
   const tokens=[secret(),secret()],id=secret().slice(0,22);
   const room={id,revision:1,gameRevision:0,status:'lobby',seats:[this.newSeat(rival.name,tokens[0]),this.newSeat(me.name,tokens[1])],pending:null,inviteHash:digest(secret()),inviteExpires:0,inviteConsumed:true,joins:[],game:null,messages:[],signals:[],turnTimer:timer?TURN_TIMERS[timer]:null,turnDeadline:null,timeout:null,randomMatch:true};
   const rooms=new Map(this.rooms);rooms.set(id,room);this.rooms=rooms;await this.commitAll();
   this.matched.set(other,{at:now,session:{roomId:id,token:tokens[0],seat:0,snapshot:this.snapshot(room,0)}});
   const mine={roomId:id,token:tokens[1],seat:1,snapshot:this.snapshot(room,1)};
   this.matched.set(me.id,{at:now,session:mine});
   return mine;
  },
  queueSnapshot(me){
   this.sweepQueue();
   const match=this.matched.get(me.id)?.session||null;
   if(match)this.matched.delete(me.id);
   return copy({waiting:this.waiting.has(me.id),waitingCount:this.waiting.size,match,now:this.now(),protocol:PROTOCOL_VERSION,rulesVersion:this.rulesVersion,catalogDigest:this.catalogDigest});
  },
  friendSnapshot(me){
   const rooms=this.friendInviteRooms();
   const friends=me.friends.map(id=>this.players.get(id)).filter(Boolean).map(pl=>({id:pl.id,name:pl.name,connected:this.connected(pl)}));
   const invites=rooms.filter(r=>r.friendInvite.to===me.id&&this.friendInviteOpen(r)).map(r=>({roomId:r.id,from:{id:r.friendInvite.from,name:this.players.get(r.friendInvite.from)?.name||'A friend'},at:r.friendInvite.at,expires:r.inviteExpires}));
   const sent=rooms.filter(r=>r.friendInvite.from===me.id&&r.status!=='closed'&&(this.friendInviteOpen(r)||r.friendInvite.declined)).map(r=>({roomId:r.id,to:{id:r.friendInvite.to,name:this.players.get(r.friendInvite.to)?.name||'A friend'},declined:!!r.friendInvite.declined,at:r.friendInvite.at}));
   return copy({playerId:me.id,name:me.name,account:me.player||null,friends,invites,sent,now:this.now(),protocol:PROTOCOL_VERSION,rulesVersion:this.rulesVersion,catalogDigest:this.catalogDigest});
  },
 });
 return fields;
}
