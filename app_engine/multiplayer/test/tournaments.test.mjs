import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {engine} from './fake-rules.mjs';
import {RoomService} from '../rooms.mjs';
import {fillField,robinRounds,firstRound,nextRound,standings,standingsText} from '../bracket.mjs';

const compatibility={protocol:1,rulesVersion:engine.rulesVersion,catalogDigest:engine.catalogDigest};
const deck=engine.generateDeck({},1701);
let sequence=0;const rid=()=>`r-${++sequence}`;
const DAY=86400000;

test('brackets fill with AI seats, pair by seed, advance winners and rank the field',()=>{
 const humans=[{id:'p1',name:'A'},{id:'p2',name:'B'},{id:'p3',name:'C'}];
 const single=fillField(humans,'single');
 assert.equal(single.length,4);assert.equal(single[3].ai,true);assert.match(single[3].name,/AI/);
 assert.equal(fillField(humans,'single',false).length,3);
 assert.equal(fillField(humans,'robin').length,4);
 assert.equal(fillField([humans[0]],'single').length,2,'a lone player still gets a bracket');
 const r1=firstRound(single.map(p=>p.id));
 assert.deepEqual(r1.map(m=>[m.a,m.b]),[['p1','p2'],['p3','ai-1']]);
 const bye=firstRound(['p1','p2','p3']);
 assert.equal(bye[1].b,null);
 r1[0].result={winner:'p2',tie:false};r1[1].result={winner:'ai-1',tie:false};
 const r2=nextRound(r1,1);
 assert.deepEqual(r2.map(m=>[m.a,m.b]),[['p2','ai-1']]);
 const robin=robinRounds(['p1','p2','p3','p4']);
 assert.equal(robin.length,3);
 const seen=new Set(robin.flat().map(m=>[m.a,m.b].sort().join('-')));
 assert.equal(seen.size,6,'every pair meets once');
 const odd=robinRounds(['p1','p2','p3']);
 assert.equal(odd.length,3);assert.ok(odd.every(r=>r.some(m=>m.b===null)),'odd fields carry a bye each round');
 const rows=standings(single,[r1,r2.map(m=>({...m,result:{winner:'p2',tie:false}}))]);
 assert.deepEqual(rows.map(r=>[r.name,r.wins,r.losses,r.rank]),[['B',2,0,1],['Wire // AI',1,1,2],['A',0,1,3],['C',0,1,3]]);
 assert.match(standingsText('Cup',rows),/^Night City tournament · Cup\n1\. B · 2W 0L\n2\. Wire \/\/ AI \(AI\) · 1W 1L/);
});

async function service({now}){
 const storageDir=await mkdtemp(join(tmpdir(),'night-city-tourney-'));
 const deliveries=[];
 const s=new RoomService({engine,...compatibility,storageDir,now,deliver:(webhook,payload)=>{deliveries.push({webhook,payload});return Promise.resolve(true);}});
 await s.init();
 return {s,storageDir,deliveries};
}
const play=async(s,session,pick)=>{const snap=await s.dispatch('snapshot',{roomId:session.roomId},session.token);const action=pick(snap.actions);if(!action)return snap;return s.dispatch('action',{roomId:session.roomId,requestId:rid(),expectedRevision:snap.gameRevision,actionId:action.id},session.token);};
const readyUp=async(s,session)=>{await s.dispatch('deck',{roomId:session.roomId,requestId:rid(),deck},session.token);return s.dispatch('ready',{roomId:session.roomId,requestId:rid(),ready:true},session.token);};

