/** Friend tournaments on the room service: a bracket of persisted tables. Installed onto
 * RoomService so rooms, clocks, alerts and storage discipline are shared. Human matches are
 * ordinary rooms; AI seats are played by the tactical policy inside the service. */
import {FORMATS,fillField,robinRounds,firstRound,nextRound,roundComplete,standings} from './bracket.mjs';

const MAX_TOURNAMENTS=50,MAX_PLAYERS=16,AI_MOVE_CAP=200,SIM_CAP=1500,DAYS_KEPT=60;
const TURN_TIMERS={'24h':86400000,'72h':259200000};
const aiSeed=state=>(state.turn*311+state.log.length)>>>0;

export function installTournaments(RoomService,{secret,digest,eqHash,text,object,copy,fail,canonical,PROTOCOL_VERSION}){
 const fields={
  tourneyCreate:['name','playerName','format','clock','ai','protocol','rulesVersion','catalogDigest'],
  tourneyJoin:['tournamentId','invite','name','requestId','protocol','rulesVersion','catalogDigest'],
  tourneySnapshot:['tournamentId'],tourneyStart:['tournamentId','requestId'],tourneyNotify:['tournamentId','requestId','webhook'],tourneyLeave:['tournamentId','requestId'],
 };
 Object.assign(RoomService.prototype,{
  tourneyFields(){return fields;},
  async loadTournaments(){
   const {readFile}=await import('node:fs/promises');
   let saved;
   try{saved=JSON.parse(await readFile(this.tournamentFile,'utf8'));}
   catch(error){if(error.code!=='ENOENT')throw error;return new Map();}
   if(!object(saved)||saved.version!==1||saved.rulesVersion!==this.rulesVersion||saved.catalogDigest!==this.catalogDigest||!Array.isArray(saved.tournaments)||saved.tournaments.length>1000)throw new Error('Invalid tournament storage.');
   const loaded=new Map();
   for(const t of saved.tournaments){
    if(!object(t)||typeof t.id!=='string'||!FORMATS.includes(t.format)||!['lobby','playing','complete'].includes(t.status)||!Array.isArray(t.players)||!Array.isArray(t.rounds)||typeof t.inviteHash!=='string')throw new Error('Invalid tournament.');
    for(const p of t.players)if(!object(p)||typeof p.id!=='string'||typeof p.name!=='string'||(!p.ai&&typeof p.tokenHash!=='string'))throw new Error('Invalid tournament player.');
    for(const p of t.players)p.lastSeen=null;
    loaded.set(t.id,t);
   }
   return loaded;
  },
  async commitTournaments(next){
   const {open,rename,unlink}=await import('node:fs/promises');
   const path=await import('node:path');
   const data=JSON.stringify({version:1,rulesVersion:this.rulesVersion,catalogDigest:this.catalogDigest,tournaments:[...next.values()]});
   const temporary=path.join(this.storageDir,`.tournaments-${secret()}.tmp`);
   let handle;
   try{handle=await open(temporary,'wx',0o600);await handle.writeFile(data,'utf8');await handle.sync();await handle.close();handle=null;await rename(temporary,this.tournamentFile);}
   catch{if(handle)await handle.close().catch(()=>{});await unlink(temporary).catch(()=>{});fail('STORAGE_FAILURE','The tournament could not be saved. Please retry.',503);}
   this.tournaments=next;
  },
  async saveTournament(t){const next=new Map(this.tournaments);next.set(t.id,t);await this.commitTournaments(next);},

  async tourney(op,p,token){
   if(op==='tourneyCreate'){
    this.compatible(p);
    const name=text(p.name,'tournament name',80).trim(),playerName=text(p.playerName,'player name',40).trim();
    if(!FORMATS.includes(p.format))fail('INVALID_REQUEST','Choose single elimination or round robin.');
    if(p.clock!=null&&!Object.hasOwn(TURN_TIMERS,p.clock))fail('INVALID_REQUEST','Choose a round clock of none, 24h or 72h.');
    if(p.ai!==undefined&&typeof p.ai!=='boolean')fail('INVALID_REQUEST','AI fill must be a boolean.');
    if(p.ai!==false&&!this.chooseAction)fail('MULTIPLAYER_UNAVAILABLE','This game has no AI policy; start the tournament without AI seats.',503);
    if([...this.tournaments.values()].filter(t=>t.status!=='complete').length>=MAX_TOURNAMENTS)fail('CAPACITY','Tournament capacity has been reached.',503);
    const ownerToken=secret(),invite=secret(),id=secret().slice(0,22);
    const cutoff=new Date(this.now()-DAYS_KEPT*86400000).toISOString();
    const next=new Map([...this.tournaments].filter(([,t])=>!(t.status==='complete'&&t.finishedAt&&t.finishedAt<cutoff)));
    const t={id,revision:1,name,format:p.format,clock:p.clock||null,ai:p.ai!==false,status:'lobby',owner:'p1',players:[{id:'p1',name:playerName,ai:false,tokenHash:digest(ownerToken),webhook:null,lastSeen:this.now(),requests:[]}],rounds:[],inviteHash:digest(invite),createdAt:new Date(this.now()).toISOString(),startedAt:null,finishedAt:null,winner:null};
    next.set(id,t);await this.commitTournaments(next);
    return {tournamentId:id,token:ownerToken,invite,playerId:'p1',snapshot:this.tourneySnapshot(t,'p1')};
   }
   text(p.tournamentId,'tournament ID',64);
   const existing=this.tournaments.get(p.tournamentId);
   if(!existing)fail('TOURNAMENT_NOT_FOUND','Tournament not found.',404);
   const t=copy(existing);
   if(op==='tourneyJoin'){
    this.compatible(p);
    const name=text(p.name,'player name',40).trim();text(p.requestId,'request ID',128);
    if(typeof p.invite!=='string'||p.invite.length>256||!eqHash(p.invite,t.inviteHash))fail('INVALID_INVITE','This invitation is invalid.',403);
    const playerToken=`${digest(`${p.invite}:${p.requestId}`)}`.slice(0,43);
    const previous=t.players.find(pl=>!pl.ai&&eqHash(playerToken,pl.tokenHash));
    if(previous)return {tournamentId:t.id,token:playerToken,playerId:previous.id,snapshot:this.tourneySnapshot(t,previous.id)};
    if(t.status!=='lobby')fail('BAD_STATE','This tournament has already started.',409);
    if(t.players.length>=MAX_PLAYERS)fail('CAPACITY','This tournament is full.',503);
    const id=`p${t.players.length+1}`;
    t.players.push({id,name,ai:false,tokenHash:digest(playerToken),webhook:null,lastSeen:this.now(),requests:[]});
    t.revision++;await this.saveTournament(t);
    return {tournamentId:t.id,token:playerToken,playerId:id,snapshot:this.tourneySnapshot(t,id)};
   }
   if(!token)fail('AUTH_REQUIRED','A tournament credential is required.',401);
   const me=t.players.find(pl=>!pl.ai&&eqHash(token,pl.tokenHash));
   if(!me)fail('AUTH_REQUIRED','A valid tournament credential is required.',401);
   me.lastSeen=this.now();
   if(op==='tourneySnapshot'){
    const changed=await this.enforceDeadlines(t);
    if(changed)t.revision++;
    await this.saveTournament(t);
    return this.tourneySnapshot(t,me.id);
   }
   text(p.requestId,'request ID',128);
   if(me.requests.includes(p.requestId))return this.tourneySnapshot(t,me.id);
   me.requests=[...me.requests.slice(-500),p.requestId];
   if(op==='tourneyNotify'){
    if(p.webhook!==null){
     if(!object(p.webhook)||typeof p.webhook.url!=='string'||p.webhook.url.length>2048||typeof p.webhook.token!=='string'||!p.webhook.token||p.webhook.token.length>4096)fail('INVALID_REQUEST','Invalid alert destination.');
     let url;try{url=new URL(p.webhook.url);}catch{fail('INVALID_REQUEST','Invalid alert destination.');}
     if(!['http:','https:'].includes(url.protocol)||url.username||url.password)fail('INVALID_REQUEST','Invalid alert destination.');
    }
    me.webhook=p.webhook?{url:p.webhook.url,token:p.webhook.token}:null;
   }
   if(op==='tourneyLeave'){
    if(t.status!=='lobby')fail('BAD_STATE','Leave only before the bracket starts.',409);
    if(me.id===t.owner){const next=new Map(this.tournaments);next.delete(t.id);await this.commitTournaments(next);return {tournamentId:t.id,closed:true};}
    t.players=t.players.filter(pl=>pl.id!==me.id);
   }
   if(op==='tourneyStart'){
    if(me.id!==t.owner)fail('FORBIDDEN','Only the owner starts the bracket.',403);
    if(t.status!=='lobby')fail('BAD_STATE','The bracket already started.',409);
    if(t.players.length<2&&!t.ai)fail('BAD_STATE','At least two players are needed.',409);
    t.players=fillField(t.players,t.format,t.ai);
    if(t.players.length<2)fail('BAD_STATE','At least two seats are needed.',409);
    t.status='playing';t.startedAt=new Date(this.now()).toISOString();
    if(t.format==='robin')t.plan=robinRounds(t.players.map(pl=>pl.id));
    await this.pairRound(t,0);
   }
   t.revision++;await this.saveTournament(t);
   return this.tourneySnapshot(t,me.id);
  },

  /** Creates the round's rooms: humans meet at a normal table, an AI seat plays inside the service, byes advance. */
  async pairRound(t,index){
   const matches=t.format==='robin'?t.plan[index]:index===0?firstRound(t.players.map(pl=>pl.id)):nextRound(t.rounds[index-1].matches,index);
   const round={index,deadline:t.clock?this.now()+TURN_TIMERS[t.clock]:null,matches};
   t.rounds.push(round);
   const rooms=new Map(this.rooms);
   for(const m of matches){
    if(m.a===null||m.b===null){m.result={winner:m.a??m.b,tie:false,reason:'bye'};continue;}
    const room=this.tournamentRoom(t,m);
    rooms.set(room.id,room);m.roomId=room.id;
    if(room.status==='over')this.settleMatch(t,m,room);
   }
   this.rooms=rooms;
   await this.commitAll();
   for(const m of matches)if(m.roomId&&!m.result)for(const id of [m.a,m.b]){const pl=t.players.find(x=>x.id===id),other=t.players.find(x=>x.id===(id===m.a?m.b:m.a));if(pl&&!pl.ai&&pl.webhook&&!this.connected(pl))Promise.resolve(this.deliver(pl.webhook,{title:`${t.name}: round ${index+1} is paired`,body:`You face ${other?.name||'a rival'}. Open Night City Table to play.`,dedup_key:`nct:tourney:${t.id}:${index}`,priority:'normal',url:'/?view=apps&app=cyberpunk-tcg'})).catch(()=>false);}
  },
  tournamentRoom(t,m){
   const seatFor=(id,seatToken)=>{const pl=t.players.find(x=>x.id===id);if(pl.ai){const deck=this.engine.generateDeck({name:pl.name},this.seedFrom(`${t.id}:${m.id}:${id}`));return {...this.newSeat(pl.name,seatToken),ai:true,deck,ready:true};}return this.newSeat(pl.name,seatToken);};
   const tokens=[secret(),secret()];
   const room={id:secret().slice(0,22),revision:1,gameRevision:0,status:'lobby',seats:[seatFor(m.a,tokens[0]),seatFor(m.b,tokens[1])],pending:null,inviteHash:digest(secret()),inviteExpires:0,inviteConsumed:true,joins:[],game:null,messages:[],signals:[],turnTimer:t.clock?TURN_TIMERS[t.clock]:null,turnDeadline:null,timeout:null,tournament:{id:t.id,matchId:m.id}};
   m.seats={[m.a]:{token:tokens[0],seat:0},[m.b]:{token:tokens[1],seat:1}};
   if(room.seats.every(s=>s.ai)){
    room.game=this.engine.createMatch(room.seats.map(s=>({...copy(s.deck),name:s.name})),this.seedFrom(`${t.id}:${m.id}:sim`));
    room.status='playing';room.gameRevision++;this.playAI(room,SIM_CAP);
    if(room.status!=='over'){room.game.winner=-1;room.game.phase='over';room.status='over';}
   }
   return room;
  },
  seedFrom(textValue){return parseInt(digest(textValue).slice(0,8),16)>>>0;},
  /** The policy takes every AI seat's decisions until a human is up or the game ends. */
  playAI(room,cap=AI_MOVE_CAP){
   let guard=0;
   while(room.status==='playing'&&room.game&&room.game.phase!=='over'&&room.seats[room.game.actor]?.ai&&guard++<cap){
    const actor=room.game.actor,allowed=this.engine.legalActions(copy(room.game),actor);
    const choice=this.chooseAction?this.chooseAction(this.engine.viewFor(copy(room.game),actor),allowed,aiSeed(room.game)):null;
    const action=allowed.find(a=>a.id===choice?.id&&a.kind!=='concede')||allowed.find(a=>a.kind!=='concede');
    if(!action)break;
    const result=this.engine.applyAction(copy(room.game),actor,copy(action));
    if(!result?.ok)break;
    room.game=result.state;room.gameRevision++;
    if(room.game.phase==='over')room.status='over';
   }
   if(room.status==='playing')this.armTimer(room);
  },
  settleMatch(t,m,room){
   if(m.result||room.status!=='over'||!room.game)return false;
   const w=room.game.winner;
   m.result={winner:w===0?m.a:w===1?m.b:null,tie:w===-1,reason:room.timeout?'clock':'played'};
   return true;
  },
  /** Called after every room commit: a finished tournament table decides its match and may pair the next round. */
  async settleTournamentRoom(room){
   if(!room.tournament)return;
   const existing=this.tournaments.get(room.tournament.id);if(!existing)return;
   const t=copy(existing),m=t.rounds.flatMap(r=>r.matches).find(x=>x.id===room.tournament.matchId);
   if(!m||!this.settleMatch(t,m,room))return;
   t.revision++;
   await this.advance(t);
  },
  async advance(t){
   const round=t.rounds.at(-1);
   if(round&&roundComplete(round.matches)){
    const last=t.format==='single'?round.matches.length===1:t.rounds.length>=t.plan.length;
    if(last){
     t.status='complete';t.finishedAt=new Date(this.now()).toISOString();
     const rows=standings(t.players,t.rounds.map(r=>r.matches));
     t.winner=rows[0]&&(!rows[1]||rows[1].rank!==1)?rows[0].id:null;
     await this.saveTournament(t);
     for(const pl of t.players)if(!pl.ai&&pl.webhook)Promise.resolve(this.deliver(pl.webhook,{title:`${t.name} is over`,body:t.winner?`${t.players.find(x=>x.id===t.winner)?.name} takes it.`:'Shared first place.',dedup_key:`nct:tourney:${t.id}:over`,priority:'normal',url:'/?view=apps&app=cyberpunk-tcg'})).catch(()=>false);
     return;
    }
    await this.pairRound(t,t.rounds.length);
    t.revision++;
   }
   await this.saveTournament(t);
  },
  /** A round past its deadline decides every open match: the room clock, or the seat that showed up. */
  async enforceDeadlines(t){
   const round=t.rounds.at(-1);
   if(!round||t.status!=='playing'||!round.deadline||this.now()<round.deadline)return false;
   let changed=false;const rooms=new Map(this.rooms);
   for(const m of round.matches){
    if(m.result||!m.roomId)continue;
    const room=copy(rooms.get(m.roomId));if(!room)continue;
    if(room.status==='playing'){room.turnDeadline=this.now()-1;this.expire(room);}
    else if(room.status==='lobby'){
     const ready=room.seats.map((s,i)=>s.ready?i:-1).filter(i=>i>=0);
     const winnerSeat=ready.length===1?ready[0]:(room.seats[0].lastSeen||0)>=(room.seats[1].lastSeen||0)?0:1;
     room.status='closed';m.result={winner:winnerSeat===0?m.a:m.b,tie:false,reason:'deadline'};
    }
    rooms.set(room.id,room);
    if(room.status==='over')this.settleMatch(t,m,room);
    changed=true;
   }
   if(changed){this.rooms=rooms;await this.commitAll();await this.advance(t);}
   return changed;
  },
  async commitAll(){
   const sample=[...this.rooms.values()][0];
   if(sample)await this.commit(sample);
  },
  tourneySnapshot(t,playerId){
   const me=t.players.find(pl=>pl.id===playerId);
   const rounds=t.rounds.map(r=>({index:r.index,deadline:r.deadline,matches:r.matches.map(m=>({id:m.id,a:m.a,b:m.b,roomId:m.roomId,result:m.result,mine:[m.a,m.b].includes(playerId)}))}));
   const open=t.rounds.at(-1)?.matches.find(m=>!m.result&&m.roomId&&[m.a,m.b].includes(playerId)&&m.seats?.[playerId]);
   const pending=open?{matchId:open.id,opponent:t.players.find(pl=>pl.id===(open.a===playerId?open.b:open.a))?.name||'Rival',session:{roomId:open.roomId,token:open.seats[playerId].token,seat:open.seats[playerId].seat}}:null;
   return copy({tournamentId:t.id,revision:t.revision,name:t.name,format:t.format,clock:t.clock?TURN_TIMERS[t.clock]:null,ai:t.ai,status:t.status,self:playerId,owner:t.owner===playerId,players:t.players.map(pl=>({id:pl.id,name:pl.name,ai:!!pl.ai,connected:pl.ai||this.connected(pl)})),rounds,pending,standings:standings(t.players,t.rounds.map(r=>r.matches)),winner:t.winner,alerts:!!me?.webhook,now:this.now(),protocol:PROTOCOL_VERSION,rulesVersion:this.rulesVersion,catalogDigest:this.catalogDigest});
  },
 });
 return fields;
}
