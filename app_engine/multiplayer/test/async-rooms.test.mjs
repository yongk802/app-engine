import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {engine} from './fake-rules.mjs';
import {RoomService,deliverWebhook} from '../rooms.mjs';

const compatibility={protocol:1,rulesVersion:engine.rulesVersion,catalogDigest:engine.catalogDigest};
const deck=engine.generateDeck({},1701);
const DAY=86400000;
let sequence=0;
const rid=()=>`r-${++sequence}`;

/** A playing room with an injectable clock and captured webhook deliveries. */
async function table({turnTimer='24h',dir=null}={}){
 const storageDir=dir||await mkdtemp(join(tmpdir(),'night-city-async-'));
 let clock=1_900_000_000_000;const deliveries=[];
 const service=new RoomService({engine,...compatibility,storageDir,now:()=>clock,deliver:(webhook,payload)=>{deliveries.push({webhook,payload});return Promise.resolve(true);}});
 await service.init();
 const owner=await service.dispatch('create',{name:'Alex',...compatibility,...(turnTimer?{turnTimer}:{})});
 const guest=await service.dispatch('join',{roomId:owner.roomId,invite:owner.invite,name:'Blair',...compatibility,requestId:rid()});
 await service.dispatch('accept',{roomId:owner.roomId,requestId:rid(),joinId:owner.snapshot.pendingJoin?.id||(await service.dispatch('snapshot',{roomId:owner.roomId},owner.token)).pendingJoin.id},owner.token);
 for(const t of [owner.token,guest.token]){await service.dispatch('deck',{roomId:owner.roomId,requestId:rid(),deck},t);await service.dispatch('ready',{roomId:owner.roomId,requestId:rid(),ready:true},t);}
 const snap=async(seat=0)=>service.dispatch('snapshot',{roomId:owner.roomId},seat===0?owner.token:guest.token);
 const act=async(seat,pick)=>{const s=await snap(seat);const action=pick(s.actions);assert.ok(action,'no action offered');return service.dispatch('action',{roomId:owner.roomId,requestId:rid(),expectedRevision:s.gameRevision,actionId:action.id},seat===0?owner.token:guest.token);};
 return {service,owner,guest,storageDir,deliveries,snap,act,tick:ms=>{clock+=ms;},now:()=>clock,cleanup:()=>dir?null:rm(storageDir,{recursive:true,force:true})};
}

test('a turn clock arms when the match starts, follows every move, and flags whose move it is',async()=>{
 const t=await table();
 try{
  const s0=await t.snap(0),s1=await t.snap(1);
  assert.equal(s0.status,'playing');assert.equal(s0.turnTimer,DAY);assert.equal(s0.turnDeadline,t.now()+DAY);
  assert.equal(s0.yourMove,s0.view.actor===0);assert.equal(s1.yourMove,s1.view.actor===1);
  assert.notEqual(s0.yourMove,s1.yourMove);
  const actor=s0.yourMove?0:1;
  t.tick(3600000);
  const after=await t.act(actor,actions=>actions.find(a=>a.kind==='pass'));
  assert.equal(after.turnDeadline,t.now()+DAY,'a move restarts the clock');
  assert.equal(after.yourMove,false);
  assert.equal((await t.snap(1-actor)).yourMove,true);
  const none=await table({turnTimer:null});
  try{const s=await none.snap(0);assert.equal(s.turnTimer,null);assert.equal(s.turnDeadline,null);}finally{await none.cleanup();}
  await assert.rejects(t.service.dispatch('create',{name:'X',...compatibility,turnTimer:'1h'}),e=>e.code==='INVALID_REQUEST');
 }finally{await t.cleanup();}
});

test('a player whose clock runs out concedes as soon as anyone touches the room',async()=>{
 const t=await table();
 try{
  const first=await t.snap(0),actor=first.yourMove?0:1;
  t.tick(DAY-1);
  assert.equal((await t.snap(1-actor)).status,'playing','one millisecond early is still live');
  t.tick(2);
  const s=await t.snap(1-actor);
  assert.equal(s.status,'over');assert.equal(s.view.winner,1-actor);
  assert.deepEqual(s.timeout,{seat:actor,at:t.now()});
  assert.equal(s.turnDeadline,null);assert.equal(s.yourMove,false);
  assert.match(s.view.log.at(-1).text,/conceded/);
  // The timed-out player sees the same verdict and cannot act.
  const mine=await t.snap(actor);
  assert.equal(mine.status,'over');assert.equal(mine.actions.length,0);
  await assert.rejects(t.service.dispatch('action',{roomId:t.owner.roomId,requestId:rid(),expectedRevision:mine.gameRevision,actionId:'x'},actor===0?t.owner.token:t.guest.token),e=>e.code==='BAD_STATE');
  // A rematch clears the verdict and the clock until the next start.
  await t.service.dispatch('rematch',{roomId:t.owner.roomId,requestId:rid()},t.owner.token);
  const lobby=await t.snap(0);
  assert.equal(lobby.status,'lobby');assert.equal(lobby.timeout,null);assert.equal(lobby.turnDeadline,null);
 }finally{await t.cleanup();}
});