test('a three-player single elimination with an AI fill runs to a champion through ordinary rooms',async()=>{
 let clock=1_900_000_000_000;
 const {s,storageDir,deliveries}=await service({now:()=>clock});
 try{
  const owner=await s.dispatch('tourneyCreate',{name:'Kabuki Cup',playerName:'Alex',format:'single',clock:'24h',ai:true,...compatibility});
  assert.equal(owner.snapshot.owner,true);assert.equal(owner.snapshot.status,'lobby');
  const b=await s.dispatch('tourneyJoin',{tournamentId:owner.tournamentId,invite:owner.invite,name:'Blair',requestId:'j1',...compatibility});
  const again=await s.dispatch('tourneyJoin',{tournamentId:owner.tournamentId,invite:owner.invite,name:'Blair',requestId:'j1',...compatibility});
  assert.equal(again.playerId,b.playerId,'joining twice with one request is one seat');
  const c=await s.dispatch('tourneyJoin',{tournamentId:owner.tournamentId,invite:owner.invite,name:'Casey',requestId:'j2',...compatibility});
  await assert.rejects(s.dispatch('tourneyJoin',{tournamentId:owner.tournamentId,invite:'wrong',name:'X',requestId:'j3',...compatibility}),e=>e.code==='INVALID_INVITE');
  await assert.rejects(s.dispatch('tourneyStart',{tournamentId:owner.tournamentId,requestId:rid()},b.token),e=>e.code==='FORBIDDEN');
  await s.dispatch('tourneyNotify',{tournamentId:owner.tournamentId,requestId:rid(),webhook:{url:'https://192.168.1.5:9900/api/app-notify/cyberpunk-tcg',token:'t'}},c.token);
  clock+=40000;
  const started=await s.dispatch('tourneyStart',{tournamentId:owner.tournamentId,requestId:rid()},owner.token);
  assert.equal(started.status,'playing');assert.equal(started.players.length,4);assert.equal(started.players[3].ai,true);
  assert.equal(started.rounds.length,1);assert.equal(started.rounds[0].matches.length,2);
  assert.equal(started.rounds[0].deadline,clock+DAY);
  assert.ok(started.pending,'the owner has a table waiting');
  assert.equal(started.pending.opponent,'Blair');
  const cSnap=await s.dispatch('tourneySnapshot',{tournamentId:owner.tournamentId},c.token);
  assert.match(cSnap.pending.opponent,/AI/);
  assert.equal(deliveries.length,1,'the absent player with a webhook heard the pairing');
  assert.match(deliveries[0].payload.title,/round 1 is paired/);
  // Casey versus the AI seat: the service plays the AI's turns.
  let room=await readyUp(s,cSnap.pending.session);
  assert.equal(room.status,'playing');
  assert.equal(room.players[1].name.includes('AI'),true);
  assert.equal(room.players[1].connected,true);
  let guard=0;
  while(room.status==='playing'&&guard++<400){
   if(!room.yourMove){room=await s.dispatch('snapshot',{roomId:cSnap.pending.session.roomId},cSnap.pending.session.token);assert.ok(room.yourMove||room.status!=='playing','the AI never leaves the human waiting');continue;}
   room=await play(s,cSnap.pending.session,actions=>actions.find(a=>a.kind==='concede'));
  }
  assert.equal(room.status,'over');
  const afterC=await s.dispatch('tourneySnapshot',{tournamentId:owner.tournamentId},c.token);
  assert.equal(afterC.rounds[0].matches[1].result.winner,'ai-1');
  assert.equal(afterC.pending,null);
  // Alex versus Blair at a normal table; Blair concedes.
  const bSnap=await s.dispatch('tourneySnapshot',{tournamentId:owner.tournamentId},b.token);
  await readyUp(s,started.pending.session);await readyUp(s,bSnap.pending.session);
  await s.dispatch('leave',{roomId:bSnap.pending.session.roomId,requestId:rid()},bSnap.pending.session.token);
  const round2=await s.dispatch('tourneySnapshot',{tournamentId:owner.tournamentId},owner.token);
  assert.equal(round2.rounds.length,2,'a complete round pairs the next');
  assert.deepEqual([round2.rounds[1].matches[0].a,round2.rounds[1].matches[0].b],['p1','ai-1']);
  assert.equal(round2.pending.matchId,'r2m1');
  // The final: Alex lets the clock run out; the round deadline decides it.
  clock+=DAY+1;
  const decided=await s.dispatch('tourneySnapshot',{tournamentId:owner.tournamentId},owner.token);
  assert.equal(decided.status,'complete');
  assert.equal(decided.winner,'ai-1');
  assert.equal(decided.standings[0].id,'ai-1');
  assert.equal(decided.rounds[1].matches[0].result.reason,'deadline');
  assert.ok(deliveries.some(d=>/is over/.test(d.payload.title)));
  // Persistence: a fresh service sees the finished bracket and the rooms.
  const again2=new RoomService({engine,...compatibility,storageDir,now:()=>clock});
  await again2.init();
  const restored=await again2.dispatch('tourneySnapshot',{tournamentId:owner.tournamentId},owner.token);
  assert.equal(restored.status,'complete');assert.equal(restored.rounds.length,2);
 }finally{await rm(storageDir,{recursive:true,force:true});}
});

test('round robin with two AI seats plays AI-versus-AI matches inside the service and finishes',async()=>{
 let clock=1_900_000_000_000;
 const {s,storageDir}=await service({now:()=>clock});
 try{
  const owner=await s.dispatch('tourneyCreate',{name:'Ledger League',playerName:'Alex',format:'robin',ai:true,...compatibility});
  await assert.rejects(s.dispatch('tourneyCreate',{name:'x',playerName:'y',format:'swiss',...compatibility}),e=>e.code==='INVALID_REQUEST');
  const started=await s.dispatch('tourneyStart',{tournamentId:owner.tournamentId,requestId:rid()},owner.token);
  assert.equal(started.players.length,2,'one human plus one AI is an even field');
  assert.equal(started.rounds.length,1);assert.equal(started.clock,null);
  let session=started.pending.session,room=await readyUp(s,session);
  let guard=0;
  while(room.status==='playing'&&guard++<400){if(!room.yourMove){room=await s.dispatch('snapshot',{roomId:session.roomId},session.token);continue;}room=await play(s,session,actions=>actions.find(a=>a.kind==='concede'));}
  const done=await s.dispatch('tourneySnapshot',{tournamentId:owner.tournamentId},owner.token);
  assert.equal(done.status,'complete');assert.equal(done.standings.length,2);
  // Two humans who both leave in the lobby: a three-round league with an AI pair decided by simulation.
  const league=await s.dispatch('tourneyCreate',{name:'Four',playerName:'A',format:'robin',ai:true,...compatibility});
  const b=await s.dispatch('tourneyJoin',{tournamentId:league.tournamentId,invite:league.invite,name:'B',requestId:'k1',...compatibility});
  await s.dispatch('tourneyLeave',{tournamentId:league.tournamentId,requestId:rid()},b.token);
  const solo=await s.dispatch('tourneySnapshot',{tournamentId:league.tournamentId},league.token);
  assert.equal(solo.players.length,1,'leaving before the start frees the seat');
  const gone=await s.dispatch('tourneyLeave',{tournamentId:league.tournamentId,requestId:rid()},league.token);
  assert.equal(gone.closed,true);
  await assert.rejects(s.dispatch('tourneySnapshot',{tournamentId:league.tournamentId},league.token),e=>e.code==='TOURNAMENT_NOT_FOUND');
 }finally{await rm(storageDir,{recursive:true,force:true});}
});