test('move alerts reach only an absent player, with a registered webhook, when the turn passes to them or the match ends',async()=>{
 const t=await table();
 try{
  const first=await t.snap(0),actor=first.yourMove?0:1,other=1-actor,otherToken=other===0?t.owner.token:t.guest.token;
  const webhook={url:'https://192.168.1.20:9900/api/app-notify/cyberpunk-tcg',token:'notify-token'};
  const registered=await t.service.dispatch('notify',{roomId:t.owner.roomId,requestId:rid(),webhook},otherToken);
  assert.equal(registered.alerts,true);
  assert.equal((await t.snap(actor)).alerts,false,'alerts are per seat');
  // Connected (recent heartbeat): no alert.
  await t.act(actor,actions=>actions.find(a=>a.kind==='pass'));
  assert.equal(t.deliveries.length,0);
  // Absent for longer than the heartbeat window: alerted once the turn passes to them.
  const back=await t.act(other,actions=>actions.find(a=>a.kind==='pass'));
  assert.equal(back.yourMove,false);
  t.tick(31000);
  await t.act(actor,actions=>actions.find(a=>a.kind==='pass'));
  assert.equal(t.deliveries.length,1);
  assert.deepEqual(t.deliveries[0].webhook,webhook);
  assert.equal(t.deliveries[0].payload.title,'Your move at the Night City table');
  assert.match(t.deliveries[0].payload.body,/Alex played|Blair played/);
  assert.match(t.deliveries[0].payload.body,/24 hours/);
  assert.equal(t.deliveries[0].payload.dedup_key,`nct:${t.owner.roomId}:move`);
  assert.match(t.deliveries[0].payload.url,/^\//);
  // Clearing the webhook stops alerts; the snapshot says so.
  const cleared=await t.service.dispatch('notify',{roomId:t.owner.roomId,requestId:rid(),webhook:null},otherToken);
  assert.equal(cleared.alerts,false);
  for(const bad of [{url:'ftp://x/y',token:'t'},{url:'https://user:pw@host/x',token:'t'},{url:'https://host/x'},{url:'https://host/x',token:''},'nope'])
   await assert.rejects(t.service.dispatch('notify',{roomId:t.owner.roomId,requestId:rid(),webhook:bad},otherToken),e=>e.code==='INVALID_REQUEST');
  // Re-register, then let the absent player's clock expire: they hear the match ended.
  await t.service.dispatch('notify',{roomId:t.owner.roomId,requestId:rid(),webhook},otherToken);
  const now=await t.snap(actor);
  if(now.yourMove){t.tick(DAY+1);await t.snap(other);}
  else{t.tick(DAY+1);}
  const ended=await t.snap(actor);
  assert.equal(ended.status,'over');
  const over=t.deliveries.filter(d=>d.payload.title==='Your Night City match is over');
  assert.equal(over.length,1);
  assert.match(over[0].payload.body,/ran out of time/);
 }finally{await t.cleanup();}
});

test('webhooks and clocks survive a service restart',async()=>{
 const storageDir=await mkdtemp(join(tmpdir(),'night-city-async-'));
 try{
  const t=await table({dir:storageDir});
  const webhook={url:'http://127.0.0.1:9900/api/app-notify/cyberpunk-tcg',token:'tok'};
  await t.service.dispatch('notify',{roomId:t.owner.roomId,requestId:rid(),webhook},t.guest.token);
  const before=await t.snap(1);
  const again=new RoomService({engine,...compatibility,storageDir,now:()=>before.now+1000});
  await again.init();
  const after=await again.dispatch('snapshot',{roomId:t.owner.roomId},t.guest.token);
  assert.equal(after.alerts,true);assert.equal(after.turnDeadline,before.turnDeadline);assert.equal(after.turnTimer,DAY);
  assert.equal(after.players[0].connected,false,'a restart is not evidence of presence');
 }finally{await rm(storageDir,{recursive:true,force:true});}
});

test('webhook delivery refuses non-http destinations without throwing',async()=>{
 assert.equal(await deliverWebhook({url:'file:///etc/passwd',token:'t'},{title:'x'}),false);
 assert.equal(await deliverWebhook({url:'not a url',token:'t'},{title:'x'}),false);
 assert.equal(await deliverWebhook({url:'http://127.0.0.1:1/x',token:'t'},{title:'x'},{timeout:300}),false);
});

